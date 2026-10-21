"""Acceptance and regression tests for Milestone Personal P1.1: Safety & Outcome Correctness.

Verifies:
1. Human action requests halt workflow execution and do NOT dispatch subsequent steps.
2. LocalWorker transitions human action jobs to PAUSED_FOR_REVIEW with account safety hold.
3. CLI returns non-zero exit code when human action is required or outcome is unconfirmed.
4. Causal linking between mutations and verifications:
   - Case A: verify_state -> click -> end  => OUTCOME_UNCONFIRMED
   - Case B: click -> verify_state -> click -> end  => OUTCOME_UNCONFIRMED
   - Case C: click -> verify_state -> end  => STATE_VERIFIED
   - Case D: Two independent mutations fully verified  => STATE_VERIFIED
   - Case E: Mutation on server with lost response (504) => PAUSED_FOR_REVIEW, no blind retry
5. Keyboard side effects:
   - press_key(Enter) is treated as mutating side-effect
   - press_key with explicit side_effect=True requires postcondition verification
   - Read-only navigation keys (Tab) complete without requiring postcondition
"""

import asyncio
import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.cli import ExitCode, build_parser, execute_cli_async
from app.core.policy import PolicyEvaluator, ProviderPolicy
from app.execution.browser_manager import BrowserManager
from app.orchestration.job_service import JobService
from app.persistence.models import Base, JobStatus, WorkflowApprovalStatus
from app.persistence.repository import (
    AccountRepository,
    ProviderRepository,
    WorkflowVersionRepository,
)
from app.workers.local_worker import LocalWorker
from app.workflows.results import StepStatus, WorkflowExecutionStatus, WorkflowOutcomeType
from app.workflows.schema import (
    ClickStep,
    NavigateStep,
    PressKeyStep,
    RequestHumanActionStep,
    TestIdLocator,
    VerifyStateStep,
    WorkflowDefinition,
)

# Load rewardlab app safely without colliding with backend 'app' package
rewardlab_root = Path(__file__).resolve().parents[2] / "rewardlab"
if str(rewardlab_root) not in sys.path:
    sys.path.insert(0, str(rewardlab_root))

_spec = importlib.util.spec_from_file_location("rewardlab_main_app_p1_1", rewardlab_root / "app.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["rewardlab_main_app_p1_1"] = _mod
_spec.loader.exec_module(_mod)

rewardlab_app = _mod.app
rewardlab_data = importlib.import_module("data")
rewardlab_store = rewardlab_data.store


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def rewardlab_server():
    """Runs RewardLab in a background thread on a free loopback port."""
    port = _find_free_port()
    config = uvicorn.Config(
        app=rewardlab_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    timeout = 10.0
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("RewardLab server failed to start within timeout")

    base_url = f"http://127.0.0.1:{port}"
    rewardlab_data.set_test_mode_override(True)
    yield base_url
    server.should_exit = True
    thread.join(timeout=3.0)


@pytest.fixture
def policy_evaluator(rewardlab_server: str) -> PolicyEvaluator:
    parsed_port = int(rewardlab_server.split(":")[-1])
    evaluator = PolicyEvaluator()
    evaluator.policies["rewardlab"] = ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist=["127.0.0.1", "localhost"],
        allowed_ports=[parsed_port],
        allowed_actions=[
            "navigate",
            "click",
            "fill",
            "wait_visible",
            "verify_state",
            "save_result",
            "hover",
            "scroll",
            "press_key",
            "wait_for_state",
            "assert_visible",
            "extract_text",
            "notify_user",
            "request_human_action",
        ],
    )
    return evaluator


@pytest.fixture
async def browser_mgr() -> Any:
    """Manages ephemeral browser manager instance for tests."""
    mgr = BrowserManager(headless=True)
    await mgr.start()
    yield mgr
    await mgr.stop()


@pytest.fixture
async def sqlite_session_factory(tmp_path: Any) -> Any:
    """File-backed SQLite session factory with WAL mode and initial schema."""
    db_file = tmp_path / "test_p1_1.sqlite"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


# -----------------------------------------------------------------------------
# 1. Human Action Must Stop Execution
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_1_human_action_halts_dispatch_and_skips_trailing_click(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Verifies that RequestHumanActionStep halts dispatch and does not execute subsequent steps."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_01"]
    session_id = rewardlab_store.authenticate(user.username, "test_password_01")
    assert session_id is not None

    # Workflow: navigate -> request_human_action -> click
    # The trailing click targets the scroll button which changes text on click
    wf = WorkflowDefinition(
        name="wf-human-action-halt",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            RequestHumanActionStep(
                id="s2",
                prompt="Security challenge checkpoint requiring manual operator resolution",
                timeout_seconds=30,
            ),
            ClickStep(
                id="s3",
                target=TestIdLocator(value="scroll-target-btn"),
                side_effect=True,
            ),
        ],
    )

    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id=user.username,
        session_id=session_id,
        policy_evaluator=policy_evaluator,
        base_url=rewardlab_server,
    )

    # Outcome must be HUMAN_ACTION_REQUIRED and status FAILED
    assert res.status == WorkflowExecutionStatus.FAILED
    assert res.outcome_type == WorkflowOutcomeType.HUMAN_ACTION_REQUIRED
    assert res.requires_verification is True
    assert res.error_code == "HUMAN_ACTION_REQUIRED"
    assert "human action required" in res.error_message.lower()

    # Step s1 succeeded, s2 succeeded, s3 was SKIPPED
    assert len(res.step_results) == 3
    assert res.step_results[0].status == StepStatus.SUCCESS
    assert res.step_results[1].status == StepStatus.SUCCESS
    assert res.step_results[1].outcome_type == WorkflowOutcomeType.HUMAN_ACTION_REQUIRED
    assert res.step_results[2].status == StepStatus.SKIPPED


@pytest.mark.asyncio
async def test_p1_1_human_action_pauses_local_worker_with_safety_hold(
    rewardlab_server: str,
    sqlite_session_factory: Any,
    policy_evaluator: PolicyEvaluator,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verifies LocalWorker transitions human action workflows to PAUSED_FOR_REVIEW."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_04"]
    sess_id = rewardlab_store.authenticate(user.username, "test_password_04")
    assert sess_id is not None
    monkeypatch.setenv("REWARDLAB_SESSION_ID", sess_id)

    async with sqlite_session_factory() as session:
        prov_repo = ProviderRepository(session)
        await prov_repo.create_or_update(
            provider_id="rewardlab",
            adapter_kind="rewardlab",
            policy_version=1,
        )

        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            provider_id="rewardlab",
            owner_id="operator_1",
            label="Test 04",
            account_id="test_04",
        )

        wf_def = WorkflowDefinition(
            name="wf-human-pause",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/security-challenge"),
                RequestHumanActionStep(
                    id="s2",
                    prompt="CAPTCHA challenge requires operator",
                    timeout_seconds=30,
                ),
                ClickStep(id="s3", target=TestIdLocator(value="ignored-btn")),
            ],
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            workflow_key="wf-human-pause",
            version=1,
            provider_id="rewardlab",
            definition_json=wf_def.model_dump(),
            approval_status=WorkflowApprovalStatus.APPROVED,
        )
        await session.commit()

        job_svc = JobService(session)
        job, _ = await job_svc.submit_job(
            workflow_version_id=wf.id,
            account_id="test_04",
            idempotency_key="idem-human-pause-01",
        )
        await session.commit()
        job_id = job.id

    worker = LocalWorker(
        session_factory=sqlite_session_factory,
        max_concurrency=1,
        poll_interval_seconds=0.1,
        base_url=rewardlab_server,
        policy_evaluator=policy_evaluator,
        headless=True,
    )
    await worker.start()

    try:
        for _ in range(30):
            await asyncio.sleep(0.3)
            async with sqlite_session_factory() as check_session:
                job_svc = JobService(check_session)
                j = await job_svc.job_repo.get_by_id(job_id)
                if j and j.status in (JobStatus.PAUSED_FOR_REVIEW.value, JobStatus.FAILED.value):
                    break

        async with sqlite_session_factory() as verify_session:
            job_svc = JobService(verify_session)
            final_job = await job_svc.job_repo.get_by_id(job_id)
            assert final_job is not None
            assert final_job.status == JobStatus.PAUSED_FOR_REVIEW.value
            assert final_job.error_code == "HUMAN_ACTION_REQUIRED"

            # Verify account safety hold: next job claim for test_04 must be skipped
            held_job = await job_svc.claim_job(
                worker_id="other_worker",
                provider_id="rewardlab",
                lease_duration_seconds=30,
            )
            assert held_job is None
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_p1_1_cli_human_action_returns_non_zero_exit_code(
    tmp_path: Any, rewardlab_server: str, policy_evaluator: PolicyEvaluator
):
    """Verifies CLI returns ExitCode.FAILED (1) when human action is required."""
    wf_yaml = f"""
schema_version: 1
id: test_cli_human
name: Test CLI Human
provider: rewardlab
execution:
  timeout_seconds: 15
steps:
  - id: s1
    action: navigate
    url: {rewardlab_server}/ui-fixtures
  - id: s2
    action: request_human_action
    prompt: Security review needed
  - id: s3
    action: click
    target:
      by: test_id
      value: scroll-target-btn
"""
    wf_file = tmp_path / "wf_human.yaml"
    wf_file.write_text(wf_yaml, encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(
        [
            str(wf_file),
            "--base-url",
            rewardlab_server,
            "--account",
            "test_01",
            "--headless",
        ]
    )
    exit_code, _ = await execute_cli_async(args, evaluator=policy_evaluator)
    # CLI must NOT return SUCCESS (0)
    assert exit_code == ExitCode.FAILED


# -----------------------------------------------------------------------------
# 2. Causal Outcome Verification (Cases A - E)
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_1_causal_case_a_verify_before_click_yields_unconfirmed(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case A: verify_state -> click -> end  => OUTCOME_UNCONFIRMED."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_01"]
    tasks = rewardlab_store.get_tasks_for_user(user.username)
    task_01 = next(t for t in tasks if t.id == "task_01")
    task_01.reviewed = False

    session_id = rewardlab_store.authenticate(user.username, "test_password_01")
    assert session_id is not None

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-a",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                # Verification happens BEFORE the mutating click!
                VerifyStateStep(
                    id="s2",
                    task_id="task_01",
                    field="reviewed",
                    equals=False,
                ),
                ClickStep(id="s3", target=TestIdLocator(value="mark-reviewed-btn")),
            ],
        )

        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=session_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.SUCCESS
        assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
        assert res.requires_verification is True
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_p1_1_causal_case_b_click_verify_click_yields_unconfirmed(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case B: click -> verify_state -> click -> end  => OUTCOME_UNCONFIRMED."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_02"]
    tasks = rewardlab_store.get_tasks_for_user(user.username)
    t1 = next(t for t in tasks if t.id == "task_01")
    t2 = next(t for t in tasks if t.id == "task_02")
    t1.reviewed = False
    t2.reviewed = False

    session_id = rewardlab_store.authenticate(user.username, "test_password_02")
    assert session_id is not None

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-b",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                # Mutation 1
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
                # Verifies Mutation 1
                VerifyStateStep(
                    id="s3",
                    task_id="task_01",
                    field="reviewed",
                    equals=True,
                ),
                # Mutation 2 on task_02: unverified trailing mutation!
                NavigateStep(id="s4", url=f"{rewardlab_server}/tasks/task_02"),
                ClickStep(id="s5", target=TestIdLocator(value="mark-reviewed-btn")),
            ],
        )

        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=session_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.SUCCESS
        # Second mutation was never verified: outcome must be UNCONFIRMED
        assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
        assert res.requires_verification is True
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_p1_1_causal_case_c_click_verify_yields_state_verified(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case C: click -> verify_state -> end  => STATE_VERIFIED."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_03"]
    tasks = rewardlab_store.get_tasks_for_user(user.username)
    task_01 = next(t for t in tasks if t.id == "task_01")
    task_01.reviewed = False

    session_id = rewardlab_store.authenticate(user.username, "test_password_03")
    assert session_id is not None

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-c",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
                VerifyStateStep(
                    id="s3",
                    task_id="task_01",
                    field="reviewed",
                    equals=True,
                ),
            ],
        )

        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=session_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.SUCCESS
        assert res.outcome_type == WorkflowOutcomeType.STATE_VERIFIED
        assert res.requires_verification is False
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_p1_1_causal_case_d_two_mutations_each_verified_yields_state_verified(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case D: Two independent mutations fully verified  => STATE_VERIFIED."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_05"]
    tasks = rewardlab_store.get_tasks_for_user(user.username)
    t1 = next(t for t in tasks if t.id == "task_01")
    t2 = next(t for t in tasks if t.id == "task_02")
    t1.reviewed = False
    t2.reviewed = False

    session_id = rewardlab_store.authenticate(user.username, "test_password_05")
    assert session_id is not None

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-d",
            provider="rewardlab",
            steps=[
                # Mutation 1 + Verification 1
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
                VerifyStateStep(
                    id="s3",
                    task_id="task_01",
                    field="reviewed",
                    equals=True,
                ),
                # Mutation 2 + Verification 2
                NavigateStep(id="s4", url=f"{rewardlab_server}/tasks/task_02"),
                ClickStep(id="s5", target=TestIdLocator(value="mark-reviewed-btn")),
                VerifyStateStep(
                    id="s6",
                    task_id="task_02",
                    field="reviewed",
                    equals=True,
                ),
            ],
        )

        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=session_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.SUCCESS
        # Both mutations had causal verifications: STATE_VERIFIED
        assert res.outcome_type == WorkflowOutcomeType.STATE_VERIFIED
        assert res.requires_verification is False
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_p1_1_causal_case_e_lost_response_requires_verification(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case E: Mutation on server but lost response flags OUTCOME_UNCONFIRMED without auto-retry."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_06"]
    tasks = rewardlab_store.get_tasks_for_user(user.username)
    task_01 = next(t for t in tasks if t.id == "task_01")
    task_01.reviewed = False

    session_id = rewardlab_store.authenticate(user.username, "test_password_06")
    assert session_id is not None

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        # Navigate to task detail with simulate_lost_response=true
        wf = WorkflowDefinition(
            name="case-e-lost-resp",
            provider="rewardlab",
            steps=[
                NavigateStep(
                    id="s1",
                    url=f"{rewardlab_server}/tasks/task_01?simulate_lost_response=true",
                ),
                ClickStep(
                    id="s2",
                    target=TestIdLocator(value="mark-reviewed-btn"),
                    side_effect=True,
                ),
            ],
        )

        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=session_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )

        # Mutation succeeded on server: task_01 is reviewed
        assert task_01.reviewed is True

        # Client execution failed due to lost response (504): no blind retry, requires review
        assert res.status == WorkflowExecutionStatus.FAILED
        assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
        assert res.can_retry is False
        assert res.requires_verification is True
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


# -----------------------------------------------------------------------------
# 3. Keyboard Side Effects
# -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p1_1_keyboard_enter_submission_treated_as_mutating(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Verifies that press_key with 'Enter' is treated as mutating and requires verification."""
    wf_unverified = WorkflowDefinition(
        name="wf-enter-unverified",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            PressKeyStep(
                id="s2",
                target=TestIdLocator(value="keyboard-input"),
                key="Enter",
            ),
        ],
    )

    res = await browser_mgr.execute_workflow(
        workflow=wf_unverified,
        account_id="acc_key",
        policy_evaluator=policy_evaluator,
        base_url=rewardlab_server,
    )
    assert res.status == WorkflowExecutionStatus.SUCCESS
    # Enter key without postcondition verification flags OUTCOME_UNCONFIRMED
    assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
    assert res.requires_verification is True


@pytest.mark.asyncio
async def test_p1_1_keyboard_tab_read_only_action_executed(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Verifies non-submission key (Tab) without side_effect=True is ACTION_EXECUTED."""
    wf_readonly = WorkflowDefinition(
        name="wf-tab-readonly",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            PressKeyStep(
                id="s2",
                target=TestIdLocator(value="keyboard-input"),
                key="Tab",
            ),
        ],
    )

    res = await browser_mgr.execute_workflow(
        workflow=wf_readonly,
        account_id="acc_key_tab",
        policy_evaluator=policy_evaluator,
        base_url=rewardlab_server,
    )
    assert res.status == WorkflowExecutionStatus.SUCCESS
    # Read-only action completes as ACTION_EXECUTED
    assert res.outcome_type == WorkflowOutcomeType.ACTION_EXECUTED
    assert res.requires_verification is False


@pytest.mark.asyncio
async def test_p1_1_keyboard_explicit_side_effect_requires_verification(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Verifies press_key with explicit side_effect=True requires postcondition."""
    wf_explicit = WorkflowDefinition(
        name="wf-key-side-effect",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            PressKeyStep(
                id="s2",
                target=TestIdLocator(value="keyboard-input"),
                key="Space",
                side_effect=True,
            ),
        ],
    )

    res = await browser_mgr.execute_workflow(
        workflow=wf_explicit,
        account_id="acc_key_explicit",
        policy_evaluator=policy_evaluator,
        base_url=rewardlab_server,
    )
    assert res.status == WorkflowExecutionStatus.SUCCESS
    # Explicit side_effect=True flags OUTCOME_UNCONFIRMED when unverified
    assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
    assert res.requires_verification is True
