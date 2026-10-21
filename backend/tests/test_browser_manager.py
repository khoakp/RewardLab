"""Tests for Milestone M2.3: Browser Manager & Lifecycle Reliability.

Verifies:
1. Single Chromium launch, reused browser process across executions.
2. Short-lived independent BrowserContexts per execution (zero cookie/localStorage sharing).
3. Guaranteed context release in finally block under success, failure, timeout, and cancellation.
4. Structured browser crash / disconnect handling (BROWSER_DISCONNECTED).
5. Side-effect safety: no unsafe retry after failure on actions with side_effect=True.
6. Trace-on-failure diagnostic artifacts: generated strictly on failure, secret redaction,
   fail-closed policy for native traces, and artifact retention pruning.
7. Policy denial does not leak resources.
"""

import asyncio
import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

# Load rewardlab app safely without colliding with backend 'app' package
rewardlab_root = Path(__file__).resolve().parents[2] / "rewardlab"
if str(rewardlab_root) not in sys.path:
    sys.path.insert(0, str(rewardlab_root))

_spec = importlib.util.spec_from_file_location("rewardlab_main_app_bm", rewardlab_root / "app.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["rewardlab_main_app_bm"] = _mod
_spec.loader.exec_module(_mod)

rewardlab_app = _mod.app

from app.core.policy import PolicyEvaluator, ProviderPolicy  # noqa: E402
from app.execution.browser_manager import BrowserManager  # noqa: E402
from app.execution.diagnostics import DiagnosticManager  # noqa: E402
from app.workflows.errors import BrowserDisconnectedError  # noqa: E402
from app.workflows.results import WorkflowExecutionStatus  # noqa: E402
from app.workflows.runner import run_workflow  # noqa: E402
from app.workflows.schema import (  # noqa: E402
    ClickStep,
    CssLocator,
    ExecutionLimits,
    FillStep,
    NavigateStep,
    RoleLocator,
    TestIdLocator,
    WaitVisibleStep,
    WorkflowDefinition,
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def rewardlab_server():
    """Runs RewardLab in a background thread on a free loopback port for the module."""
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

    # Wait for server ready
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
    yield base_url
    server.should_exit = True
    thread.join(timeout=3.0)


@pytest.fixture
def temp_diagnostics_dir(tmp_path: Path) -> Path:
    diag_dir = tmp_path / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    return diag_dir


@pytest.fixture
def permissive_evaluator(rewardlab_server: str) -> PolicyEvaluator:
    parsed_port = int(rewardlab_server.split(":")[-1])
    evaluator = PolicyEvaluator()
    evaluator.policies["rewardlab"] = ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist=["127.0.0.1", "localhost"],
        allowed_ports=[parsed_port],
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
    return evaluator


# =============================================================================
# 1. Browser Process Lifecycle & Reuse
# =============================================================================


@pytest.mark.asyncio
async def test_browser_manager_lifecycle_and_process_reuse():
    """Verifies that BrowserManager starts once and reuses the same Browser process."""
    bm = BrowserManager(headless=True)
    assert not bm.is_running
    assert bm.active_contexts_count == 0

    await bm.start()
    assert bm.is_running
    assert bm.is_healthy()
    browser_ref_1 = bm.browser
    assert browser_ref_1 is not None

    # Idempotent start() does not recreate browser
    await bm.start()
    assert bm.browser is browser_ref_1

    # Stop manager shuts down cleanly
    await bm.stop()
    assert not bm.is_running
    assert bm.browser is None
    assert bm.active_contexts_count == 0


@pytest.mark.asyncio
async def test_browser_manager_async_context_manager():
    """Verifies async with BrowserManager() lifecycle."""
    async with BrowserManager(headless=True) as bm:
        assert bm.is_running
        assert bm.is_healthy()
    assert not bm.is_running
    assert bm.browser is None


# =============================================================================
# 2. Independent Contexts & Zero Cross-Account State Contamination
# =============================================================================


@pytest.mark.asyncio
async def test_browser_manager_independent_contexts_isolation(
    rewardlab_server: str,
):
    """Verifies that two contexts under the same browser have zero shared
    cookies or localStorage.
    """
    async with BrowserManager(headless=True) as bm:
        # Context 1: Account test_01
        ctx1 = await bm.new_context(
            account_id="test_01",
            session_id="token_account_01",
            base_url=rewardlab_server,
        )
        assert bm.active_contexts_count == 1

        # Context 2: Account test_02
        ctx2 = await bm.new_context(
            account_id="test_02",
            session_id="token_account_02",
            base_url=rewardlab_server,
        )
        assert bm.active_contexts_count == 2

        # Check cookies in context 1
        cookies_1 = await ctx1.cookies()
        session_cookie_1 = next((c for c in cookies_1 if c["name"] == "rewardlab_session"), None)
        assert session_cookie_1 is not None
        assert session_cookie_1["value"] == "token_account_01"

        # Check cookies in context 2
        cookies_2 = await ctx2.cookies()
        session_cookie_2 = next((c for c in cookies_2 if c["name"] == "rewardlab_session"), None)
        assert session_cookie_2 is not None
        assert session_cookie_2["value"] == "token_account_02"

        # Mutate localStorage in page 1
        p1 = await ctx1.new_page()
        await p1.goto(f"{rewardlab_server}/login")
        await p1.evaluate("localStorage.setItem('user_key', 'account_01_secret')")

        # Check localStorage in page 2
        p2 = await ctx2.new_page()
        await p2.goto(f"{rewardlab_server}/login")
        val2 = await p2.evaluate("localStorage.getItem('user_key')")
        assert val2 is None, "Context 2 must not see localStorage from Context 1"

        # Close contexts
        await bm.close_context(ctx1)
        assert bm.active_contexts_count == 1
        await bm.close_context(ctx2)
        assert bm.active_contexts_count == 0


# =============================================================================
# 3. Resource Cleanup on Success, Failure, Timeout, and Cancellation
# =============================================================================


@pytest.mark.asyncio
async def test_workflow_success_releases_context(
    rewardlab_server: str,
    permissive_evaluator: PolicyEvaluator,
):
    """Verifies that a successful workflow execution cleanly closes and releases its context."""
    async with BrowserManager(headless=True) as bm:
        wf = WorkflowDefinition(
            id="wf_success_cleanup",
            schema_version=1,
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", action="navigate", url=f"{rewardlab_server}/login"),
                WaitVisibleStep(
                    id="s2",
                    action="wait_visible",
                    target=RoleLocator(role="button", name="Sign in"),
                ),
            ],
        )

        assert bm.active_contexts_count == 0
        res = await bm.execute_workflow(
            workflow=wf,
            base_url=rewardlab_server,
            account_id="test_01",
            policy_evaluator=permissive_evaluator,
        )

        assert res.status == WorkflowExecutionStatus.SUCCESS
        assert bm.active_contexts_count == 0, "Active context must be 0 after successful execution"


@pytest.mark.asyncio
async def test_workflow_failure_releases_context(
    rewardlab_server: str,
    permissive_evaluator: PolicyEvaluator,
    temp_diagnostics_dir: Path,
):
    """Verifies that a failing workflow releases its context and saves failure diagnostics."""
    diag_mgr = DiagnosticManager(artifacts_dir=temp_diagnostics_dir)
    async with BrowserManager(headless=True, diagnostic_manager=diag_mgr) as bm:
        wf = WorkflowDefinition(
            id="wf_failure_cleanup",
            schema_version=1,
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", action="navigate", url=f"{rewardlab_server}/login"),
                WaitVisibleStep(
                    id="s2",
                    action="wait_visible",
                    target=TestIdLocator(value="nonexistent-target-element"),
                    timeout_seconds=1.0,
                ),
            ],
        )

        res = await bm.execute_workflow(
            workflow=wf,
            base_url=rewardlab_server,
            account_id="test_01",
            policy_evaluator=permissive_evaluator,
            record_trace_on_failure=True,
        )

        assert res.status == WorkflowExecutionStatus.TIMEOUT
        assert res.error_code == "STEP_TIMEOUT"
        assert bm.active_contexts_count == 0, "Context must be released even after step failure"
        assert len(res.artifacts) > 0, "Diagnostic artifacts must be recorded on failure"

        # Check diagnostic file existence and contents
        diag_file = Path(res.artifacts[-1])
        assert diag_file.exists()
        with open(diag_file, encoding="utf-8") as f:
            data = json.load(f)
        assert data["workflow_id"] == "wf_failure_cleanup"
        assert data["status"] == "TIMEOUT"
        assert data["failed_step"]["step_id"] == "s2"


@pytest.mark.asyncio
async def test_context_lifecycle_timeout_releases_context(
    rewardlab_server: str,
    permissive_evaluator: PolicyEvaluator,
):
    """Verifies that context_timeout_seconds bounds execution and releases resources."""
    async with BrowserManager(headless=True) as bm:
        wf = WorkflowDefinition(
            id="wf_context_timeout",
            schema_version=1,
            provider="rewardlab",
            execution=ExecutionLimits(timeout_seconds=30, step_timeout_seconds=30),
            steps=[
                NavigateStep(id="s1", action="navigate", url=f"{rewardlab_server}/login"),
                WaitVisibleStep(
                    id="s2",
                    action="wait_visible",
                    target=TestIdLocator(value="never-visible-element"),
                    timeout_seconds=30.0,
                ),
            ],
        )

        # Enforce context lifecycle timeout of 1.5 seconds
        res = await bm.execute_workflow(
            workflow=wf,
            base_url=rewardlab_server,
            account_id="test_01",
            context_timeout_seconds=1.5,
            policy_evaluator=permissive_evaluator,
        )

        assert res.status == WorkflowExecutionStatus.TIMEOUT
        assert res.error_code == "CONTEXT_TIMEOUT"
        assert "Browser context lifecycle timed out" in (res.error_message or "")
        assert bm.active_contexts_count == 0


@pytest.mark.asyncio
async def test_asyncio_cancellation_releases_context(
    rewardlab_server: str,
    permissive_evaluator: PolicyEvaluator,
):
    """Verifies that cancelling the execute_workflow task cleans up the BrowserContext."""
    async with BrowserManager(headless=True) as bm:
        wf = WorkflowDefinition(
            id="wf_cancellation_cleanup",
            schema_version=1,
            provider="rewardlab",
            execution=ExecutionLimits(timeout_seconds=30),
            steps=[
                NavigateStep(id="s1", action="navigate", url=f"{rewardlab_server}/login"),
                WaitVisibleStep(
                    id="s2",
                    action="wait_visible",
                    target=TestIdLocator(value="long-running-wait"),
                    timeout_seconds=30.0,
                ),
            ],
        )

        task = asyncio.create_task(
            bm.execute_workflow(
                workflow=wf,
                base_url=rewardlab_server,
                account_id="test_01",
                policy_evaluator=permissive_evaluator,
            )
        )

        # Allow task to start and create context
        await asyncio.sleep(0.5)
        assert bm.active_contexts_count == 1, "Context should be active while task is executing"

        # Cancel execution
        task.cancel()
        res = await task

        assert res.status == WorkflowExecutionStatus.CANCELLED
        assert res.error_code == "EXECUTION_CANCELLED"
        assert bm.active_contexts_count == 0


# =============================================================================
# 4. Browser Disconnect & Crash Handling
# =============================================================================


@pytest.mark.asyncio
async def test_browser_disconnect_handling(
    rewardlab_server: str,
    permissive_evaluator: PolicyEvaluator,
):
    """Verifies structured error and status when browser unexpectedly closes or crashes."""
    bm = BrowserManager(headless=True)
    await bm.start()
    assert bm.is_healthy()

    # Simulate unexpected disconnect by closing the underlying browser process
    assert bm.browser is not None
    await bm.browser.close()

    # Verify disconnect detection
    assert not bm.is_healthy()
    assert bm.is_disconnected or not bm.is_running

    # Subsequent new_context must raise BrowserDisconnectedError
    with pytest.raises(BrowserDisconnectedError):
        await bm.new_context()

    # Subsequent execute_workflow returns structured BROWSER_DISCONNECTED failure
    wf = WorkflowDefinition(
        id="wf_post_disconnect",
        schema_version=1,
        provider="rewardlab",
        steps=[NavigateStep(id="s1", action="navigate", url=f"{rewardlab_server}/login")],
    )
    res = await bm.execute_workflow(
        workflow=wf,
        base_url=rewardlab_server,
        policy_evaluator=permissive_evaluator,
    )
    assert res.status == WorkflowExecutionStatus.FAILED
    assert res.error_code == "BROWSER_DISCONNECTED"
    assert res.can_retry is False

    await bm.stop()


# =============================================================================
# 5. Side-Effect Safety & No Blind Retry
# =============================================================================


@pytest.mark.asyncio
async def test_side_effect_failure_marks_requires_verification(
    rewardlab_server: str,
    permissive_evaluator: PolicyEvaluator,
):
    """Verifies that failures on actions with side_effect=True require manual verification."""
    async with BrowserManager(headless=True) as bm:
        wf = WorkflowDefinition(
            id="wf_side_effect_check",
            schema_version=1,
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", action="navigate", url=f"{rewardlab_server}/login"),
                # Failing click with side_effect=True
                ClickStep(
                    id="s2",
                    action="click",
                    target=TestIdLocator(value="nonexistent-review-button"),
                    side_effect=True,
                    timeout_seconds=1,
                ),
            ],
        )

        res = await bm.execute_workflow(
            workflow=wf,
            base_url=rewardlab_server,
            account_id="test_01",
            policy_evaluator=permissive_evaluator,
        )

        assert res.status == WorkflowExecutionStatus.TIMEOUT
        assert res.requires_verification is True
        assert res.can_retry is False


# =============================================================================
# 6. Trace-on-Failure & Secret Redaction
# =============================================================================


@pytest.mark.asyncio
async def test_diagnostic_artifact_redacts_credentials(
    rewardlab_server: str,
    permissive_evaluator: PolicyEvaluator,
    temp_diagnostics_dir: Path,
):
    """Verifies that diagnostic artifacts never leak passwords or sensitive tokens."""
    diag_mgr = DiagnosticManager(
        artifacts_dir=temp_diagnostics_dir,
        enable_playwright_trace=True,  # Test fail-closed trace policy
    )
    async with BrowserManager(headless=True, diagnostic_manager=diag_mgr) as bm:
        wf = WorkflowDefinition(
            id="wf_secret_redaction_test",
            schema_version=1,
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", action="navigate", url=f"{rewardlab_server}/login"),
                FillStep(
                    id="s2",
                    action="fill",
                    target=CssLocator(value='input[name="password"]'),
                    value="super_secret_cleartext_password",
                ),
                # Deliberate failure on step 3
                WaitVisibleStep(
                    id="s3",
                    action="wait_visible",
                    target=TestIdLocator(value="nonexistent-submit"),
                    timeout_seconds=1,
                ),
            ],
        )

        res = await bm.execute_workflow(
            workflow=wf,
            base_url=rewardlab_server,
            account_id="test_01",
            policy_evaluator=permissive_evaluator,
            record_trace_on_failure=True,
        )

        assert res.status == WorkflowExecutionStatus.TIMEOUT
        assert len(res.artifacts) > 0

        # Read JSON diagnostic artifact
        diag_path = next(p for p in res.artifacts if p.endswith("_diagnostic.json"))
        with open(diag_path, encoding="utf-8") as f:
            raw_text = f.read()

        # Invariant: Secret password MUST NOT be present in plain text in artifact
        assert "super_secret_cleartext_password" not in raw_text

        # Invariant: Native Playwright trace was suppressed due to sensitive password input
        parsed_diag = json.loads(raw_text)
        assert "suppressed" in parsed_diag["trace_policy"]["trace_suppressed_reason"].lower()


# =============================================================================
# 7. Diagnostic Manager Artifact Retention & Pruning
# =============================================================================


def test_diagnostic_manager_retention_pruning(tmp_path: Path):
    """Verifies that DiagnosticManager prunes oldest files when limit is exceeded."""
    diag_dir = tmp_path / "prune_test"
    diag_dir.mkdir(parents=True, exist_ok=True)
    mgr = DiagnosticManager(artifacts_dir=diag_dir, max_artifacts=3)

    # Create 5 dummy diagnostic files with distinct mtimes
    created_files = []
    for i in range(5):
        f = diag_dir / f"test_artifact_{i}.json"
        f.write_text(json.dumps({"index": i}))
        created_files.append(f)
        time.sleep(0.01)

    # Prune artifacts beyond limit of 3
    deleted = mgr.prune_artifacts()
    assert deleted == 2

    remaining = list(diag_dir.iterdir())
    assert len(remaining) == 3
    remaining_names = {r.name for r in remaining}
    # Oldest files (0 and 1) should have been pruned
    assert "test_artifact_0.json" not in remaining_names
    assert "test_artifact_1.json" not in remaining_names
    assert "test_artifact_4.json" in remaining_names


# =============================================================================
# 8. Policy Denial Does Not Leak Resources
# =============================================================================


@pytest.mark.asyncio
async def test_policy_denial_does_not_leak_context():
    """Verifies that an unauthorized provider execution is denied immediately
    without leaking contexts.
    """
    async with BrowserManager(headless=True) as bm:
        wf = WorkflowDefinition(
            id="wf_denied_provider",
            schema_version=1,
            provider="microsoft_rewards",  # Automation prohibited
            steps=[NavigateStep(id="s1", action="navigate", url="https://rewards.bing.com")],
        )

        res = await bm.execute_workflow(
            workflow=wf,
            account_id="test_01",
        )

        assert res.status == WorkflowExecutionStatus.POLICY_DENIED
        assert res.error_code == "POLICY_DENIED"
        assert bm.active_contexts_count == 0, "No context should remain after policy denial"


# =============================================================================
# 9. Runner Integration: run_workflow with BrowserManager
# =============================================================================


@pytest.mark.asyncio
async def test_run_workflow_with_browser_manager(
    rewardlab_server: str,
    permissive_evaluator: PolicyEvaluator,
):
    """Verifies that runner.run_workflow seamlessly supports BrowserManager."""
    async with BrowserManager(headless=True) as bm:
        wf = WorkflowDefinition(
            id="wf_runner_bm",
            schema_version=1,
            provider="rewardlab",
            steps=[
                NavigateStep(id="s1", action="navigate", url=f"{rewardlab_server}/login"),
                WaitVisibleStep(
                    id="s2",
                    action="wait_visible",
                    target=RoleLocator(role="button", name="Sign in"),
                ),
            ],
        )

        res = await run_workflow(
            workflow=wf,
            browser=bm,
            base_url=rewardlab_server,
            account_id="test_01",
            policy_evaluator=permissive_evaluator,
        )

        assert res.status == WorkflowExecutionStatus.SUCCESS
        assert bm.active_contexts_count == 0
