"""Acceptance and regression tests for Milestone Personal P2:
Causal Verification Hardening & Safe Operator Console.

Covers:
Phase A:
- Case A: verify A -> mutate A -> end => OUTCOME_UNCONFIRMED
- Case B: mutate A -> verify A -> mutate B -> end => OUTCOME_UNCONFIRMED
- Case C: mutate A -> verify A -> end => STATE_VERIFIED
- Case D: mutate A -> verify A -> mutate B -> verify B => STATE_VERIFIED
- Case E: mutate A -> mutate B -> verify B -> verify B => OUTCOME_UNCONFIRMED (FIFO fix)
- Case F: mutate A -> verify B (task scope mismatch) => schema validation error
- Case G: verify referencing non-existent step => schema validation error
- Case H: verify referencing future step => schema validation error
- Case I: verify referencing non-mutating step => schema validation error
- Case J: mutation response lost (504) => OUTCOME_UNCONFIRMED, can_retry=False
- Case K: legacy workflow with single mutation & verification => STATE_VERIFIED
- Case L: legacy workflow with ambiguous multiple mutations => OUTCOME_UNCONFIRMED
- Legacy form compatibility: fill -> fill -> click -> verify => STATE_VERIFIED
- Failed verification does not relieve pending mutations

Phase B:
- Operator CLI jobs list (text & JSON)
- Operator CLI jobs show (text & JSON, redacted)
- Operator CLI jobs review cancel
- Operator CLI jobs review reject
- Direct transition to SUCCEEDED is rejected
- Reviewing non-paused job is rejected
- Repeated review on terminal job is rejected
- Concurrent review attempts handling
- Account safety hold: paused job prevents claiming next queued job
- Multiple paused jobs on same account keep hold active until all resolved
- Cross-account isolation: account A hold does not affect account B

Phase C:
- Workflow inspector execution plan (zero browser launch)
- Workflow inspector policy check for unauthorized provider
- Workflow inspector flags unverified mutations
- DiagnosticManager path traversal rejection (.. sequences and outside absolute paths)
- DiagnosticManager safe reading with credential redaction
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.policy import PolicyEvaluator, ProviderPolicy
from app.execution.browser_manager import BrowserManager
from app.execution.diagnostics import DiagnosticManager
from app.operator_cli import (
    build_jobs_parser,
    execute_jobs_async,
    inspect_workflow_plan,
)
from app.persistence.models import (
    Base,
    JobStatus,
    WorkflowApprovalStatus,
)
from app.persistence.repository import (
    AccountRepository,
    JobRepository,
    ProviderRepository,
    WorkflowVersionRepository,
)
from app.workflows.results import (
    StepStatus,
    WorkflowExecutionStatus,
    WorkflowOutcomeType,
)
from app.workflows.schema import (
    ClickStep,
    FillStep,
    NavigateStep,
    TestIdLocator,
    VerifyStateStep,
    WorkflowDefinition,
)

# -----------------------------------------------------------------------------
# RewardLab Test Server Setup
# -----------------------------------------------------------------------------
rewardlab_root = Path(__file__).resolve().parents[2] / "rewardlab"
if str(rewardlab_root) not in sys.path:
    sys.path.insert(0, str(rewardlab_root))

_spec = importlib.util.spec_from_file_location("rewardlab_main_app_p2", rewardlab_root / "app.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["rewardlab_main_app_p2"] = _mod
_spec.loader.exec_module(_mod)

rewardlab_app = _mod.app
rewardlab_data = importlib.import_module("data")
rewardlab_store = rewardlab_data.store


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
    import time

    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("RewardLab test server failed to start within timeout.")

    base_url = f"http://127.0.0.1:{port}"
    yield base_url
    server.should_exit = True
    thread.join(timeout=2.0)


@pytest.fixture
def policy_evaluator(rewardlab_server: str) -> PolicyEvaluator:
    domain = rewardlab_server.split("://")[1].split(":")[0]
    port = int(rewardlab_server.split(":")[-1])
    test_policy = ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist={domain, "127.0.0.1", "localhost"},
        allowed_ports={port},
        allow_popups=False,
        max_duration_seconds=30,
        allowed_actions={
            "navigate",
            "wait_visible",
            "extract_text",
            "click",
            "fill",
            "hover",
            "scroll",
            "press_key",
            "wait_for_state",
            "assert_visible",
            "verify_state",
            "notify_user",
            "request_human_action",
        },
    )
    return PolicyEvaluator(policies={"rewardlab": test_policy})


@pytest.fixture
async def browser_mgr() -> Any:
    mgr = BrowserManager(headless=True)
    await mgr.start()
    yield mgr
    await mgr.stop()


@pytest.fixture
async def sqlite_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sqlite_session_factory(sqlite_engine):
    return async_sessionmaker(
        bind=sqlite_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


# =============================================================================
# PHASE A: Causal Verification Acceptance Tests (Cases A - L)
# =============================================================================


@pytest.mark.asyncio
async def test_case_a_verify_before_mutate_yields_unconfirmed(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case A: verify A -> mutate A -> end => OUTCOME_UNCONFIRMED."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_01"]
    task = next(t for t in rewardlab_store.get_tasks_for_user(user.username) if t.id == "task_01")
    task.reviewed = False
    sess_id = rewardlab_store.authenticate(user.username, "test_password_01")

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-a-verify-before-mutate",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                VerifyStateStep(id="s2", task_id="task_01", field="reviewed", equals=False),
                ClickStep(id="s3", target=TestIdLocator(value="mark-reviewed-btn")),
            ],
        )
        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=sess_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.SUCCESS
        assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
        assert res.requires_verification is True
        assert "s3" in res.unverified_mutations
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_case_b_mutate_verify_mutate_yields_unconfirmed(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case B: mutate A -> verify A -> mutate B -> end => OUTCOME_UNCONFIRMED."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_02"]
    sess_id = rewardlab_store.authenticate(user.username, "test_password_02")

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-b-trailing-mutation",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
                VerifyStateStep(
                    id="s3",
                    task_id="task_01",
                    field="reviewed",
                    equals=True,
                    verifies_step_ids=["s2"],
                ),
                NavigateStep(id="s4", url=f"{rewardlab_server}/tasks/task_02"),
                ClickStep(id="s5", target=TestIdLocator(value="mark-reviewed-btn")),
            ],
        )
        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=sess_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.SUCCESS
        assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
        assert res.requires_verification is True
        assert res.unverified_mutations == ["s5"]
        assert "s2" in res.verified_mutations
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_case_c_mutate_verify_yields_state_verified(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case C: mutate A -> verify A -> end => STATE_VERIFIED."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_03"]
    sess_id = rewardlab_store.authenticate(user.username, "test_password_03")

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-c-verified",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
                VerifyStateStep(
                    id="s3",
                    task_id="task_01",
                    field="reviewed",
                    equals=True,
                    verifies_step_ids=["s2"],
                ),
            ],
        )
        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=sess_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.SUCCESS
        assert res.outcome_type == WorkflowOutcomeType.STATE_VERIFIED
        assert res.requires_verification is False
        assert len(res.unverified_mutations) == 0
        assert "s2" in res.verified_mutations
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_case_d_two_mutations_two_verifications_yields_state_verified(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case D: mutate A -> verify A -> mutate B -> verify B => STATE_VERIFIED."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_04"]
    sess_id = rewardlab_store.authenticate(user.username, "test_password_04")

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-d-both-verified",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
                VerifyStateStep(
                    id="s3",
                    task_id="task_01",
                    field="reviewed",
                    equals=True,
                    verifies_step_ids=["s2"],
                ),
                NavigateStep(id="s4", url=f"{rewardlab_server}/tasks/task_02"),
                ClickStep(id="s5", target=TestIdLocator(value="mark-reviewed-btn")),
                VerifyStateStep(
                    id="s6",
                    task_id="task_02",
                    field="reviewed",
                    equals=True,
                    verifies_step_ids=["s5"],
                ),
            ],
        )
        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=sess_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.SUCCESS
        assert res.outcome_type == WorkflowOutcomeType.STATE_VERIFIED
        assert res.requires_verification is False
        assert len(res.unverified_mutations) == 0
        assert "s2" in res.verified_mutations
        assert "s5" in res.verified_mutations
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_case_e_duplicate_verification_does_not_relieve_other_mutation(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case E (The FIFO Root Cause):
    mutate A -> mutate B -> verify B -> verify B (duplicate)
    In P1.1 naive FIFO pop, the second verify B would pop mutate A!
    In P2 explicit mapping, duplicate verify B cannot relieve mutate A.
    Must yield OUTCOME_UNCONFIRMED with 's2' in unverified_mutations!
    """
    rewardlab_store.reset()
    user = rewardlab_store.users["test_05"]
    sess_id = rewardlab_store.authenticate(user.username, "test_password_05")

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-e-duplicate-verify-b",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),  # mutate A
                NavigateStep(id="s3", url=f"{rewardlab_server}/tasks/task_02"),
                ClickStep(id="s4", target=TestIdLocator(value="mark-reviewed-btn")),  # mutate B
                VerifyStateStep(
                    id="s5",
                    task_id="task_02",
                    field="reviewed",
                    equals=True,
                    verifies_step_ids=["s4"],  # verify B #1
                ),
                VerifyStateStep(
                    id="s6",
                    task_id="task_02",
                    field="reviewed",
                    equals=True,
                    verifies_step_ids=["s4"],  # verify B #2 (duplicate!)
                ),
            ],
        )
        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=sess_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        # Outcome MUST be OUTCOME_UNCONFIRMED because mutate A ('s2') is unverified!
        assert res.status == WorkflowExecutionStatus.SUCCESS
        assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
        assert res.requires_verification is True
        assert res.unverified_mutations == ["s2"]
        assert "s4" in res.verified_mutations
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


def test_case_f_task_scope_mismatch_raises_validation_error():
    """Case F: mutate A -> verify B with explicit task mismatch raises ValueError."""
    with pytest.raises(ValueError, match="task scope mismatch"):
        WorkflowDefinition(
            name="case-f-task-mismatch",
            provider="rewardlab",
            steps=[
                ClickStep(id="s1", target=TestIdLocator(value="btn"), task_id="task_01"),
                VerifyStateStep(
                    id="s2",
                    task_id="task_02",  # Mismatch!
                    field="reviewed",
                    equals=True,
                    verifies_step_ids=["s1"],
                ),
            ],
        )


def test_case_g_non_existent_step_reference_raises_validation_error():
    """Case G: verifies_step_ids references non-existent step."""
    with pytest.raises(ValueError, match="references non-existent step"):
        WorkflowDefinition(
            name="case-g-missing-step",
            provider="rewardlab",
            steps=[
                ClickStep(id="s1", target=TestIdLocator(value="btn")),
                VerifyStateStep(
                    id="s2",
                    field="reviewed",
                    equals=True,
                    verifies_step_ids=["ghost_step"],
                ),
            ],
        )


def test_case_h_future_step_reference_raises_validation_error():
    """Case H: verifies_step_ids references future step appearing after verification."""
    with pytest.raises(ValueError, match="does not appear before it"):
        WorkflowDefinition(
            name="case-h-future-step",
            provider="rewardlab",
            steps=[
                VerifyStateStep(
                    id="s1",
                    field="reviewed",
                    equals=True,
                    verifies_step_ids=["s2"],  # Appears after!
                ),
                ClickStep(id="s2", target=TestIdLocator(value="btn")),
            ],
        )


def test_case_i_non_mutating_step_reference_raises_validation_error():
    """Case I: verifies_step_ids references read-only step (e.g. navigate)."""
    with pytest.raises(ValueError, match="not a mutating step"):
        WorkflowDefinition(
            name="case-i-non-mutating",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url="http://127.0.0.1:8001/tasks/task_01"),
                VerifyStateStep(
                    id="s2",
                    field="reviewed",
                    equals=True,
                    verifies_step_ids=["s1"],  # s1 is navigate!
                ),
            ],
        )


@pytest.mark.asyncio
async def test_case_j_lost_response_504_fails_unconfirmed_without_retry(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case J: Mutating step receives HTTP 504 -> OUTCOME_UNCONFIRMED, can_retry=False."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_01"]
    sess_id = rewardlab_store.authenticate(user.username, "test_password_01")

    wf = WorkflowDefinition(
        name="case-j-504",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01?error=504"),
            ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
        ],
    )
    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id=user.username,
        session_id=sess_id,
        policy_evaluator=policy_evaluator,
        base_url=rewardlab_server,
    )
    assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
    assert res.can_retry is False
    assert res.requires_verification is True


@pytest.mark.asyncio
async def test_case_k_legacy_workflow_single_mutation_retained_compatibility(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case K: Legacy workflow without verifies_step_ids having exactly one mutation."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_01"]
    sess_id = rewardlab_store.authenticate(user.username, "test_password_01")

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-k-legacy-single",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
                # Legacy: verifies_step_ids is None
                VerifyStateStep(id="s3", task_id="task_01", field="reviewed", equals=True),
            ],
        )
        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=sess_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.SUCCESS
        assert res.outcome_type == WorkflowOutcomeType.STATE_VERIFIED
        assert res.requires_verification is False
        assert len(res.unverified_mutations) == 0
        assert "s2" in res.verified_mutations
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_case_l_legacy_workflow_ambiguous_mutations_fails_closed(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Case L: Legacy workflow with ambiguous mutations fails-closed to OUTCOME_UNCONFIRMED."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_02"]
    sess_id = rewardlab_store.authenticate(user.username, "test_password_02")

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="case-l-legacy-ambiguous",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
                NavigateStep(id="s3", url=f"{rewardlab_server}/tasks/task_02"),
                ClickStep(id="s4", target=TestIdLocator(value="mark-reviewed-btn")),
                # Legacy verify with 2 ambiguous prior clicks on different tasks
                VerifyStateStep(id="s5", task_id="task_02", field="reviewed", equals=True),
            ],
        )
        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=sess_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.SUCCESS
        # Must fail-closed: not all mutations could be unambiguously attributed
        assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
        assert res.requires_verification is True
        assert len(res.unverified_mutations) > 0
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_legacy_form_fill_click_compatibility(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Verifies that a cohesive form submission sequence (fill* -> click -> verify)
    remains backward-compatible without explicit step IDs.
    """
    rewardlab_store.reset()
    user = rewardlab_store.users["test_01"]
    sess_id = rewardlab_store.authenticate(user.username, "test_password_01")

    wf = WorkflowDefinition(
        name="legacy-form-fill-click",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            FillStep(id="s2", target=TestIdLocator(value="keyboard-input"), value="sample"),
            ClickStep(id="s3", target=TestIdLocator(value="submit-input-btn")),
            VerifyStateStep(
                id="s4",
                source="dom",
                field="text",
                equals="sample",
                target=TestIdLocator(value="input-result"),
            ),
        ],
    )
    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id=user.username,
        session_id=sess_id,
        policy_evaluator=policy_evaluator,
        base_url=rewardlab_server,
    )
    assert res.status == WorkflowExecutionStatus.SUCCESS
    assert res.outcome_type == WorkflowOutcomeType.STATE_VERIFIED
    assert len(res.unverified_mutations) == 0


@pytest.mark.asyncio
async def test_failed_verification_does_not_relieve_mutation(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """If verify_state fails its assertion, pending mutations are NOT relieved."""
    rewardlab_store.reset()
    user = rewardlab_store.users["test_01"]
    sess_id = rewardlab_store.authenticate(user.username, "test_password_01")

    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(True)
        wf = WorkflowDefinition(
            name="failed-verification",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
                VerifyStateStep(
                    id="s3",
                    task_id="task_01",
                    field="reviewed",
                    equals="NON_EXISTENT_VALUE",  # Assertion failure!
                    verifies_step_ids=["s2"],
                ),
            ],
        )
        res = await browser_mgr.execute_workflow(
            workflow=wf,
            account_id=user.username,
            session_id=sess_id,
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res.status == WorkflowExecutionStatus.FAILED
        assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
        assert "s2" in res.unverified_mutations
        assert len(res.verified_mutations) == 0
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


# =============================================================================
# PHASE B: Safe Operator Console Tests
# =============================================================================


@pytest.fixture
async def setup_db_provider(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        prov_repo = ProviderRepository(session)
        await prov_repo.create_or_update(
            provider_id="rewardlab",
            adapter_kind="rewardlab",
            policy_version=1,
        )
        await session.commit()


@pytest.mark.asyncio
async def test_operator_list_paused_jobs_json(sqlite_session_factory, setup_db_provider):
    """Tests `adbap jobs list --status PAUSED_FOR_REVIEW --json`."""
    async with sqlite_session_factory() as session:
        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            account_id="acc_01", provider_id="rewardlab", owner_id="op1", label="Account 1"
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            "wf_key", 1, "rewardlab", {"steps": []}, approval_status=WorkflowApprovalStatus.APPROVED
        )
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf.id, "acc_01", "idem_list_01")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        job.error_code = "SIDE_EFFECT_AMBIGUOUS"
        await session.commit()
        job_id = job.id

    parser = build_jobs_parser()
    args = parser.parse_args(["list", "--status", "PAUSED_FOR_REVIEW", "--json"])

    from contextlib import asynccontextmanager
    from unittest.mock import patch

    @asynccontextmanager
    async def mock_db():
        async with sqlite_session_factory() as s:
            yield s

    with patch("app.operator_cli.get_db_session", mock_db):
        code, out = await execute_jobs_async(args)

    assert code == 0
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    matching = [j for j in parsed if j["job_id"] == job_id]
    assert len(matching) == 1
    assert matching[0]["status"] == "PAUSED_FOR_REVIEW"
    assert matching[0]["is_paused_for_review"] is True


@pytest.mark.asyncio
async def test_operator_show_job_redacted(sqlite_session_factory, setup_db_provider):
    """Tests `adbap jobs show <job_id>` with secret redaction and JSON output."""
    async with sqlite_session_factory() as session:
        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            account_id="acc_02", provider_id="rewardlab", owner_id="op1", label="Account 2"
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            "wf_show",
            1,
            "rewardlab",
            {"steps": []},
            approval_status=WorkflowApprovalStatus.APPROVED,
        )
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf.id, "acc_02", "idem_show_01")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        job.error_code = "HUMAN_ACTION_REQUIRED"
        job.error_message = "operator review required for secret password=secret_token_123"
        await job_repo.record_step_result(
            job_id=job.id,
            step_key="s1",
            attempt=1,
            status=StepStatus.SUCCESS,
            details={"token": "raw_token_xyz", "safe_val": 42},
        )
        await session.commit()
        job_id = job.id

    parser = build_jobs_parser()
    args = parser.parse_args(["show", job_id, "--json"])

    from contextlib import asynccontextmanager
    from unittest.mock import patch

    @asynccontextmanager
    async def mock_db():
        async with sqlite_session_factory() as s:
            yield s

    with patch("app.operator_cli.get_db_session", mock_db):
        code, out = await execute_jobs_async(args)

    assert code == 0
    detail = json.loads(out)
    assert detail["job_id"] == job_id
    assert detail["status"] == "PAUSED_FOR_REVIEW"
    assert len(detail["steps"]) == 1
    # Secrets in step details MUST be redacted
    step_details = detail["steps"][0]["details"]
    assert step_details["token"] == "[REDACTED]"
    assert step_details["safe_val"] == 42


@pytest.mark.asyncio
async def test_operator_review_cancel_decision(sqlite_session_factory, setup_db_provider):
    """Tests operator review cancelling a paused job."""
    async with sqlite_session_factory() as session:
        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            account_id="acc_03", provider_id="rewardlab", owner_id="op1", label="Account 3"
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            "wf_rev", 1, "rewardlab", {"steps": []}, approval_status=WorkflowApprovalStatus.APPROVED
        )
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf.id, "acc_03", "idem_rev_01")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        await session.commit()
        job_id = job.id

    parser = build_jobs_parser()
    args = parser.parse_args(
        [
            "review",
            job_id,
            "--decision",
            "cancel",
            "--reason",
            "Aborted duplicate execution",
            "--json",
        ]
    )

    from contextlib import asynccontextmanager
    from unittest.mock import patch

    @asynccontextmanager
    async def mock_db():
        async with sqlite_session_factory() as s:
            yield s

    with patch("app.operator_cli.get_db_session", mock_db):
        code, out = await execute_jobs_async(args)

    assert code == 0
    res = json.loads(out)
    assert res["status"] == "CANCELLED"
    assert res["decision"] == "cancel"

    # Verify persistent state and audit log in database
    async with sqlite_session_factory() as session:
        j_db = await JobRepository(session).get_by_id(job_id)
        assert j_db.status == JobStatus.CANCELLED.value
        assert "CANCEL" in j_db.error_code


@pytest.mark.asyncio
async def test_operator_review_reject_decision(sqlite_session_factory, setup_db_provider):
    """Tests operator review rejecting a paused job."""
    async with sqlite_session_factory() as session:
        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            account_id="acc_04", provider_id="rewardlab", owner_id="op1", label="Account 4"
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            "wf_rej", 1, "rewardlab", {"steps": []}, approval_status=WorkflowApprovalStatus.APPROVED
        )
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf.id, "acc_04", "idem_rej_01")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        await session.commit()
        job_id = job.id

    parser = build_jobs_parser()
    args = parser.parse_args(
        [
            "review",
            job_id,
            "--decision",
            "reject",
            "--reason",
            "Rejected due to policy non-compliance",
            "--json",
        ]
    )

    from contextlib import asynccontextmanager
    from unittest.mock import patch

    @asynccontextmanager
    async def mock_db():
        async with sqlite_session_factory() as s:
            yield s

    with patch("app.operator_cli.get_db_session", mock_db):
        code, out = await execute_jobs_async(args)

    assert code == 0
    res = json.loads(out)
    assert res["status"] == "REJECTED"


@pytest.mark.asyncio
async def test_operator_review_cannot_transition_to_succeeded(sqlite_session_factory):
    """Direct transition from PAUSED_FOR_REVIEW to SUCCEEDED is strictly prohibited."""
    parser = build_jobs_parser()
    with pytest.raises(SystemExit):
        # Argument parser choices restrict decision to cancel/reject
        parser.parse_args(["review", "job_1", "--decision", "succeed", "--reason", "test"])


@pytest.mark.asyncio
async def test_review_on_non_paused_job_fails(sqlite_session_factory, setup_db_provider):
    """Attempting to review a job in QUEUED or RUNNING status raises an error."""
    async with sqlite_session_factory() as session:
        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            account_id="acc_05", provider_id="rewardlab", owner_id="op1", label="Acc 5"
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            "wf_queued",
            1,
            "rewardlab",
            {"steps": []},
            approval_status=WorkflowApprovalStatus.APPROVED,
        )
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf.id, "acc_05", "idem_queued_01")
        job.status = JobStatus.QUEUED.value
        await session.commit()
        job_id = job.id

    parser = build_jobs_parser()
    args = parser.parse_args(
        ["review", job_id, "--decision", "cancel", "--reason", "test", "--json"]
    )

    from contextlib import asynccontextmanager
    from unittest.mock import patch

    @asynccontextmanager
    async def mock_db():
        async with sqlite_session_factory() as s:
            yield s

    with patch("app.operator_cli.get_db_session", mock_db):
        code, out = await execute_jobs_async(args)

    assert code == 1
    res = json.loads(out)
    assert res["error"] == "REVIEW_REJECTED"
    assert "Only paused jobs can be reviewed" in res["message"]


@pytest.mark.asyncio
async def test_repeated_review_on_terminal_job_fails(sqlite_session_factory, setup_db_provider):
    """Reviewing an already cancelled or rejected job fails due to terminal state immutability."""
    async with sqlite_session_factory() as session:
        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            account_id="acc_06", provider_id="rewardlab", owner_id="op1", label="Acc 6"
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            "wf_term",
            1,
            "rewardlab",
            {"steps": []},
            approval_status=WorkflowApprovalStatus.APPROVED,
        )
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf.id, "acc_06", "idem_term_01")
        job.status = JobStatus.CANCELLED.value
        await session.commit()
        job_id = job.id

    parser = build_jobs_parser()
    args = parser.parse_args(
        ["review", job_id, "--decision", "reject", "--reason", "again", "--json"]
    )

    from contextlib import asynccontextmanager
    from unittest.mock import patch

    @asynccontextmanager
    async def mock_db():
        async with sqlite_session_factory() as s:
            yield s

    with patch("app.operator_cli.get_db_session", mock_db):
        code, out = await execute_jobs_async(args)

    assert code == 1
    res = json.loads(out)
    assert res["error"] == "REVIEW_REJECTED"


# =============================================================================
# Account Safety Hold & Isolation Tests
# =============================================================================


@pytest.mark.asyncio
async def test_account_safety_hold_and_isolation(sqlite_session_factory, setup_db_provider):
    """Verifies:
    1. A paused job on Account A prevents claiming QUEUED jobs on Account A.
    2. Account B is NOT blocked by Account A's paused job (cross-account isolation).
    3. Multiple paused jobs on Account A: reviewing one keeps hold active.
    4. Only when ALL paused jobs on Account A are resolved can Account A resume claiming.
    """
    async with sqlite_session_factory() as session:
        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            account_id="acc_a", provider_id="rewardlab", owner_id="op1", label="Acc A"
        )
        await acc_repo.create_account(
            account_id="acc_b", provider_id="rewardlab", owner_id="op1", label="Acc B"
        )

        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            "wf_hold",
            1,
            "rewardlab",
            {"steps": []},
            approval_status=WorkflowApprovalStatus.APPROVED,
        )
        job_repo = JobRepository(session)

        # Acc A: Job 1 (PAUSED), Job 2 (PAUSED), Job 3 (QUEUED)
        j1 = await job_repo.create_job(wf.id, "acc_a", "idem_a_1")
        j1.status = JobStatus.PAUSED_FOR_REVIEW.value

        j2 = await job_repo.create_job(wf.id, "acc_a", "idem_a_2")
        j2.status = JobStatus.PAUSED_FOR_REVIEW.value

        j3 = await job_repo.create_job(wf.id, "acc_a", "idem_a_3")
        j3.status = JobStatus.QUEUED.value

        # Acc B: Job B1 (QUEUED)
        jb = await job_repo.create_job(wf.id, "acc_b", "idem_b_1")
        jb.status = JobStatus.QUEUED.value

        await session.commit()

        # Step 1: Claim for worker. Acc A must be held, but Acc B must be claimable!
        claimed_b = await job_repo.claim_next_job(worker_id="worker_1", provider_id="rewardlab")
        assert claimed_b is not None
        assert claimed_b.account_id == "acc_b"

        # Acc A still has paused jobs; claim again should return None
        claimed_none = await job_repo.claim_next_job(worker_id="worker_1", provider_id="rewardlab")
        assert claimed_none is None

        # Step 2: Resolve Job 1 on Acc A (cancel it)
        j1.status = JobStatus.CANCELLED.value
        await session.commit()

        # Job 2 is STILL paused on Acc A! Hold must remain active.
        claimed_still_none = await job_repo.claim_next_job(
            worker_id="worker_1", provider_id="rewardlab"
        )
        assert claimed_still_none is None

        # Step 3: Resolve Job 2 on Acc A (reject it)
        j2.status = JobStatus.REJECTED.value
        await session.commit()

        # Step 4: All paused jobs resolved on Acc A! Now Job 3 must be successfully claimed.
        claimed_a3 = await job_repo.claim_next_job(worker_id="worker_1", provider_id="rewardlab")
        assert claimed_a3 is not None
        assert claimed_a3.id == j3.id
        assert claimed_a3.account_id == "acc_a"
        assert claimed_a3.status == JobStatus.LEASED.value


# =============================================================================
# PHASE C: Workflow Inspector & Diagnostics Tests
# =============================================================================


def test_workflow_inspector_execution_plan(tmp_path: Path):
    """Verifies `inspect-workflow` produces execution plan and mapping with zero browser launch."""
    wf_yaml = """
schema_version: 1
id: test_inspect_plan
name: Inspect Plan
provider: rewardlab
execution:
  timeout_seconds: 30
steps:
  - id: s1
    action: navigate
    url: http://127.0.0.1:8001/tasks/task_01
  - id: s2
    action: click
    target:
      by: test_id
      value: mark-btn
  - id: s3
    action: verify_state
    field: reviewed
    equals: true
    verifies_step_ids:
      - s2
"""
    f = tmp_path / "plan.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["workflow_id"] == "test_inspect_plan"
    assert report["total_steps"] == 3
    assert report["mutating_step_count"] == 1
    assert report["has_unverified_mutations"] is False
    assert report["unverified_mutations"] == []
    assert report["policy_evaluation"]["automation_allowed"] is True


def test_workflow_inspector_flags_unverified_mutations(tmp_path: Path):
    """Inspector warns when a workflow has mutating steps missing postconditions."""
    wf_yaml = """
schema_version: 1
id: test_missing_postcondition
provider: rewardlab
steps:
  - id: s1
    action: navigate
    url: http://127.0.0.1:8001/tasks/task_01
  - id: s2
    action: click
    target:
      by: test_id
      value: submit-btn
"""
    f = tmp_path / "missing_post.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["has_unverified_mutations"] is True
    assert "s2" in report["unverified_mutations"]


def test_workflow_inspector_denies_unknown_provider(tmp_path: Path):
    """Inspector detects unauthorized provider statically without starting any browser."""
    wf_yaml = """
schema_version: 1
id: test_unauthorized_provider
provider: microsoft_rewards
steps:
  - id: s1
    action: navigate
    url: https://rewards.bing.com
"""
    f = tmp_path / "bad_provider.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["policy_evaluation"]["automation_allowed"] is False
    assert "companion-only" in report["policy_evaluation"]["policy_reason"]


def test_diagnostic_manager_rejects_path_traversal(tmp_path: Path):
    """Verifies DiagnosticManager strictly rejects path traversal sequences."""
    diag_dir = tmp_path / "var" / "diagnostics"
    mgr = DiagnosticManager(artifacts_dir=diag_dir)

    # 1. Reject .. traversal
    with pytest.raises(ValueError, match="Path traversal"):
        mgr.resolve_diagnostic_path("../../etc/passwd")

    with pytest.raises(ValueError, match="Path traversal"):
        mgr.resolve_diagnostic_path("../other_dir/diag.json")

    # 2. Reject absolute path outside artifacts_dir
    with pytest.raises(ValueError, match="Path traversal detected"):
        outside = tmp_path / "outside.json"
        mgr.resolve_diagnostic_path(outside)


def test_diagnostic_manager_reads_and_redacts_json(tmp_path: Path):
    """Verifies reading a diagnostic JSON artifact correctly redacts sensitive data."""
    diag_dir = tmp_path / "var" / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    mgr = DiagnosticManager(artifacts_dir=diag_dir)

    sample_diag = {
        "execution_id": "test_exec_01",
        "password": "super_secret_password",
        "nested": {"token": "secret_token_val", "public_info": "safe"},
    }
    diag_file = diag_dir / "diag_test_01_diagnostic.json"
    diag_file.write_text(json.dumps(sample_diag), encoding="utf-8")

    data = mgr.read_diagnostic_artifact("diag_test_01_diagnostic.json")
    assert data["execution_id"] == "test_exec_01"
    assert data["password"] == "[REDACTED]"
    assert data["nested"]["token"] == "[REDACTED]"
    assert data["nested"]["public_info"] == "safe"
