import asyncio
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from playwright.async_api import Browser, Page, async_playwright

from app.core.policy import PolicyEvaluator
from app.domain.policy import ProviderPolicy
from app.workflows.interpreter import (
    ExecutionContext,
    WorkflowInterpreter,
)
from app.workflows.locator import resolve_locator
from app.workflows.results import StepStatus, WorkflowExecutionStatus
from app.workflows.runner import run_workflow
from app.workflows.schema import (
    AssertVisibleStep,
    ClickStep,
    CssLocator,
    ExecutionLimits,
    ExtractTextStep,
    FillStep,
    NavigateStep,
    RoleLocator,
    SaveResultStep,
    TestIdLocator,
    TextLocator,
    VerifyStateStep,
    WaitVisibleStep,
    WorkflowDefinition,
    XPathLocator,
)


@pytest.fixture
async def browser() -> Browser:
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        try:
            yield b
        finally:
            await b.close()


@pytest.fixture
async def page(browser: Browser) -> Page:
    context = await browser.new_context()
    pg = await context.new_page()
    try:
        yield pg
    finally:
        await context.close()


# =============================================================================
# 1. Locator Resolution Tests
# =============================================================================


async def test_resolve_locator_all_types(page: Page):
    await page.set_content("""
        <div>
            <button id="btn-save" data-testid="save-button">Save Changes</button>
            <p class="summary-text">Summary of work</p>
            <span id="xpath-target">XPath Text</span>
        </div>
    """)

    # Role locator
    loc_role = resolve_locator(page, RoleLocator(role="button", name="Save Changes"))
    assert await loc_role.inner_text() == "Save Changes"

    # Test ID locator
    loc_testid = resolve_locator(page, TestIdLocator(value="save-button"))
    assert await loc_testid.inner_text() == "Save Changes"

    # Text locator
    loc_text = resolve_locator(page, TextLocator(value="Summary of work"))
    assert "Summary of work" in await loc_text.inner_text()

    # CSS locator
    loc_css = resolve_locator(page, CssLocator(value=".summary-text"))
    assert "Summary of work" in await loc_css.inner_text()

    # XPath locator
    loc_xpath = resolve_locator(page, XPathLocator(value="//span[@id='xpath-target']"))
    assert await loc_xpath.inner_text() == "XPath Text"


# =============================================================================
# 2. Individual Action Execution Tests
# =============================================================================


async def test_wait_visible_action_success(page: Page):
    await page.set_content("<div id='box' data-testid='box-el'>Loaded content</div>")
    interpreter = WorkflowInterpreter()
    step = WaitVisibleStep(id="wait_step", target=TestIdLocator(value="box-el"))
    context = ExecutionContext(workflow=WorkflowDefinition(provider="rewardlab", steps=[step]))

    res = await interpreter._execute_step_safe(
        step=step,
        page=page,
        context=context,
        step_timeout=5.0,
        provider="rewardlab",
    )
    assert res.status == StepStatus.SUCCESS
    assert res.output_data == {"visible": True}


async def test_extract_text_action_success(page: Page):
    await page.set_content("<p data-testid='task-desc'>   Simulated reward task details.   </p>")
    interpreter = WorkflowInterpreter()
    step = ExtractTextStep(
        id="extract_step",
        target=TestIdLocator(value="task-desc"),
        output="extracted_summary",
    )
    context = ExecutionContext(workflow=WorkflowDefinition(provider="rewardlab", steps=[step]))

    res = await interpreter._execute_step_safe(
        step=step,
        page=page,
        context=context,
        step_timeout=5.0,
        provider="rewardlab",
    )
    assert res.status == StepStatus.SUCCESS
    assert context.outputs["extracted_summary"] == "Simulated reward task details."
    assert res.output_data == {"extracted_summary": "Simulated reward task details."}


async def test_click_action_success(page: Page):
    await page.set_content("""
        <button id='btn' data-testid='action-btn' onclick="this.innerText='Clicked!'">
            Submit
        </button>
    """)
    interpreter = WorkflowInterpreter()
    step = ClickStep(
        id="click_step",
        target=TestIdLocator(value="action-btn"),
        side_effect=True,
    )
    context = ExecutionContext(workflow=WorkflowDefinition(provider="rewardlab", steps=[step]))

    res = await interpreter._execute_step_safe(
        step=step,
        page=page,
        context=context,
        step_timeout=5.0,
        provider="rewardlab",
    )
    assert res.status == StepStatus.SUCCESS
    assert res.output_data == {"clicked": True, "side_effect": True}
    assert await page.inner_text("#btn") == "Clicked!"


async def test_fill_action_success(page: Page):
    await page.set_content("<input id='user-in' data-testid='username-field' value='old_val'>")
    interpreter = WorkflowInterpreter()
    step = FillStep(
        id="fill_step",
        target=TestIdLocator(value="username-field"),
        value="test_user_01",
        clear_before=True,
    )
    context = ExecutionContext(workflow=WorkflowDefinition(provider="rewardlab", steps=[step]))

    res = await interpreter._execute_step_safe(
        step=step,
        page=page,
        context=context,
        step_timeout=5.0,
        provider="rewardlab",
    )
    assert res.status == StepStatus.SUCCESS
    assert await page.input_value("#user-in") == "test_user_01"


async def test_assert_visible_action_success_and_failure(page: Page):
    await page.set_content("<div data-testid='present-banner'>Welcome</div>")
    interpreter = WorkflowInterpreter()

    # Success case
    step_ok = AssertVisibleStep(id="assert_ok", target=TestIdLocator(value="present-banner"))
    context = ExecutionContext(workflow=WorkflowDefinition(provider="rewardlab", steps=[step_ok]))
    res_ok = await interpreter._execute_step_safe(
        step=step_ok,
        page=page,
        context=context,
        step_timeout=5.0,
        provider="rewardlab",
    )
    assert res_ok.status == StepStatus.SUCCESS

    # Failure case
    step_fail = AssertVisibleStep(
        id="assert_fail",
        target=TestIdLocator(value="missing-banner"),
        timeout_seconds=1,
    )
    res_fail = await interpreter._execute_step_safe(
        step=step_fail,
        page=page,
        context=context,
        step_timeout=1.0,
        provider="rewardlab",
    )
    assert res_fail.status == StepStatus.FAILED
    assert res_fail.error_code == "ASSERTION_FAILED"
    assert "was not visible" in res_fail.error_message


# =============================================================================
# 3. State Verification Tests
# =============================================================================


async def test_verify_state_dom_source(page: Page):
    await page.set_content("<span data-testid='status-badge' class='badge-active'>Reviewed</span>")
    interpreter = WorkflowInterpreter()

    # DOM text check: success
    step_text_ok = VerifyStateStep(
        id="v1",
        source="dom",
        target=TestIdLocator(value="status-badge"),
        field="text",
        equals="Reviewed",
    )
    context = ExecutionContext(
        workflow=WorkflowDefinition(provider="rewardlab", steps=[step_text_ok])
    )
    res_text_ok = await interpreter._execute_step_safe(
        step=step_text_ok, page=page, context=context, step_timeout=5.0, provider="rewardlab"
    )
    assert res_text_ok.status == StepStatus.SUCCESS

    # DOM attribute check: failure
    step_attr_fail = VerifyStateStep(
        id="v2",
        source="dom",
        target=TestIdLocator(value="status-badge"),
        field="class",
        equals="badge-pending",
    )
    res_attr_fail = await interpreter._execute_step_safe(
        step=step_attr_fail, page=page, context=context, step_timeout=5.0, provider="rewardlab"
    )
    assert res_attr_fail.status == StepStatus.FAILED
    assert res_attr_fail.error_code == "STATE_VERIFICATION_FAILED"


async def test_verify_state_api_mock(page: Page):
    # Mock httpx AsyncClient returning API response
    mock_response = httpx.Response(
        status_code=200,
        json={"username": "test_01", "task_id": "task-001", "reviewed": True, "total_points": 130},
        request=httpx.Request("GET", "http://127.0.0.1:8001/api/accounts/test_01/tasks/task-001"),
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get.return_value = mock_response

    interpreter = WorkflowInterpreter(http_client=mock_client)

    # Success: reviewed == True
    step_ok = VerifyStateStep(
        id="v_api_ok",
        source="rewardlab_api",
        task_id="task-001",
        field="reviewed",
        equals=True,
    )
    context = ExecutionContext(
        workflow=WorkflowDefinition(provider="rewardlab", steps=[step_ok]),
        account_id="test_01",
        task_id="task-001",
    )
    res_ok = await interpreter._execute_step_safe(
        step=step_ok, page=page, context=context, step_timeout=5.0, provider="rewardlab"
    )
    assert res_ok.status == StepStatus.SUCCESS

    # Mismatch: total_points == 999 -> fails
    step_fail = VerifyStateStep(
        id="v_api_fail",
        source="rewardlab_api",
        task_id="task-001",
        field="total_points",
        equals=999,
    )
    res_fail = await interpreter._execute_step_safe(
        step=step_fail, page=page, context=context, step_timeout=5.0, provider="rewardlab"
    )
    assert res_fail.status == StepStatus.FAILED
    assert res_fail.error_code == "STATE_VERIFICATION_FAILED"
    assert "expected 999, got 130" in res_fail.error_message


# =============================================================================
# 4. Error Handling & Timeout Tests
# =============================================================================


async def test_unsupported_action_fails_clearly(page: Page):
    interpreter = WorkflowInterpreter()
    # SaveResultStep is valid in schema but unsupported in M2.2 interpreter
    step = SaveResultStep(id="save_step", key="result_key", value="val")
    context = ExecutionContext(workflow=WorkflowDefinition(provider="rewardlab", steps=[step]))

    res = await interpreter._execute_step_safe(
        step=step, page=page, context=context, step_timeout=5.0, provider="rewardlab"
    )
    assert res.status == StepStatus.FAILED
    assert res.error_code == "UNSUPPORTED_ACTION_TYPE"
    assert "not yet supported" in res.error_message


async def test_step_timeout_handling(page: Page):
    await page.set_content("<div>No matching elements</div>")
    interpreter = WorkflowInterpreter()
    step = WaitVisibleStep(
        id="timeout_step",
        target=TestIdLocator(value="non-existent-id"),
        timeout_seconds=1,
    )
    context = ExecutionContext(workflow=WorkflowDefinition(provider="rewardlab", steps=[step]))

    res = await interpreter._execute_step_safe(
        step=step, page=page, context=context, step_timeout=1.0, provider="rewardlab"
    )
    assert res.status == StepStatus.TIMEOUT
    assert res.error_code == "STEP_TIMEOUT"
    assert "timed out after 1.0s" in res.error_message


async def test_workflow_overall_timeout_handling(page: Page):
    await page.set_content("<div data-testid='item'>Test</div>")
    interpreter = WorkflowInterpreter()

    wf = WorkflowDefinition(
        id="timeout_wf",
        provider="rewardlab",
        execution=ExecutionLimits(timeout_seconds=2, step_timeout_seconds=5),
        steps=[
            WaitVisibleStep(id="step_1", target=TestIdLocator(value="item")),
            WaitVisibleStep(id="step_2", target=TestIdLocator(value="item")),
        ],
    )

    # Delay after first step completes so remaining workflow timeout expires before second step
    orig_dispatch = interpreter._dispatch_action
    call_count = 0

    async def delayed_dispatch(*args: Any, **kwargs: Any):
        nonlocal call_count
        call_count += 1
        res = await orig_dispatch(*args, **kwargs)
        if call_count == 1:
            await asyncio.sleep(2.2)
        return res

    interpreter._dispatch_action = delayed_dispatch

    result = await interpreter.execute(workflow=wf, page=page)
    assert result.status == WorkflowExecutionStatus.TIMEOUT
    assert result.error_code == "WORKFLOW_TIMEOUT"
    assert len(result.step_results) == 2
    assert result.step_results[0].status == StepStatus.SUCCESS
    assert result.step_results[1].status == StepStatus.TIMEOUT


async def test_fail_fast_skips_remaining_steps(page: Page):
    await page.set_content("<div>Initial page</div>")
    interpreter = WorkflowInterpreter()

    wf = WorkflowDefinition(
        id="fail_fast_wf",
        provider="rewardlab",
        steps=[
            AssertVisibleStep(id="step_fail", target=TestIdLocator(value="missing-element")),
            ClickStep(id="step_skipped_1", target=TestIdLocator(value="missing-btn")),
            ExtractTextStep(id="step_skipped_2", target=TestIdLocator(value="text"), output="out"),
        ],
    )

    result = await interpreter.execute(workflow=wf, page=page)
    assert result.status == WorkflowExecutionStatus.FAILED
    assert result.error_code == "ASSERTION_FAILED"
    assert len(result.step_results) == 3
    assert result.step_results[0].status == StepStatus.FAILED
    assert result.step_results[1].status == StepStatus.SKIPPED
    assert result.step_results[2].status == StepStatus.SKIPPED


# =============================================================================
# 5. Security & Policy Denial Tests
# =============================================================================


async def test_policy_denial_unauthorized_provider(page: Page):
    interpreter = WorkflowInterpreter()
    wf = WorkflowDefinition(
        id="mr_wf",
        provider="microsoft_rewards",
        steps=[NavigateStep(id="nav_1", path="/tasks")],
    )

    result = await interpreter.execute(workflow=wf, page=page)
    assert result.status == WorkflowExecutionStatus.POLICY_DENIED
    assert result.error_code == "POLICY_DENIED"
    assert "Microsoft Rewards is companion-only" in result.error_message


async def test_policy_denial_before_navigate(page: Page):
    interpreter = WorkflowInterpreter()
    wf = WorkflowDefinition(
        id="disallowed_host_wf",
        provider="rewardlab",
        steps=[
            NavigateStep(
                id="nav_bad",
                url="https://attacker-domain.evil/steal",
            )
        ],
    )

    result = await interpreter.execute(workflow=wf, page=page)
    assert result.status == WorkflowExecutionStatus.POLICY_DENIED
    assert result.error_code == "POLICY_DENIED"
    assert "not in provider domain allowlist" in result.error_message


async def test_policy_denial_on_redirect(page: Page):
    """Verifies that route-level and post-navigation checks catch redirection
    to an unpermitted domain.
    """
    policy = ProviderPolicy(
        provider="rewardlab",
        automation_allowed=True,
        domain_allowlist=["127.0.0.1", "localhost"],
        allowed_actions=["navigate"],
    )
    evaluator = PolicyEvaluator(policies={"rewardlab": policy})
    interpreter = WorkflowInterpreter(policy_evaluator=evaluator)

    # Set up route handler in test to simulate a 302 redirect from loopback to external host
    async def redirect_handler(route):
        if "redirect-me" in route.request.url:
            await route.fulfill(
                status=302,
                headers={"Location": "https://malicious-external-site.com/pwn"},
            )
        else:
            await route.continue_()

    await page.route("**/redirect-me", redirect_handler)

    wf = WorkflowDefinition(
        id="redirect_wf",
        provider="rewardlab",
        steps=[
            NavigateStep(
                id="nav_redirect",
                url="http://127.0.0.1:8001/redirect-me",
            )
        ],
    )

    try:
        result = await interpreter.execute(workflow=wf, page=page)
        # Should be stopped by route guard or post-check
        denied_or_failed = (WorkflowExecutionStatus.POLICY_DENIED, WorkflowExecutionStatus.FAILED)
        assert result.status in denied_or_failed
    finally:
        await page.unroute("**/redirect-me", redirect_handler)


async def test_secret_redaction_in_fill_and_errors(page: Page):
    await page.set_content("<input id='pwd' data-testid='password-input'>")
    interpreter = WorkflowInterpreter()

    step = FillStep(
        id="fill_pwd",
        target=TestIdLocator(value="password-input"),
        value="super_secret_password_1234",
    )
    context = ExecutionContext(workflow=WorkflowDefinition(provider="rewardlab", steps=[step]))

    res = await interpreter._execute_step_safe(
        step=step, page=page, context=context, step_timeout=5.0, provider="rewardlab"
    )
    assert res.status == StepStatus.SUCCESS
    # Raw password must NEVER appear in output data
    assert "super_secret_password_1234" not in str(res.output_data)
    assert res.output_data == {"filled": True, "length": len("super_secret_password_1234")}


async def test_run_workflow_cleans_up_context(browser: Browser):
    wf = WorkflowDefinition(
        id="cleanup_wf",
        provider="rewardlab",
        steps=[
            AssertVisibleStep(
                id="failing_step",
                target=TestIdLocator(value="never-exists"),
                timeout_seconds=1,
            ),
        ],
    )

    # Ensure run_workflow closes context cleanly on error
    result = await run_workflow(
        workflow=wf,
        browser=browser,
        base_url="http://127.0.0.1:8001",
    )
    assert result.status == WorkflowExecutionStatus.FAILED
    assert result.error_code == "ASSERTION_FAILED"
