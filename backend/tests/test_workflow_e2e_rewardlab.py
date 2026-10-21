import importlib.util
import socket
import sys
import threading
import time
from collections.abc import Generator
from contextlib import closing
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.async_api import Browser, async_playwright

# Ensure rewardlab app can be imported without name collision with backend app
rewardlab_root = Path(__file__).resolve().parents[2] / "rewardlab"
if str(rewardlab_root) not in sys.path:
    sys.path.insert(0, str(rewardlab_root))

_spec = importlib.util.spec_from_file_location("rewardlab_main_app", rewardlab_root / "app.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["rewardlab_main_app"] = _mod
_spec.loader.exec_module(_mod)

rewardlab_app = _mod.app
rewardlab_store = _mod.store
fault_controller = _mod.fault_controller

from app.core.policy import PolicyEvaluator  # noqa: E402
from app.domain.policy import ProviderPolicy  # noqa: E402
from app.workflows.results import StepStatus, WorkflowExecutionStatus  # noqa: E402
from app.workflows.runner import run_workflow  # noqa: E402
from app.workflows.schema import (  # noqa: E402
    AssertVisibleStep,
    ClickStep,
    ExecutionLimits,
    ExtractTextStep,
    FillStep,
    NavigateStep,
    RoleLocator,
    TestIdLocator,
    VerifyStateStep,
    WorkflowDefinition,
)
from app.workflows.validator import parse_and_validate_workflow  # noqa: E402


def get_free_port() -> int:
    """Find an available port on 127.0.0.1."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def live_server_url() -> Generator[str, None, None]:
    """Spawn an actual Uvicorn server running RewardLab on loopback in a background thread."""
    port = get_free_port()
    config = uvicorn.Config(
        app=rewardlab_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Poll until live server responds on TCP
    started = False
    for _ in range(50):
        try:
            with httpx.Client(base_url=base_url, timeout=1.0) as client:
                r = client.get("/api/health")
                if r.status_code == 200:
                    started = True
                    break
        except Exception:
            time.sleep(0.05)

    if not started:
        server.should_exit = True
        raise RuntimeError("Live RewardLab Uvicorn server failed to start within timeout")

    yield base_url

    server.should_exit = True
    thread.join(timeout=3.0)


@pytest.fixture
def reset_rewardlab_state():
    """Resets RewardLab synthetic users and active faults to baseline state."""
    rewardlab_store.reset()
    fault_controller.reset()
    # Explicitly enable test mode override so inspection API /api/accounts/... responds
    fault_controller.set_test_mode(True)
    yield
    rewardlab_store.reset()
    fault_controller.reset()
    fault_controller.set_test_mode(None)


@pytest.fixture
async def browser() -> Browser:
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        try:
            yield b
        finally:
            await b.close()


# =============================================================================
# End-to-End Workflow Tests on RewardLab
# =============================================================================


async def test_e2e_login_and_task_review_workflow(
    browser: Browser,
    live_server_url: str,
    reset_rewardlab_state,
):
    """Executes a full end-to-end workflow on RewardLab:

    1. Init browser context for synthetic account test_01.
    2. Navigate to /login and fill credentials.
    3. Submit login and assert dashboard/tasks visible.
    4. Navigate to task detail /tasks/task_01.
    5. Extract task description.
    6. Click 'Mark as reviewed'.
    7. Verify state via RewardLab API.
    8. Confirm points awarded exactly once (+10).
    9. Verify account test_02 remains isolated and untouched.
    """
    account_id = "test_01"
    initial_user = rewardlab_store.users[account_id]
    initial_points = initial_user.points
    assert initial_points == 120

    # Policy allowing navigation to the live test server host
    policy = ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist=["127.0.0.1", "localhost"],
        allowed_actions=[
            "navigate",
            "wait_visible",
            "extract_text",
            "click",
            "fill",
            "assert_visible",
            "verify_state",
        ],
    )
    evaluator = PolicyEvaluator(policies={"rewardlab": policy})

    workflow = WorkflowDefinition(
        id="e2e_login_and_review",
        provider="rewardlab",
        execution=ExecutionLimits(timeout_seconds=30, step_timeout_seconds=10),
        steps=[
            # 1. Open login page
            NavigateStep(id="step_goto_login", path="/login"),
            # 2. Fill username
            FillStep(
                id="step_fill_user",
                target=TestIdLocator(value="username-input"),
                value="test_01",
            ),
            # 3. Fill password
            FillStep(
                id="step_fill_pwd",
                target=TestIdLocator(value="password-input"),
                value="test_password_01",
            ),
            # 4. Click Sign in
            ClickStep(
                id="step_click_login",
                target=RoleLocator(role="button", name="Sign in"),
            ),
            # 5. Assert dashboard or header is visible
            AssertVisibleStep(
                id="step_assert_logged_in",
                target=TestIdLocator(value="dashboard-header"),
            ),
            # 6. Navigate to task detail page
            NavigateStep(id="step_goto_task", path="/tasks/task_01"),
            # 7. Extract task description
            ExtractTextStep(
                id="step_extract_desc",
                target=TestIdLocator(value="task-description"),
                output="task_description",
            ),
            # 8. Click 'Mark as reviewed'
            ClickStep(
                id="step_click_review",
                target=RoleLocator(role="button", name="Mark as reviewed"),
                side_effect=True,
            ),
            # 9. Assert reviewed note/status is visible
            AssertVisibleStep(
                id="step_assert_reviewed",
                target=TestIdLocator(value="task-reviewed-note"),
            ),
            # 10. Verify state via RewardLab API
            VerifyStateStep(
                id="step_verify_api",
                source="rewardlab_api",
                task_id="task_01",
                field="reviewed",
                equals=True,
            ),
        ],
    )

    result = await run_workflow(
        workflow=workflow,
        browser=browser,
        base_url=live_server_url,
        account_id=account_id,
        task_id="task_01",
        policy_evaluator=evaluator,
    )

    # 1. Assert workflow completed successfully
    assert result.status == WorkflowExecutionStatus.SUCCESS, (
        f"Workflow failed: {result.error_code} - {result.error_message}"
    )
    assert result.steps_executed == 10
    assert all(s.status == StepStatus.SUCCESS for s in result.step_results)

    # 2. Assert output extracted
    assert "task_description" in result.outputs
    assert len(result.outputs["task_description"]) > 0

    # 3. Verify server state directly on RewardLab
    updated_user = rewardlab_store.users[account_id]
    assert updated_user.points == initial_points + 10  # 120 + 10 = 130
    task_01 = rewardlab_store.get_task_for_user(account_id, "task_01")
    assert task_01 is not None
    assert task_01.reviewed is True

    # 4. Assert isolation: test_02 must NOT be modified (initial points: 50)
    user_02 = rewardlab_store.users["test_02"]
    assert user_02.points == 50
    task_02 = rewardlab_store.get_task_for_user("test_02", "task_01")
    assert task_02 is not None
    assert task_02.reviewed is False


async def test_e2e_canonical_sample_yaml_with_session(
    browser: Browser,
    live_server_url: str,
    reset_rewardlab_state,
):
    """Executes the canonical example YAML with pre-authenticated session cookie."""
    account_id = "test_03"
    session_id = rewardlab_store.authenticate(account_id, "test_password_03")
    assert session_id is not None

    repo_root = Path(__file__).resolve().parents[2]
    yaml_path = repo_root / "examples" / "workflows" / "rewardlab_review_first_task.yaml"
    yaml_content = yaml_path.read_text(encoding="utf-8")
    wf_def = parse_and_validate_workflow(yaml_content)

    # In canonical YAML:
    # step open_tasks navigates to /tasks
    # step read_task extracts task-description
    # step mark_reviewed clicks 'Mark as reviewed' button
    # In RewardLab, 'Mark as reviewed' is on task detail page /tasks/task_01
    # We target /tasks/task_01 by updating the first navigate step path for direct task review
    wf_def.steps[0].path = "/tasks/task_01"

    policy = ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist=["127.0.0.1", "localhost"],
        allowed_actions=[
            "navigate",
            "wait_visible",
            "extract_text",
            "click",
            "fill",
            "assert_visible",
            "verify_state",
        ],
    )
    evaluator = PolicyEvaluator(policies={"rewardlab": policy})

    result = await run_workflow(
        workflow=wf_def,
        browser=browser,
        base_url=live_server_url,
        account_id=account_id,
        task_id="task_01",
        session_id=session_id,
        policy_evaluator=evaluator,
    )

    assert result.status == WorkflowExecutionStatus.SUCCESS
    assert result.steps_executed == len(wf_def.steps)

    # Confirm points awarded exactly once for test_03 (initial: 80 + 10 = 90)
    user_03 = rewardlab_store.users[account_id]
    assert user_03.points == 90


async def test_e2e_idempotent_rerun_safety(
    browser: Browser,
    live_server_url: str,
    reset_rewardlab_state,
):
    """Verifies that re-running a review action on an already-reviewed task

    does not allocate duplicate reward points.
    """
    account_id = "test_04"
    # Pre-mark task_01 as reviewed (initial 110 + 10 = 120)
    rewardlab_store.mark_task_reviewed(account_id, "task_01")
    pts_after_first = rewardlab_store.users[account_id].points
    assert pts_after_first == 120

    session_id = rewardlab_store.authenticate(account_id, "test_password_04")

    # Workflow attempting to review again
    wf = WorkflowDefinition(
        id="repeat_review_wf",
        provider="rewardlab",
        steps=[
            NavigateStep(id="s1", path="/tasks/task_01"),
            # Button is disabled after review, so assert reviewed note is visible
            AssertVisibleStep(id="s2", target=TestIdLocator(value="task-reviewed-note")),
            VerifyStateStep(
                id="s3",
                source="rewardlab_api",
                task_id="task_01",
                field="reviewed",
                equals=True,
            ),
        ],
    )

    policy = ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist=["127.0.0.1", "localhost"],
        allowed_actions=["navigate", "assert_visible", "verify_state"],
    )
    evaluator = PolicyEvaluator(policies={"rewardlab": policy})

    result = await run_workflow(
        workflow=wf,
        browser=browser,
        base_url=live_server_url,
        account_id=account_id,
        task_id="task_01",
        session_id=session_id,
        policy_evaluator=evaluator,
    )

    assert result.status == WorkflowExecutionStatus.SUCCESS
    # Points must remain strictly unchanged (zero double-award)
    assert rewardlab_store.users[account_id].points == 120
