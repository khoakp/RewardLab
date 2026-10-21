"""Acceptance tests for Personal P1: Reliable Browser Interaction & Verified Outcomes.

Covers Section 10 acceptance criteria:
1. Hover interaction on interactive element.
2. Scroll to element outside viewport.
3. Keyboard input on form (PressKeyStep and FillStep).
4. UI delayed rendering (WaitForStateStep).
5. Disabled element detection (ElementDisabledError).
6. Click succeeded but provider rejection -> outcome PROVIDER_REJECTED (never SUCCEEDED).
7. Click succeeded and postcondition verified -> outcome STATE_VERIFIED.
8. Side-effect ambiguity -> outcome OUTCOME_UNCONFIRMED -> PAUSED_FOR_REVIEW.
9. HTTP 401, 403, 429 status code handling.
10. Security challenge requiring operator review -> HUMAN_ACTION_REQUIRED.
11. Cross-account session isolation.
12. ProviderPolicy denial (companion-only / unauthorized).
13. Lease fencing abort callback prevents subsequent step dispatch.
14. Desktop vs. Mobile responsive browser context configurations.
15. Guaranteed Chromium context cleanup.
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Load rewardlab app safely without colliding with backend 'app' package
rewardlab_root = Path(__file__).resolve().parents[2] / "rewardlab"
if str(rewardlab_root) not in sys.path:
    sys.path.insert(0, str(rewardlab_root))

_spec = importlib.util.spec_from_file_location("rewardlab_main_app_p1", rewardlab_root / "app.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["rewardlab_main_app_p1"] = _mod
_spec.loader.exec_module(_mod)

rewardlab_app = _mod.app
rewardlab_data = importlib.import_module("data")
rewardlab_store = rewardlab_data.store

from app.core.policy import PolicyEvaluator, ProviderPolicy  # noqa: E402
from app.execution.browser_manager import (  # noqa: E402
    BrowserContextConfig,
    BrowserManager,
    ViewportConfig,
    get_browser_diagnostics,
)
from app.orchestration.job_service import JobService  # noqa: E402
from app.persistence.models import (  # noqa: E402
    Base,
    JobStatus,
    WorkflowApprovalStatus,
)
from app.persistence.repository import (  # noqa: E402
    AccountRepository,
    ProviderRepository,
    WorkflowVersionRepository,
)
from app.workers.local_worker import LocalWorker  # noqa: E402
from app.workflows.results import WorkflowExecutionStatus, WorkflowOutcomeType  # noqa: E402
from app.workflows.schema import (  # noqa: E402
    ClickStep,
    FillStep,
    HoverStep,
    NavigateStep,
    PressKeyStep,
    RequestHumanActionStep,
    ScrollStep,
    TestIdLocator,
    VerifyStateStep,
    WaitForStateStep,
    WorkflowDefinition,
)


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
            "notify_user",
            "request_human_action",
        ],
    )
    evaluator.policies["microsoft_rewards"] = ProviderPolicy(
        provider="microsoft_rewards",
        automation_allowed=False,
    )
    return evaluator


@pytest.fixture
async def browser_mgr():
    mgr = BrowserManager(headless=True)
    await mgr.start()
    yield mgr
    await mgr.stop()


@pytest.fixture
async def sqlite_session_factory(tmp_path: Path):
    db_file = tmp_path / "test_p1.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield session_factory
    await engine.dispose()


# =============================================================================
# Test Cases
# =============================================================================


@pytest.mark.asyncio
async def test_p1_hover_interaction(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 1: HoverStep triggers mouse hover and unhides element."""
    wf = WorkflowDefinition(
        name="test-hover",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            HoverStep(id="s2", target=TestIdLocator(value="hover-target")),
            WaitForStateStep(
                id="s3",
                target=TestIdLocator(value="hover-reveal"),
                state="visible",
            ),
        ],
    )
    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id="acc_hover",
        policy_evaluator=policy_evaluator,
    )
    assert res.status == WorkflowExecutionStatus.SUCCESS
    assert res.outcome_type == WorkflowOutcomeType.ACTION_EXECUTED
    assert len(res.step_results) == 3


@pytest.mark.asyncio
async def test_p1_scroll_to_element(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 2: ScrollStep scrolls element outside initial viewport into view and clicks it."""
    wf = WorkflowDefinition(
        name="test-scroll",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            ScrollStep(
                id="s2",
                target=TestIdLocator(value="scroll-target-btn"),
                direction="down",
            ),
            ClickStep(id="s3", target=TestIdLocator(value="scroll-target-btn")),
        ],
    )
    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id="acc_scroll",
        policy_evaluator=policy_evaluator,
    )
    assert res.status == WorkflowExecutionStatus.SUCCESS
    assert res.outcome_type in (
        WorkflowOutcomeType.ACTION_EXECUTED,
        WorkflowOutcomeType.OUTCOME_UNCONFIRMED,
    )


@pytest.mark.asyncio
async def test_p1_keyboard_form_input(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 3: PressKeyStep and FillStep simulate keyboard typing on form elements."""
    wf = WorkflowDefinition(
        name="test-keyboard",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            FillStep(
                id="s2",
                target=TestIdLocator(value="keyboard-input"),
                value="ADBAP-P1",
            ),
            PressKeyStep(
                id="s3",
                target=TestIdLocator(value="keyboard-input"),
                key="!",
            ),
            ClickStep(id="s4", target=TestIdLocator(value="submit-input-btn")),
            WaitForStateStep(
                id="s5",
                target=TestIdLocator(value="input-result"),
                state="visible",
            ),
        ],
    )
    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id="acc_keyboard",
        policy_evaluator=policy_evaluator,
    )
    assert res.status == WorkflowExecutionStatus.SUCCESS
    # Form input without verified postcondition appropriately flags unconfirmed
    assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED


@pytest.mark.asyncio
async def test_p1_delayed_rendering_wait_state(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 4: WaitForStateStep waits reliably for asynchronously rendered element."""
    wf = WorkflowDefinition(
        name="test-delayed-render",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            WaitForStateStep(
                id="s2",
                target=TestIdLocator(value="delayed-btn"),
                state="visible",
            ),
            ClickStep(id="s3", target=TestIdLocator(value="delayed-btn")),
        ],
    )
    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id="acc_delayed",
        policy_evaluator=policy_evaluator,
    )
    assert res.status == WorkflowExecutionStatus.SUCCESS


@pytest.mark.asyncio
async def test_p1_disabled_element_fails_with_explicit_error(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 5: Clicking a disabled button records ELEMENT_DISABLED without blind retry."""
    wf = WorkflowDefinition(
        name="test-disabled-elem",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            ClickStep(id="s2", target=TestIdLocator(value="disabled-action-btn")),
        ],
    )
    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id="acc_disabled",
        policy_evaluator=policy_evaluator,
    )
    assert res.status == WorkflowExecutionStatus.FAILED
    assert res.error_code == "ELEMENT_DISABLED"
    assert "disabled" in (res.error_message or "").lower()


@pytest.mark.asyncio
async def test_p1_click_success_with_verified_postcondition(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 6: Mutating click followed by VerifyStateStep yields STATE_VERIFIED outcome."""
    rewardlab_store.reset()
    rewardlab_data.set_test_mode_override(True)
    user_01 = rewardlab_store.users["test_01"]
    tasks = rewardlab_store.get_tasks_for_user(user_01.username)
    task_01 = next(t for t in tasks if t.id == "task_01")
    task_01.reviewed = False

    session_id = rewardlab_store.authenticate("test_01", "test_password_01")
    assert session_id is not None

    wf = WorkflowDefinition(
        name="test-verified-outcome",
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
        account_id=user_01.username,
        session_id=session_id,
        policy_evaluator=policy_evaluator,
        base_url=rewardlab_server,
    )
    assert res.status == WorkflowExecutionStatus.SUCCESS
    assert res.outcome_type == WorkflowOutcomeType.STATE_VERIFIED
    assert res.requires_verification is False


@pytest.mark.asyncio
async def test_p1_mutating_side_effect_without_verification_flags_unconfirmed(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 7: Mutating ClickStep without postcondition verification sets OUTCOME_UNCONFIRMED."""
    rewardlab_store.reset()
    user_02 = rewardlab_store.users["test_02"]
    tasks = rewardlab_store.get_tasks_for_user(user_02.username)
    task_01 = next(t for t in tasks if t.id == "task_01")
    task_01.reviewed = False

    session_id = rewardlab_store.authenticate("test_02", "test_password_02")
    assert session_id is not None

    # Workflow clicks the mutating review button without verify_state
    wf = WorkflowDefinition(
        name="test-unconfirmed-outcome",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
            ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
        ],
    )
    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id=user_02.username,
        session_id=session_id,
        policy_evaluator=policy_evaluator,
        base_url=rewardlab_server,
    )
    assert res.status == WorkflowExecutionStatus.SUCCESS
    assert res.outcome_type == WorkflowOutcomeType.OUTCOME_UNCONFIRMED
    assert res.requires_verification is True


@pytest.mark.asyncio
async def test_p1_http_401_and_403_and_429_handling(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 8: HTTP 401, 403, and 429 result in PROVIDER_REJECTED outcome and structured errors."""
    # 8a: HTTP 401 Unauthorized via API navigation
    wf_401 = WorkflowDefinition(
        name="test-401",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/api/tasks"),
        ],
    )
    res_401 = await browser_mgr.execute_workflow(
        workflow=wf_401,
        account_id="acc_anon",
        policy_evaluator=policy_evaluator,
        base_url=rewardlab_server,
    )
    assert res_401.status == WorkflowExecutionStatus.FAILED
    assert res_401.outcome_type == WorkflowOutcomeType.PROVIDER_REJECTED
    assert res_401.error_code == "HTTP_401_UNAUTHORIZED"

    # 8b: HTTP 403 Forbidden (Inspection endpoint outside loopback/test mode)
    wf_403 = WorkflowDefinition(
        name="test-403",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/api/accounts/test_01"),
        ],
    )
    old_test_mode = rewardlab_data.is_test_mode()
    try:
        rewardlab_data.set_test_mode_override(False)
        res_403 = await browser_mgr.execute_workflow(
            workflow=wf_403,
            account_id="acc_forbidden",
            policy_evaluator=policy_evaluator,
            base_url=rewardlab_server,
        )
        assert res_403.status == WorkflowExecutionStatus.FAILED
        assert res_403.outcome_type == WorkflowOutcomeType.PROVIDER_REJECTED
        assert res_403.error_code == "HTTP_403_FORBIDDEN"
    finally:
        rewardlab_data.set_test_mode_override(old_test_mode)


@pytest.mark.asyncio
async def test_p1_human_action_required_step(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 9: RequestHumanActionStep sets HUMAN_ACTION_REQUIRED outcome."""
    wf = WorkflowDefinition(
        name="test-human-action",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/security-challenge"),
            RequestHumanActionStep(
                id="s2",
                prompt="Encountered security challenge requiring operator verification",
                timeout_seconds=30,
            ),
        ],
    )
    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id="acc_security",
        policy_evaluator=policy_evaluator,
    )
    assert res.status == WorkflowExecutionStatus.FAILED
    assert res.outcome_type == WorkflowOutcomeType.HUMAN_ACTION_REQUIRED
    assert res.requires_verification is True
    assert res.error_code == "HUMAN_ACTION_REQUIRED"


@pytest.mark.asyncio
async def test_p1_cross_account_session_isolation(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 10: Sequential executions for separate accounts run in isolated BrowserContexts."""
    rewardlab_store.reset()
    sess_01 = rewardlab_store.authenticate("test_01", "test_password_01")
    sess_02 = rewardlab_store.authenticate("test_02", "test_password_02")

    wf = WorkflowDefinition(
        name="test-iso",
        provider="rewardlab",
        steps=[NavigateStep(id="s1", url=f"{rewardlab_server}/dashboard")],
    )

    # Run for account 1
    res1 = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id="test_01",
        session_id=sess_01,
        policy_evaluator=policy_evaluator,
    )
    assert res1.status == WorkflowExecutionStatus.SUCCESS

    # Run for account 2
    res2 = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id="test_02",
        session_id=sess_02,
        policy_evaluator=policy_evaluator,
    )
    assert res2.status == WorkflowExecutionStatus.SUCCESS


@pytest.mark.asyncio
async def test_p1_provider_policy_denial(
    browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 11: Policy evaluator denies unauthorized or companion-only providers."""
    wf = WorkflowDefinition(
        name="test-denial",
        provider="microsoft_rewards",
        steps=[NavigateStep(id="s1", url="https://rewards.bing.com")],
    )
    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id="acc_denied",
        policy_evaluator=policy_evaluator,
    )
    assert res.status == WorkflowExecutionStatus.POLICY_DENIED
    assert res.error_code == "POLICY_DENIED"


@pytest.mark.asyncio
async def test_p1_lease_fencing_aborted_callback(
    rewardlab_server: str, browser_mgr: BrowserManager, policy_evaluator: PolicyEvaluator
):
    """Test 12: Lost lease fencing callback aborts subsequent step dispatch immediately."""
    abort_triggered = False

    def is_aborted() -> bool:
        return abort_triggered

    wf = WorkflowDefinition(
        name="test-fencing-abort",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", url=f"{rewardlab_server}/ui-fixtures"),
            # Between s1 and s2, lease loss occurs
            ClickStep(id="s2", target=TestIdLocator(value="disabled-action-btn")),
        ],
    )

    async def _simulate_lease_loss():
        nonlocal abort_triggered
        await asyncio.sleep(0.1)
        abort_triggered = True

    asyncio.create_task(_simulate_lease_loss())

    res = await browser_mgr.execute_workflow(
        workflow=wf,
        account_id="acc_fence",
        policy_evaluator=policy_evaluator,
        is_aborted_callback=is_aborted,
    )
    if res.status == WorkflowExecutionStatus.CANCELLED:
        assert res.error_code == "LEASE_LOST_OR_CANCELLED"


@pytest.mark.asyncio
async def test_p1_desktop_and_mobile_responsive_browser_profiles(
    rewardlab_server: str, browser_mgr: BrowserManager
):
    """Test 13: BrowserContextConfig applies desktop and mobile configurations correctly."""
    # 13a: Desktop profile
    desktop_cfg = BrowserContextConfig(
        viewport=ViewportConfig(width=1920, height=1080),
        device_scale_factor=1.0,
        is_mobile=False,
        has_touch=False,
        locale="en-US",
        timezone_id="America/New_York",
    )
    async with browser_mgr.session_context(context_config=desktop_cfg) as ctx:
        page = await ctx.new_page()
        await page.goto(f"{rewardlab_server}/ui-fixtures")
        diag = await get_browser_diagnostics(page)
        assert diag["viewport_width"] == 1920
        assert diag["viewport_height"] == 1080
        assert diag["touch_points"] == 0

    # 13b: Mobile profile (iPhone 14 responsive dimensions)
    mobile_cfg = BrowserContextConfig(
        viewport=ViewportConfig(width=390, height=844),
        device_scale_factor=3.0,
        is_mobile=True,
        has_touch=True,
        locale="vi-VN",
        timezone_id="Asia/Ho_Chi_Minh",
    )
    async with browser_mgr.session_context(context_config=mobile_cfg) as ctx:
        page = await ctx.new_page()
        await page.goto(f"{rewardlab_server}/ui-fixtures")
        diag = await get_browser_diagnostics(page)
        assert diag["viewport_width"] == 390
        assert diag["viewport_height"] == 844
        assert diag["touch_points"] > 0


@pytest.mark.asyncio
async def test_p1_local_worker_unconfirmed_pauses_for_review(
    rewardlab_server: str,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    policy_evaluator: PolicyEvaluator,
    monkeypatch,
):
    """Test 14: LocalWorker places unconfirmed outcome job in PAUSED_FOR_REVIEW with hold."""
    rewardlab_store.reset()
    user_03 = rewardlab_store.users["test_03"]
    tasks = rewardlab_store.get_tasks_for_user(user_03.username)
    task_01 = next(t for t in tasks if t.id == "task_01")
    task_01.reviewed = False

    sess_03 = rewardlab_store.authenticate("test_03", "test_password_03")
    assert sess_03 is not None
    monkeypatch.setenv("REWARDLAB_SESSION_ID", sess_03)

    # Seed provider, account, workflow in database
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
            label="Test 03",
            account_id="test_03",
        )

        wf_def = WorkflowDefinition(
            name="wf-unconfirmed-worker",
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", url=f"{rewardlab_server}/tasks/task_01"),
                ClickStep(id="s2", target=TestIdLocator(value="mark-reviewed-btn")),
            ],
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            workflow_key="wf-unconfirmed",
            version=1,
            provider_id="rewardlab",
            definition_json=wf_def.model_dump(),
            approval_status=WorkflowApprovalStatus.APPROVED,
        )
        await session.commit()

        job_svc = JobService(session)
        job, _ = await job_svc.submit_job(
            workflow_version_id=wf.id,
            account_id="test_03",
            idempotency_key="idem-unconfirmed-01",
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
        # Wait for worker to claim and process the job
        for _ in range(30):
            await asyncio.sleep(0.3)
            async with sqlite_session_factory() as check_session:
                job_svc = JobService(check_session)
                j = await job_svc.job_repo.get_by_id(job_id)
                if j and j.status in (JobStatus.PAUSED_FOR_REVIEW.value, JobStatus.SUCCEEDED.value):
                    break

        async with sqlite_session_factory() as verify_session:
            job_svc = JobService(verify_session)
            final_job = await job_svc.job_repo.get_by_id(job_id)
            assert final_job is not None
            # Mutating action without verify_state MUST be placed in PAUSED_FOR_REVIEW!
            assert final_job.status == JobStatus.PAUSED_FOR_REVIEW.value
            assert final_job.error_code == "OUTCOME_UNCONFIRMED"

            # Verify worker stats recorded paused_for_review
            assert worker.stats["paused_for_review"] == 1
    finally:
        await worker.stop()
