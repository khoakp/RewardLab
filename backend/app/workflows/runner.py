from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from playwright.async_api import Browser

from app.core.errors import PolicyDeniedException
from app.core.policy import PolicyEvaluator
from app.observability.logging import get_logger, redact_url_credentials
from app.workflows.interpreter import WorkflowInterpreter
from app.workflows.parser import parse_workflow_content
from app.workflows.results import WorkflowExecutionResult, WorkflowExecutionStatus
from app.workflows.schema import WorkflowDefinition
from app.workflows.validator import validate_workflow_definition

if TYPE_CHECKING:
    from app.execution.browser_manager import BrowserManager

logger = get_logger("adbap.workflows.runner")


async def run_workflow(
    workflow: WorkflowDefinition | str | bytes | dict[str, Any],
    browser: Browser | BrowserManager,
    base_url: str = "http://127.0.0.1:8001",
    account_id: str = "test_01",
    task_id: str | None = None,
    session_id: str | None = None,
    policy_evaluator: PolicyEvaluator | None = None,
    http_client: httpx.AsyncClient | None = None,
    initial_outputs: dict[str, Any] | None = None,
    context_timeout_seconds: float | None = None,
    record_trace_on_failure: bool = True,
) -> WorkflowExecutionResult:
    """Executes a workflow within an isolated Playwright BrowserContext.

    Supports either a direct Playwright `Browser` instance or a managed `BrowserManager`.
    Guarantees that browser contexts are short-lived, never shared across accounts,
    and always closed in a finally block.
    """
    from app.execution.browser_manager import BrowserManager

    if isinstance(browser, BrowserManager):
        return await browser.execute_workflow(
            workflow=workflow,
            account_id=account_id,
            task_id=task_id,
            session_id=session_id,
            base_url=base_url,
            context_timeout_seconds=context_timeout_seconds,
            initial_outputs=initial_outputs,
            policy_evaluator=policy_evaluator,
            http_client=http_client,
            record_trace_on_failure=record_trace_on_failure,
        )

    evaluator = policy_evaluator or PolicyEvaluator()
    start_time = datetime.now(UTC)

    # 1. Ensure WorkflowDefinition is strictly validated
    try:
        if isinstance(workflow, WorkflowDefinition):
            wf_def = validate_workflow_definition(workflow, evaluator)
        elif isinstance(workflow, dict):
            wf_def = WorkflowDefinition.model_validate(workflow)
            wf_def = validate_workflow_definition(wf_def, evaluator)
        else:
            wf_def = parse_workflow_content(workflow)
            wf_def = validate_workflow_definition(wf_def, evaluator)
    except PolicyDeniedException as pde:
        wf_id = getattr(workflow, "id", "denied_workflow")
        provider_name = getattr(workflow, "provider", "unknown")
        step_count = len(getattr(workflow, "steps", []))
        logger.warning("Pre-execution policy evaluation denied: %s", pde.message)
        return WorkflowExecutionResult(
            workflow_id=wf_id,
            provider=provider_name,
            status=WorkflowExecutionStatus.POLICY_DENIED,
            steps_executed=0,
            total_steps=step_count,
            step_results=[],
            outputs=initial_outputs or {},
            error_code="POLICY_DENIED",
            error_message=pde.message,
            started_at=start_time,
            completed_at=datetime.now(UTC),
            duration_seconds=0.0,
            can_retry=False,
            requires_verification=False,
        )

    # Determine service worker restriction from provider policy
    block_sw = True
    provider_policy = evaluator.get_policy(wf_def.provider)
    if provider_policy is not None:
        block_sw = provider_policy.block_service_workers
    sw_setting = "block" if block_sw else "allow"

    # 2. Spawn isolated browser context
    context = await browser.new_context(
        ignore_https_errors=False,
        service_workers=sw_setting,
    )

    try:
        # Pre-authenticate context with synthetic test session cookie if provided
        if session_id:
            parsed = urlparse(base_url)
            cookie_domain = parsed.hostname or "127.0.0.1"
            await context.add_cookies(
                [
                    {
                        "name": "rewardlab_session",
                        "value": session_id,
                        "domain": cookie_domain,
                        "path": "/",
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            logger.info(
                "Injected synthetic test session cookie for account '%s' (domain: %s)",
                account_id,
                cookie_domain,
            )

        page = await context.new_page()

        interpreter = WorkflowInterpreter(
            policy_evaluator=evaluator,
            base_url=base_url,
            http_client=http_client,
        )

        return await interpreter.execute(
            workflow=wf_def,
            page=page,
            account_id=account_id,
            task_id=task_id,
            initial_outputs=initial_outputs,
        )

    finally:
        # Guarantee context release and cleanup
        try:
            await context.close()
            logger.info("Closed isolated browser context for account '%s'", account_id)
        except Exception as e:
            logger.warning(
                "Error closing browser context for '%s': %s",
                account_id,
                redact_url_credentials(str(e)),
            )
