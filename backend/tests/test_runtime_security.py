"""Runtime Security and Permission Engine verification suite (Milestone M2.4).

Verifies:
1. Deny-by-default for unauthorized providers before browser/context creation.
2. Step-level policy enforcement before action execution.
3. Capability checking and rejection of ungranted capabilities.
4. Route interception blocking redirects to foreign domains.
5. Post-click navigation security blocking links to domains outside allowlist.
6. Popup/new window suppression and auto-close guard.
7. Unsafe URL scheme rejection (javascript:, data:, file:, etc.).
8. Cloud metadata and SSRF destination blocking.
9. Arbitrary code / eval execution rejection.
10. Fail-closed diagnostic secret protection (screenshot suppression).
11. Side-effect retry safety (click/fill treated as state mutations).
12. Reliable context release and resource cleanup upon policy violation.
"""

import asyncio

import pytest
from playwright.async_api import Browser

from app.core.errors import PolicyDeniedException
from app.core.policy import PolicyEvaluator
from app.domain.policy import ProviderPolicy
from app.execution.browser_manager import BrowserManager
from app.execution.diagnostics import DiagnosticManager
from app.workflows.interpreter import WorkflowInterpreter
from app.workflows.results import WorkflowExecutionStatus
from app.workflows.schema import (
    ClickStep,
    FillStep,
    NavigateStep,
    TestIdLocator,
    VerifyStateStep,
    WorkflowDefinition,
)
from app.workflows.validator import validate_workflow_definition

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def browser_mgr():
    """Provides a healthy running BrowserManager for security testing."""
    mgr = BrowserManager(headless=True)
    await mgr.start()
    try:
        yield mgr
    finally:
        await mgr.stop()


@pytest.fixture
async def browser(browser_mgr: BrowserManager) -> Browser:
    """Provides a running Chromium Browser instance."""
    assert browser_mgr.browser is not None
    return browser_mgr.browser


# =============================================================================
# 1. Pre-Dispatch Policy Checks & Zero-Context Creation
# =============================================================================


async def test_denied_provider_zero_context_creation(browser_mgr: BrowserManager):
    """Verifies that an unauthorized provider is denied before browser context creation."""
    wf = WorkflowDefinition(
        id="denied_prov_wf",
        provider="microsoft_rewards",
        steps=[NavigateStep(id="s1", path="/home")],
    )

    initial_contexts = browser_mgr.active_contexts_count
    assert initial_contexts == 0

    result = await browser_mgr.execute_workflow(workflow=wf)

    assert result.status == WorkflowExecutionStatus.POLICY_DENIED
    assert result.error_code == "POLICY_DENIED"
    assert "Automation is prohibited" in (result.error_message or "")
    # Zero BrowserContexts created or leaked
    assert browser_mgr.active_contexts_count == 0


async def test_unknown_provider_denied_by_default(browser_mgr: BrowserManager):
    """Verifies unknown providers are denied by default."""
    wf = WorkflowDefinition(
        id="unknown_prov_wf",
        provider="unregistered_provider_xyz",
        steps=[NavigateStep(id="s1", path="/home")],
    )

    result = await browser_mgr.execute_workflow(workflow=wf)
    assert result.status == WorkflowExecutionStatus.POLICY_DENIED
    assert browser_mgr.active_contexts_count == 0


# =============================================================================
# 2. Capability Evaluation & Unauthorized Capability Blocking
# =============================================================================


def test_workflow_requesting_unauthorized_capability_rejected():
    """Workflows requesting capabilities not granted to the provider are rejected."""
    evaluator = PolicyEvaluator()
    wf = WorkflowDefinition(
        id="priv_esc_wf",
        provider="rewardlab",
        requested_capabilities=["read_tasks", "admin_bypass"],
        steps=[NavigateStep(id="s1", path="/tasks")],
    )

    with pytest.raises(PolicyDeniedException) as exc_info:
        validate_workflow_definition(wf, evaluator)

    assert "Requested capabilities ['admin_bypass'] are not granted" in str(exc_info.value)


async def test_workflow_with_granted_capabilities_allowed(browser_mgr: BrowserManager):
    """Workflows requesting only permitted capabilities validate successfully."""
    evaluator = PolicyEvaluator()
    wf = WorkflowDefinition(
        id="valid_cap_wf",
        provider="rewardlab",
        requested_capabilities=["read_tasks", "mark_reviewed"],
        steps=[NavigateStep(id="s1", url="http://127.0.0.1:8001/tasks")],
    )

    validated = validate_workflow_definition(wf, evaluator)
    assert validated.id == "valid_cap_wf"


# =============================================================================
# 3. URL Schemes & SSRF / Cloud Metadata Protection
# =============================================================================


@pytest.mark.parametrize(
    "prohibited_url, expected_code",
    [
        ("javascript:alert(document.cookie)", "UNSAFE_SCHEME"),
        ("data:text/html,<h1>Pwned</h1>", "UNSAFE_SCHEME"),
        ("file:///etc/passwd", "UNSAFE_SCHEME"),
        ("about:blank", "UNSAFE_SCHEME"),
        ("vbscript:MsgBox(1)", "UNSAFE_SCHEME"),
        ("blob:http://127.0.0.1/uuid", "UNSAFE_SCHEME"),
        ("http://user:pass@127.0.0.1:8001/login", "POLICY_DENIED"),
        ("http://169.254.169.254/latest/meta-data/", "SSRF_ATTEMPT_BLOCKED"),
        ("http://169.254.169.123/secrets", "SSRF_ATTEMPT_BLOCKED"),
        ("http://metadata.google.internal/computeMetadata/v1/", "SSRF_ATTEMPT_BLOCKED"),
    ],
)
def test_static_url_security_rules(prohibited_url: str, expected_code: str):
    """Tests static URL security validation against prohibited schemes and SSRF."""
    evaluator = PolicyEvaluator()
    is_valid, reason, _ = evaluator.validate_static_url(
        url=prohibited_url,
        domain_allowlist=["127.0.0.1", "localhost"],
        allowed_ports=[8001],
    )
    assert is_valid is False
    assert (
        "scheme" in reason.lower()
        or "metadata" in reason.lower()
        or "embedded credentials" in reason.lower()
        or "not in provider" in reason.lower()
    )


def test_api_verification_endpoint_ssrf_blocked():
    """VerifyStateStep cannot use foreign absolute URLs or path traversal."""
    evaluator = PolicyEvaluator()

    # 1. Foreign URL in endpoint
    is_valid, reason, _ = evaluator.validate_api_endpoint(
        base_url="http://127.0.0.1:8001",
        endpoint="https://attacker-host.com/exfiltrate",
        provider="rewardlab",
    )
    assert is_valid is False
    assert "relative path" in reason

    # 2. Path traversal in task_id is rejected by validator
    wf = WorkflowDefinition(
        id="ssrf_wf",
        provider="rewardlab",
        steps=[
            VerifyStateStep(
                id="v1",
                source="rewardlab_api",
                task_id="../../admin/keys",
                field="status",
                equals="ok",
            )
        ],
    )
    with pytest.raises(PolicyDeniedException) as exc_info:
        validate_workflow_definition(wf, evaluator)
    assert "path traversal" in str(exc_info.value)


# =============================================================================
# 4. Runtime Network & Redirect Security
# =============================================================================


async def test_redirect_to_unauthorized_domain_blocked(browser: Browser):
    """Route interception terminates executions attempting redirects to foreign domains."""
    context = await browser.new_context()
    page = await context.new_page()

    policy = ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist=["127.0.0.1", "localhost"],
        allowed_actions=["navigate"],
    )
    evaluator = PolicyEvaluator(policies={"rewardlab": policy})
    interpreter = WorkflowInterpreter(policy_evaluator=evaluator)

    async def mock_redirect_route(route):
        if "redirect-test" in route.request.url:
            await route.fulfill(
                status=302,
                headers={"Location": "https://malicious-exfiltrate.com/steal"},
            )
        else:
            await route.continue_()

    await page.route("**/redirect-test", mock_redirect_route)

    wf = WorkflowDefinition(
        id="redirect_wf",
        provider="rewardlab",
        steps=[NavigateStep(id="s1", url="http://127.0.0.1:8001/redirect-test")],
    )

    try:
        res = await interpreter.execute(workflow=wf, page=page)
        # Execution must fail and not proceed to foreign host
        assert res.status in (
            WorkflowExecutionStatus.POLICY_DENIED,
            WorkflowExecutionStatus.FAILED,
        )
        assert "malicious-exfiltrate.com" not in page.url
    finally:
        await page.unroute("**/redirect-test", mock_redirect_route)
        await context.close()


async def test_click_induced_navigation_out_of_allowlist_blocked(browser: Browser):
    """Clicks that navigate to unauthorized domains are blocked at runtime."""
    context = await browser.new_context()
    page = await context.new_page()

    await page.set_content("""
        <div>
            <button id="leak-btn" data-testid="leak-button"
                onclick="window.location.href='https://unauthorized-domain.com/landing'">
                Click to Exfiltrate
            </button>
        </div>
    """)

    policy = ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist=["127.0.0.1", "localhost"],
        allowed_actions=["click"],
    )
    evaluator = PolicyEvaluator(policies={"rewardlab": policy})
    interpreter = WorkflowInterpreter(policy_evaluator=evaluator)

    wf = WorkflowDefinition(
        id="click_nav_wf",
        provider="rewardlab",
        steps=[ClickStep(id="s1", target=TestIdLocator(value="leak-button"))],
    )

    try:
        res = await interpreter.execute(workflow=wf, page=page)
        assert res.status == WorkflowExecutionStatus.POLICY_DENIED
        assert res.error_code == "POLICY_DENIED"
        err_msg = (res.error_message or "").lower()
        assert (
            "unauthorized navigation" in err_msg
            or "unauthorized-domain.com" in err_msg
            or "chrome-error" in err_msg
        )
    finally:
        await context.close()


async def test_popup_blocked_and_closed_by_policy(browser: Browser):
    """Popups and new pages are auto-closed and suppressed when allow_popups is False."""
    context = await browser.new_context()
    page = await context.new_page()

    await page.set_content("""
        <div>
            <button id="pop-btn" data-testid="pop-button"
                onclick="window.open('https://pop-up-site.com')">
                Open Popup
            </button>
        </div>
    """)

    policy = ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist=["127.0.0.1", "localhost"],
        allowed_actions=["click"],
        allow_popups=False,
    )
    evaluator = PolicyEvaluator(policies={"rewardlab": policy})
    interpreter = WorkflowInterpreter(policy_evaluator=evaluator)

    wf = WorkflowDefinition(
        id="popup_wf",
        provider="rewardlab",
        steps=[ClickStep(id="s1", target=TestIdLocator(value="pop-button"))],
    )

    try:
        await interpreter.execute(workflow=wf, page=page)
        await asyncio.sleep(0.3)
        # Context should have only the original page; popup was closed by guard
        open_pages = [p for p in context.pages if not p.is_closed()]
        assert len(open_pages) == 1
    finally:
        await context.close()


# =============================================================================
# 5. Side-Effect & Mutation Safety
# =============================================================================


async def test_click_and_fill_actions_treated_as_mutations(browser_mgr: BrowserManager):
    """Even if workflow claims side_effect: false, click/fill failure prevents blind retry."""
    wf = WorkflowDefinition(
        id="mutate_wf",
        provider="rewardlab",
        steps=[
            # click declared with side_effect=False
            ClickStep(
                id="s_click",
                target=TestIdLocator(value="non-existent-button"),
                side_effect=False,
                timeout_seconds=1,
            )
        ],
    )

    result = await browser_mgr.execute_workflow(workflow=wf)

    assert result.status in (WorkflowExecutionStatus.FAILED, WorkflowExecutionStatus.TIMEOUT)
    # Safety invariant: mutating actions must set requires_verification=True and can_retry=False
    assert result.requires_verification is True
    assert result.can_retry is False


# =============================================================================
# 6. Diagnostic Secret Protection
# =============================================================================


async def test_sensitive_workflow_suppresses_failure_screenshot(
    browser_mgr: BrowserManager, tmp_path
):
    """Workflows containing passwords or tokens suppress screenshot capture on failure."""
    diag_mgr = DiagnosticManager(artifacts_dir=tmp_path, capture_screenshot=True)
    browser_mgr.diagnostic_manager = diag_mgr

    wf = WorkflowDefinition(
        id="sensitive_auth_wf",
        provider="rewardlab",
        steps=[
            FillStep(
                id="fill_pw",
                target=TestIdLocator(value="password-field"),
                value="super_secret_password_123",
                timeout_seconds=1,
            )
        ],
    )

    result = await browser_mgr.execute_workflow(workflow=wf)
    assert result.status in (WorkflowExecutionStatus.FAILED, WorkflowExecutionStatus.TIMEOUT)

    # Check diagnostic files
    created_files = list(tmp_path.iterdir())
    json_files = [f for f in created_files if f.suffix == ".json"]
    png_files = [f for f in created_files if f.suffix == ".png"]

    # Exactly 1 sanitized JSON diagnostic artifact, ZERO png screenshots!
    assert len(json_files) == 1
    assert len(png_files) == 0

    diag_content = json_files[0].read_text(encoding="utf-8")
    assert "super_secret_password_123" not in diag_content
    assert "Screenshot suppressed: workflow inputs contain sensitive credentials" in diag_content


# =============================================================================
# 7. Context Isolation & Cleanup Safety
# =============================================================================


async def test_runtime_policy_denial_releases_context_cleanly(browser_mgr: BrowserManager):
    """Runtime policy violation properly closes and frees BrowserContext."""
    wf = WorkflowDefinition(
        id="cleanup_after_denial_wf",
        provider="rewardlab",
        steps=[
            NavigateStep(
                id="nav_outside",
                url="http://disallowed-host.example.com/test",
            )
        ],
    )

    result = await browser_mgr.execute_workflow(workflow=wf)
    assert result.status == WorkflowExecutionStatus.POLICY_DENIED
    # Guaranteed cleanup: active context count must return to 0
    assert browser_mgr.active_contexts_count == 0
