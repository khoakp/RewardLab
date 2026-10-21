import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.policy import PolicyEvaluator
from app.observability.logging import get_logger, redact_data, redact_url_credentials
from app.workflows.errors import (
    ActionTimeoutError,
    ElementDisabledError,
    RuntimePolicyViolationError,
    StepAssertionError,
    StepVerificationError,
    UnsupportedActionError,
    WorkflowExecutionError,
)
from app.workflows.locator import resolve_locator
from app.workflows.results import (
    StepExecutionResult,
    StepStatus,
    WorkflowExecutionResult,
    WorkflowExecutionStatus,
    WorkflowOutcomeType,
)
from app.workflows.schema import (
    AssertVisibleStep,
    ClickStep,
    ExtractTextStep,
    FillStep,
    HoverStep,
    NavigateStep,
    NotifyUserStep,
    PressKeyStep,
    RequestHumanActionStep,
    ScrollStep,
    VerifyStateStep,
    WaitForStateStep,
    WaitVisibleStep,
    WorkflowDefinition,
    WorkflowStep,
    is_step_mutating,
)

logger = get_logger("adbap.workflows.interpreter")

# Supported action types in Milestone P1
SUPPORTED_ACTIONS = {
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
}

KEYBOARD_SUBMISSION_KEYS = {"enter", "numpadenter", "return"}


class ExecutionContext:
    """Carries runtime state, outputs, and parameters during workflow execution."""

    def __init__(
        self,
        workflow: WorkflowDefinition,
        base_url: str = "http://127.0.0.1:8001",
        account_id: str = "test_01",
        task_id: str | None = None,
        initial_outputs: dict[str, Any] | None = None,
    ) -> None:
        self.workflow = workflow
        self.base_url = base_url.rstrip("/")
        self.account_id = account_id
        self.task_id = task_id
        self.outputs: dict[str, Any] = initial_outputs.copy() if initial_outputs else {}
        self.last_response: Any | None = None


class WorkflowInterpreter:
    """Async Playwright action interpreter for strict versioned workflows."""

    def __init__(
        self,
        policy_evaluator: PolicyEvaluator | None = None,
        base_url: str = "http://127.0.0.1:8001",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.policy_evaluator = policy_evaluator or PolicyEvaluator()
        self.base_url = base_url.rstrip("/")
        self._http_client = http_client

    async def execute(
        self,
        workflow: WorkflowDefinition,
        page: Page,
        account_id: str = "test_01",
        task_id: str | None = None,
        initial_outputs: dict[str, Any] | None = None,
        is_aborted_callback: Callable[[], bool] | None = None,
    ) -> WorkflowExecutionResult:
        """Executes a validated WorkflowDefinition against a Playwright Page."""
        start_time = datetime.now(UTC)
        mono_start = time.monotonic()
        total_timeout = workflow.execution.timeout_seconds

        context = ExecutionContext(
            workflow=workflow,
            base_url=self.base_url,
            account_id=account_id,
            task_id=task_id,
            initial_outputs=initial_outputs,
        )

        step_results: list[StepExecutionResult] = []
        overall_status = WorkflowExecutionStatus.SUCCESS
        overall_error_code: str | None = None
        overall_error_message: str | None = None

        # Resolve provider policy for allowlist & security checks
        provider_policy = self.policy_evaluator.policies.get(workflow.provider)
        if not provider_policy or not provider_policy.automation_allowed:
            reason = (
                f"Automation is prohibited for provider '{workflow.provider}'. "
                "Microsoft Rewards is companion-only and unknown providers are denied by default."
            )
            logger.warning("Workflow dispatch denied: %s", reason)
            return WorkflowExecutionResult(
                workflow_id=workflow.id,
                provider=workflow.provider,
                status=WorkflowExecutionStatus.POLICY_DENIED,
                outcome_type=WorkflowOutcomeType.PROVIDER_REJECTED,
                steps_executed=0,
                total_steps=len(workflow.steps),
                step_results=[],
                outputs=context.outputs,
                error_code="POLICY_DENIED",
                error_message=reason,
                started_at=start_time,
                completed_at=datetime.now(UTC),
                duration_seconds=0.0,
            )

        # Capabilities check: ensure workflow does not claim ungranted capabilities
        has_eval_caps = hasattr(self.policy_evaluator, "evaluate_capabilities")
        if workflow.requested_capabilities and has_eval_caps:
            cap_decision = self.policy_evaluator.evaluate_capabilities(
                workflow.provider,
                workflow.requested_capabilities,
                actor="action_interpreter",
            )
            if not cap_decision.allowed:
                logger.warning("Workflow dispatch denied: %s", cap_decision.reason)
                return WorkflowExecutionResult(
                    workflow_id=workflow.id,
                    provider=workflow.provider,
                    status=WorkflowExecutionStatus.POLICY_DENIED,
                    outcome_type=WorkflowOutcomeType.PROVIDER_REJECTED,
                    steps_executed=0,
                    total_steps=len(workflow.steps),
                    step_results=[],
                    outputs=context.outputs,
                    error_code=cap_decision.error_code or "CAPABILITY_NOT_GRANTED",
                    error_message=cap_decision.reason,
                    started_at=start_time,
                    completed_at=datetime.now(UTC),
                    duration_seconds=0.0,
                )

        domain_allowlist = provider_policy.domain_allowlist

        # Route-level navigation security guard (defense-in-depth)
        async def _navigation_route_guard(route: Route) -> None:
            req = route.request
            if req.is_navigation_request():
                is_valid, reason, _ = self.policy_evaluator.validate_static_url(
                    req.url,
                    domain_allowlist,
                    allowed_ports=provider_policy.allowed_ports,
                )
                if not is_valid:
                    logger.warning(
                        "Route guard blocked out-of-allowlist navigation to %s: %s",
                        redact_url_credentials(req.url),
                        reason,
                    )
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        # Attach route guard
        await page.route("**/*", _navigation_route_guard)

        # Popup / New Window Guard: block or inspect popups
        async def _popup_guard(popup_page: Page) -> None:
            if not provider_policy.allow_popups:
                logger.warning(
                    "Popup window opened but popups are prohibited by policy. Closing popup."
                )
                with contextlib.suppress(Exception):
                    await popup_page.close()
                return

            is_valid, reason, _ = self.policy_evaluator.validate_static_url(
                popup_page.url,
                domain_allowlist,
                allowed_ports=provider_policy.allowed_ports,
            )
            if not is_valid:
                logger.warning(
                    "Popup to %s blocked by policy: %s",
                    redact_url_credentials(popup_page.url),
                    reason,
                )
                with contextlib.suppress(Exception):
                    await popup_page.close()

        page.on("popup", _popup_guard)
        if page.context:
            page.context.on("page", _popup_guard)

        total_mutations_executed = 0
        unverified_mutations: list[str] = []
        verified_mutations: list[str] = []
        step_by_id: dict[str, WorkflowStep] = {s.id: s for s in workflow.steps}
        had_verified_postcondition = False
        provider_rejected = False
        human_action_required = False
        mutating_step_attempted = False

        try:
            for idx, step in enumerate(workflow.steps):
                # Check external abort or lease fencing signal prior to dispatching next step
                if is_aborted_callback and is_aborted_callback():
                    logger.warning(
                        "Execution aborted before step '%s': lease lost or cancelled",
                        step.id,
                    )
                    overall_status = WorkflowExecutionStatus.CANCELLED
                    overall_error_code = "LEASE_LOST_OR_CANCELLED"
                    overall_error_message = (
                        "Execution aborted: active lease was lost or job was cancelled"
                    )
                    self._skip_remaining_steps(workflow.steps[idx:], step_results)
                    break

                elapsed = time.monotonic() - mono_start
                remaining = total_timeout - elapsed

                if remaining <= 0:
                    overall_status = WorkflowExecutionStatus.TIMEOUT
                    overall_error_code = "WORKFLOW_TIMEOUT"
                    overall_error_message = (
                        f"Workflow exceeded overall execution timeout limit of {total_timeout}s"
                    )
                    # Mark current and remaining steps as skipped/failed
                    step_results.append(
                        StepExecutionResult(
                            step_id=step.id,
                            action=step.action,
                            status=StepStatus.TIMEOUT,
                            duration_ms=0.0,
                            error_code="WORKFLOW_TIMEOUT",
                            error_message=overall_error_message,
                            outcome_type=WorkflowOutcomeType.OUTCOME_UNCONFIRMED,
                        )
                    )
                    self._skip_remaining_steps(workflow.steps[idx + 1 :], step_results)
                    break

                # Step-specific timeout bound: minimum of step timeout and remaining workflow time
                step_limit = step.timeout_seconds or workflow.execution.step_timeout_seconds
                effective_step_timeout = min(float(step_limit), remaining)

                # Execute individual step
                step_res = await self._execute_step_safe(
                    step=step,
                    page=page,
                    context=context,
                    step_timeout=effective_step_timeout,
                    provider=workflow.provider,
                )
                step_results.append(step_res)

                # Track verified outcome components
                if step.action == "request_human_action":
                    human_action_required = True
                    overall_status = WorkflowExecutionStatus.FAILED
                    overall_error_code = "HUMAN_ACTION_REQUIRED"
                    prompt_txt = getattr(step, "prompt", "operator intervention required")
                    overall_error_message = (
                        f"Workflow execution paused: human action required: {prompt_txt}"
                    )
                    # Fail-fast: skip all remaining steps immediately without dispatching
                    self._skip_remaining_steps(workflow.steps[idx + 1 :], step_results)
                    break

                if step_res.error_code in (
                    "SECURITY_CHALLENGE_REQUIRED",
                    "HUMAN_ACTION_REQUIRED",
                ):
                    human_action_required = True

                is_mutation = is_step_mutating(step)
                if is_mutation:
                    mutating_step_attempted = True
                if is_mutation and step_res.status == StepStatus.SUCCESS:
                    total_mutations_executed += 1
                    # A new mutation invalidates prior verifications for this new mutation
                    unverified_mutations.append(step.id)

                if step.action == "verify_state" and step_res.status == StepStatus.SUCCESS:
                    had_verified_postcondition = True
                    verifies_ids = getattr(step, "verifies_step_ids", None)
                    if verifies_ids is not None:
                        for ref_id in verifies_ids:
                            if ref_id in unverified_mutations:
                                unverified_mutations.remove(ref_id)
                                if ref_id not in verified_mutations:
                                    verified_mutations.append(ref_id)
                            # Duplicate verify of ref_id does NOT relieve other mutations
                    else:
                        # Legacy backward compatibility (verifies_step_ids is None)
                        if len(unverified_mutations) == 1:
                            # Single unambiguous pending mutation
                            relieved = unverified_mutations.pop(0)
                            if relieved not in verified_mutations:
                                verified_mutations.append(relieved)
                        elif len(unverified_mutations) > 1:
                            # Check if pending mutations form a cohesive form sequence
                            is_form_sequence = (
                                len(unverified_mutations) >= 2
                                and all(
                                    step_by_id[s_id].action == "fill"
                                    for s_id in unverified_mutations[:-1]
                                )
                                and (
                                    step_by_id[unverified_mutations[-1]].action == "click"
                                    or (
                                        step_by_id[unverified_mutations[-1]].action == "press_key"
                                        and getattr(step_by_id[unverified_mutations[-1]], "key", "")
                                        .strip()
                                        .lower()
                                        in KEYBOARD_SUBMISSION_KEYS
                                    )
                                )
                            )
                            if is_form_sequence:
                                for m_id in list(unverified_mutations):
                                    if m_id not in verified_mutations:
                                        verified_mutations.append(m_id)
                                unverified_mutations.clear()
                            else:
                                # Ambiguous multiple mutations: fail-closed!
                                logger.warning(
                                    "Legacy verification step '%s' encountered ambiguous "
                                    "mutations %s; fail-closed without auto-relieving",
                                    step.id,
                                    unverified_mutations,
                                )

                if step_res.error_code in (
                    "POLICY_DENIED",
                    "HTTP_401_UNAUTHORIZED",
                    "HTTP_403_FORBIDDEN",
                    "HTTP_429_RATE_LIMITED",
                    "PROVIDER_REJECTED",
                ):
                    provider_rejected = True

                if step_res.status != StepStatus.SUCCESS:
                    overall_status = (
                        WorkflowExecutionStatus.POLICY_DENIED
                        if step_res.error_code == "POLICY_DENIED"
                        else WorkflowExecutionStatus.TIMEOUT
                        if step_res.status == StepStatus.TIMEOUT
                        else WorkflowExecutionStatus.FAILED
                    )
                    overall_error_code = step_res.error_code
                    overall_error_message = step_res.error_message

                    # Fail-fast: skip remaining steps
                    self._skip_remaining_steps(workflow.steps[idx + 1 :], step_results)
                    break

        finally:
            with contextlib.suppress(Exception):
                await page.unroute("**/*", _navigation_route_guard)
            with contextlib.suppress(Exception):
                await asyncio.sleep(0.1)
            if page.context and not provider_policy.allow_popups:
                for p in list(page.context.pages):
                    if p != page and not p.is_closed():
                        with contextlib.suppress(Exception):
                            await p.close()

        completed_time = datetime.now(UTC)
        duration_sec = round(time.monotonic() - mono_start, 4)

        executed_count = sum(
            1
            for s in step_results
            if s.status in (StepStatus.SUCCESS, StepStatus.FAILED, StepStatus.TIMEOUT)
        )

        # Classify business outcome:
        # Browser action success does NOT equate to business outcome success.
        if human_action_required or overall_error_code in (
            "SECURITY_CHALLENGE_REQUIRED",
            "HUMAN_ACTION_REQUIRED",
        ):
            outcome_type = WorkflowOutcomeType.HUMAN_ACTION_REQUIRED
            requires_verification = True
        elif provider_rejected or overall_error_code in (
            "POLICY_DENIED",
            "HTTP_401_UNAUTHORIZED",
            "HTTP_403_FORBIDDEN",
            "HTTP_429_RATE_LIMITED",
            "PROVIDER_REJECTED",
        ):
            outcome_type = WorkflowOutcomeType.PROVIDER_REJECTED
            requires_verification = False
        elif total_mutations_executed > 0 or mutating_step_attempted:
            if overall_status == WorkflowExecutionStatus.SUCCESS and len(unverified_mutations) == 0:
                outcome_type = WorkflowOutcomeType.STATE_VERIFIED
                requires_verification = False
            else:
                # Mutating side effect occurred or was attempted, but postcondition unconfirmed,
                # remaining unverified mutations exist, or execution failed (e.g. lost response)
                outcome_type = WorkflowOutcomeType.OUTCOME_UNCONFIRMED
                requires_verification = True
        else:
            # Read-only workflow
            if overall_status == WorkflowExecutionStatus.SUCCESS:
                outcome_type = (
                    WorkflowOutcomeType.STATE_VERIFIED
                    if had_verified_postcondition
                    else WorkflowOutcomeType.ACTION_EXECUTED
                )
                requires_verification = False
            else:
                outcome_type = WorkflowOutcomeType.OUTCOME_UNCONFIRMED
                requires_verification = False

        return WorkflowExecutionResult(
            workflow_id=workflow.id,
            provider=workflow.provider,
            status=overall_status,
            outcome_type=outcome_type,
            steps_executed=executed_count,
            total_steps=len(workflow.steps),
            step_results=step_results,
            outputs=context.outputs,
            error_code=overall_error_code,
            error_message=overall_error_message,
            started_at=start_time,
            completed_at=completed_time,
            duration_seconds=duration_sec,
            can_retry=False,
            requires_verification=requires_verification,
            unverified_mutations=list(unverified_mutations),
            verified_mutations=list(verified_mutations),
        )

    def _skip_remaining_steps(
        self, remaining_steps: list[WorkflowStep], step_results: list[StepExecutionResult]
    ) -> None:
        for r_step in remaining_steps:
            step_results.append(
                StepExecutionResult(
                    step_id=r_step.id,
                    action=r_step.action,
                    status=StepStatus.SKIPPED,
                    duration_ms=0.0,
                    output_data=None,
                    error_code=None,
                    error_message="Skipped due to prior step failure",
                )
            )

    async def _execute_step_safe(
        self,
        step: WorkflowStep,
        page: Page,
        context: ExecutionContext,
        step_timeout: float,
        provider: str,
    ) -> StepExecutionResult:
        """Safely executes a single step, mapping exceptions to structured StepExecutionResult."""
        t0 = time.monotonic()
        step_id = step.id
        action = step.action

        logger.info("Executing step '%s' (action=%s, timeout=%.1fs)", step_id, action, step_timeout)

        # Check action support
        if action not in SUPPORTED_ACTIONS:
            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=StepStatus.FAILED,
                duration_ms=round((time.monotonic() - t0) * 1000, 2),
                error_code="UNSUPPORTED_ACTION_TYPE",
                error_message=(
                    f"Action '{action}' is defined in schema but not yet supported by the "
                    "Playwright action interpreter in Milestone M2.2"
                ),
            )

        # Evaluate action policy
        policy_decision = self.policy_evaluator.evaluate_action(
            provider=provider,
            action=action,
            actor="action_interpreter",
        )
        if not policy_decision.allowed:
            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=StepStatus.FAILED,
                duration_ms=round((time.monotonic() - t0) * 1000, 2),
                error_code="POLICY_DENIED",
                error_message=policy_decision.reason,
            )

        try:
            # Dispatch to specific action handler
            output_data = await self._dispatch_action(
                step=step,
                page=page,
                context=context,
                step_timeout=step_timeout,
                provider=provider,
            )
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            logger.info("Step '%s' completed successfully in %.1fms", step_id, elapsed_ms)
            step_outcome: WorkflowOutcomeType | None = None
            if action == "request_human_action":
                step_outcome = WorkflowOutcomeType.HUMAN_ACTION_REQUIRED

            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=StepStatus.SUCCESS,
                duration_ms=elapsed_ms,
                output_data=redact_data(output_data) if output_data else None,
                outcome_type=step_outcome,
            )

        except PlaywrightTimeoutError:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            msg = f"Step '{step_id}' timed out after {step_timeout:.1f}s"
            logger.warning("Step timeout: %s", msg)
            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=StepStatus.TIMEOUT,
                duration_ms=elapsed_ms,
                error_code="STEP_TIMEOUT",
                error_message=msg,
            )

        except ActionTimeoutError as e:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=StepStatus.TIMEOUT,
                duration_ms=elapsed_ms,
                error_code=e.code,
                error_message=e.message,
            )

        except RuntimePolicyViolationError as e:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=StepStatus.FAILED,
                duration_ms=elapsed_ms,
                error_code=e.code,
                error_message=e.message,
            )

        except WorkflowExecutionError as e:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            step_outcome: WorkflowOutcomeType | None = None
            if e.code in (
                "POLICY_DENIED",
                "HTTP_401_UNAUTHORIZED",
                "HTTP_403_FORBIDDEN",
                "HTTP_429_RATE_LIMITED",
                "PROVIDER_REJECTED",
            ):
                step_outcome = WorkflowOutcomeType.PROVIDER_REJECTED
            elif e.code in ("SECURITY_CHALLENGE_REQUIRED", "HUMAN_ACTION_REQUIRED"):
                step_outcome = WorkflowOutcomeType.HUMAN_ACTION_REQUIRED

            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=StepStatus.FAILED,
                duration_ms=elapsed_ms,
                error_code=e.code,
                error_message=e.message,
                outcome_type=step_outcome,
            )

        except PlaywrightError as e:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            sanitized_msg = redact_url_credentials(str(e))
            lower_msg = sanitized_msg.lower()
            # Distinguish element not found/visible, timeout, detached,
            # disabled, modal blocking, or browser disconnect
            code = "ACTION_EXECUTION_FAILED"
            if "timeout" in lower_msg or "timed out" in lower_msg:
                status = StepStatus.TIMEOUT
                code = "STEP_TIMEOUT"
            elif any(
                s in lower_msg
                for s in [
                    "target closed",
                    "browser closed",
                    "browser has been closed",
                    "connection closed",
                    "target page, context or browser has been closed",
                ]
            ):
                status = StepStatus.FAILED
                code = "BROWSER_DISCONNECTED"
            elif "detached" in lower_msg or "not attached" in lower_msg:
                status = StepStatus.FAILED
                code = "ELEMENT_DETACHED"
            elif "intercepts pointer events" in lower_msg or "is not clickable" in lower_msg:
                status = StepStatus.FAILED
                code = "MODAL_BLOCKING"
            elif "disabled" in lower_msg:
                status = StepStatus.FAILED
                code = "ELEMENT_DISABLED"
            elif "not visible" in lower_msg:
                status = StepStatus.FAILED
                code = "ELEMENT_NOT_VISIBLE"
            elif "waiting for locator" in lower_msg:
                status = StepStatus.FAILED
                code = "ELEMENT_NOT_FOUND"
            else:
                status = StepStatus.FAILED

            logger.error("Playwright error in step '%s': %s", step_id, sanitized_msg)
            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=status,
                duration_ms=elapsed_ms,
                error_code=code,
                error_message=sanitized_msg,
            )

        except Exception as e:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            sanitized_msg = redact_url_credentials(str(e))
            logger.exception("Unexpected error in step '%s': %s", step_id, sanitized_msg)
            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=StepStatus.FAILED,
                duration_ms=elapsed_ms,
                error_code="ACTION_EXECUTION_FAILED",
                error_message=sanitized_msg,
            )

    async def _dispatch_action(
        self,
        step: WorkflowStep,
        page: Page,
        context: ExecutionContext,
        step_timeout: float,
        provider: str,
    ) -> dict[str, Any] | None:
        """Dispatches typed WorkflowStep to its concrete async action handler."""
        timeout_ms = int(step_timeout * 1000)

        match step:
            case NavigateStep():
                return await self._handle_navigate(step, page, context, timeout_ms, provider)

            case WaitVisibleStep():
                loc = resolve_locator(page, step.target).first
                await loc.wait_for(state="visible", timeout=timeout_ms)
                return {"visible": True}

            case ExtractTextStep():
                loc = resolve_locator(page, step.target).first
                await loc.wait_for(state="visible", timeout=timeout_ms)
                text = await loc.inner_text(timeout=timeout_ms)
                cleaned = text.strip()
                context.outputs[step.output] = cleaned
                return {step.output: cleaned}

            case ClickStep():
                loc = resolve_locator(page, step.target).first
                await loc.wait_for(state="visible", timeout=timeout_ms)
                if await loc.is_disabled():
                    raise ElementDisabledError(
                        message=f"Element target '{step.target}' is disabled and cannot be clicked",
                        step_id=step.id,
                        details={"target": str(step.target)},
                    )
                initial_url = page.url

                last_network_response: list[Any] = []

                def _capture_resp(resp: Any) -> None:
                    if resp.status >= 400 and getattr(resp.request, "resource_type", "") in (
                        "document",
                        "fetch",
                        "xhr",
                    ):
                        last_network_response.append(resp)

                page.on("response", _capture_resp)
                try:
                    await loc.click(
                        button=step.button,
                        click_count=step.click_count,
                        timeout=timeout_ms,
                    )
                    await asyncio.sleep(0.05)
                finally:
                    with contextlib.suppress(Exception):
                        page.remove_listener("response", _capture_resp)

                if last_network_response:
                    bad_resp = last_network_response[-1]
                    if bad_resp.status == 504:
                        raise StepVerificationError(
                            message=(
                                f"Click received HTTP 504 Gateway Timeout from "
                                f"{redact_url_credentials(bad_resp.url)}"
                            ),
                            code="HTTP_504_GATEWAY_TIMEOUT",
                            step_id=step.id,
                            details={"status_code": 504, "error_code": "HTTP_504_GATEWAY_TIMEOUT"},
                        )
                    elif bad_resp.status >= 500:
                        raise StepVerificationError(
                            message=(
                                f"Click received HTTP {bad_resp.status} Server Error from "
                                f"{redact_url_credentials(bad_resp.url)}"
                            ),
                            code=f"HTTP_{bad_resp.status}_SERVER_ERROR",
                            step_id=step.id,
                            details={
                                "status_code": bad_resp.status,
                                "error_code": f"HTTP_{bad_resp.status}_SERVER_ERROR",
                            },
                        )

                # Verify destination URL if click caused navigation or redirect
                current_url = page.url
                if current_url != initial_url:
                    provider_policy = self.policy_evaluator.policies.get(provider)
                    if provider_policy:
                        is_valid, reason, _ = self.policy_evaluator.validate_static_url(
                            current_url,
                            provider_policy.domain_allowlist,
                            allowed_ports=provider_policy.allowed_ports,
                        )
                        if not is_valid:
                            raise RuntimePolicyViolationError(
                                message=(
                                    f"Click triggered unauthorized navigation to "
                                    f"{redact_url_credentials(current_url)}: {reason}"
                                ),
                                step_id=step.id,
                                details={"destination_url": redact_url_credentials(current_url)},
                            )
                return {"clicked": True, "side_effect": step.side_effect}

            case FillStep():
                loc = resolve_locator(page, step.target).first
                await loc.wait_for(state="visible", timeout=timeout_ms)
                if await loc.is_disabled():
                    raise ElementDisabledError(
                        message=f"Element target '{step.target}' is disabled and cannot be filled",
                        step_id=step.id,
                        details={"target": str(step.target)},
                    )
                if step.clear_before:
                    await loc.fill(step.value, timeout=timeout_ms)
                else:
                    await loc.type(step.value, timeout=timeout_ms)
                return {"filled": True, "length": len(step.value)}

            case HoverStep():
                return await self._handle_hover(step, page, context, timeout_ms)

            case ScrollStep():
                return await self._handle_scroll(step, page, context, timeout_ms)

            case PressKeyStep():
                return await self._handle_press_key(step, page, context, timeout_ms)

            case WaitForStateStep():
                return await self._handle_wait_for_state(step, page, context, timeout_ms)

            case AssertVisibleStep():
                loc = resolve_locator(page, step.target).first
                try:
                    await loc.wait_for(state="visible", timeout=timeout_ms)
                except PlaywrightTimeoutError:
                    raise StepAssertionError(
                        message=(
                            f"Element target '{step.target.by}' was not visible "
                            f"within {step_timeout:.1f}s"
                        ),
                        step_id=step.id,
                        details={"target": str(step.target)},
                    ) from None
                return {"asserted_visible": True}

            case VerifyStateStep():
                return await self._handle_verify_state(step, page, context, timeout_ms)

            case NotifyUserStep():
                return await self._handle_notify_user(step, page, context, timeout_ms)

            case RequestHumanActionStep():
                return await self._handle_request_human_action(step, page, context, timeout_ms)

            case _:
                raise UnsupportedActionError(action=step.action, step_id=step.id)

    async def _handle_hover(
        self,
        step: HoverStep,
        page: Page,
        context: ExecutionContext,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Handles hovering over an interactive element."""
        loc = resolve_locator(page, step.target).first
        await loc.wait_for(state="visible", timeout=timeout_ms)
        if await loc.is_disabled():
            raise ElementDisabledError(
                message=f"Element target '{step.target}' is disabled and cannot be hovered",
                step_id=step.id,
                details={"target": str(step.target)},
            )
        await loc.hover(force=step.force, timeout=timeout_ms)
        return {"hovered": True}

    async def _handle_scroll(
        self,
        step: ScrollStep,
        page: Page,
        context: ExecutionContext,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Handles scrolling element into view or scrolling the container/page."""
        if step.target is not None:
            loc = resolve_locator(page, step.target).first
            await loc.scroll_into_view_if_needed(timeout=timeout_ms)
            return {"scrolled_to_target": True}
        else:
            direction = step.direction or "down"
            delta_x = 0
            delta_y = 0
            if direction == "down":
                delta_y = step.amount
            elif direction == "up":
                delta_y = -step.amount
            elif direction == "right":
                delta_x = step.amount
            elif direction == "left":
                delta_x = -step.amount
            await page.mouse.wheel(delta_x, delta_y)
            return {"scrolled": True, "direction": direction, "amount": step.amount}

    async def _handle_press_key(
        self,
        step: PressKeyStep,
        page: Page,
        context: ExecutionContext,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Handles sending keyboard key events to element or active page."""
        if step.target is not None:
            loc = resolve_locator(page, step.target).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            if await loc.is_disabled():
                raise ElementDisabledError(
                    message=f"Element target '{step.target}' is disabled",
                    step_id=step.id,
                    details={"target": str(step.target)},
                )
            await loc.press(step.key, timeout=timeout_ms)
        else:
            await page.keyboard.press(step.key)
        return {"pressed_key": step.key, "side_effect": step.side_effect}

    async def _handle_wait_for_state(
        self,
        step: WaitForStateStep,
        page: Page,
        context: ExecutionContext,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Handles waiting for element UI state (visible, hidden, attached, detached)."""
        loc = resolve_locator(page, step.target).first
        await loc.wait_for(state=step.state, timeout=timeout_ms)
        return {"waited_for_state": step.state}

    async def _handle_notify_user(
        self,
        step: NotifyUserStep,
        page: Page,
        context: ExecutionContext,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Handles user notification step without external network leakage."""
        logger.info("Notification [%s]: %s", step.level.upper(), step.message)
        return {"notified": True, "level": step.level, "message": step.message}

    async def _handle_request_human_action(
        self,
        step: RequestHumanActionStep,
        page: Page,
        context: ExecutionContext,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Handles pausing execution and requesting human intervention."""
        logger.warning("Human action requested: %s", step.prompt)
        return {"human_action_requested": True, "prompt": step.prompt}

    async def _handle_navigate(
        self,
        step: NavigateStep,
        page: Page,
        context: ExecutionContext,
        timeout_ms: int,
        provider: str,
    ) -> dict[str, Any]:
        """Handles navigation with strict pre-check and post-check runtime redirect validation."""
        # Resolve target URL
        url = f"{context.base_url}{step.path}" if step.path else step.url or step.target_url or ""

        # Pre-navigation policy check
        decision = self.policy_evaluator.evaluate_action(
            provider=provider,
            action="navigate",
            target_url=url,
            actor="action_interpreter",
        )
        if not decision.allowed:
            raise RuntimePolicyViolationError(
                message=decision.reason,
                step_id=step.id,
                details={"target_url": redact_url_credentials(url)},
            )

        # Execute navigation
        response = await page.goto(url, timeout=timeout_ms, wait_until="load")
        context.last_response = response

        # Post-navigation redirect check: verify final origin remains within allowlist
        current_url = page.url
        provider_policy = self.policy_evaluator.policies.get(provider)
        if provider_policy:
            is_valid, reason, _ = self.policy_evaluator.validate_static_url(
                current_url,
                provider_policy.domain_allowlist,
                allowed_ports=provider_policy.allowed_ports,
            )
            if not is_valid:
                raise RuntimePolicyViolationError(
                    message=(
                        f"Navigation redirected to prohibited or out-of-allowlist destination: "
                        f"{redact_url_credentials(current_url)} ({reason})"
                    ),
                    step_id=step.id,
                    details={"destination_url": redact_url_credentials(current_url)},
                )

        status_code = response.status if response else 200
        if status_code == 401:
            raise StepVerificationError(
                message=(
                    f"Navigation received HTTP 401 Unauthorized from "
                    f"{redact_url_credentials(current_url)}"
                ),
                code="HTTP_401_UNAUTHORIZED",
                step_id=step.id,
                details={"status_code": 401, "error_code": "HTTP_401_UNAUTHORIZED"},
            )
        elif status_code == 403:
            raise StepVerificationError(
                message=(
                    f"Navigation received HTTP 403 Forbidden from "
                    f"{redact_url_credentials(current_url)}"
                ),
                code="HTTP_403_FORBIDDEN",
                step_id=step.id,
                details={"status_code": 403, "error_code": "HTTP_403_FORBIDDEN"},
            )
        elif status_code == 429:
            retry_after = (response.headers.get("retry-after") if response else "2") or "2"
            raise StepVerificationError(
                message=f"Navigation received HTTP 429 Rate Limited (Retry-After: {retry_after}s)",
                code="HTTP_429_RATE_LIMITED",
                step_id=step.id,
                details={
                    "status_code": 429,
                    "retry_after": retry_after,
                    "error_code": "HTTP_429_RATE_LIMITED",
                },
            )
        elif status_code == 504:
            raise StepVerificationError(
                message=(
                    f"Navigation received HTTP 504 Gateway Timeout from "
                    f"{redact_url_credentials(current_url)}"
                ),
                code="HTTP_504_GATEWAY_TIMEOUT",
                step_id=step.id,
                details={"status_code": 504, "error_code": "HTTP_504_GATEWAY_TIMEOUT"},
            )
        elif status_code >= 500:
            raise StepVerificationError(
                message=(
                    f"Navigation received HTTP {status_code} Server Error from "
                    f"{redact_url_credentials(current_url)}"
                ),
                code=f"HTTP_{status_code}_SERVER_ERROR",
                step_id=step.id,
                details={
                    "status_code": status_code,
                    "error_code": f"HTTP_{status_code}_SERVER_ERROR",
                },
            )

        return {
            "navigated_url": redact_url_credentials(current_url),
            "status_code": status_code,
        }

    async def _handle_verify_state(
        self,
        step: VerifyStateStep,
        page: Page,
        context: ExecutionContext,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Handles state verification across rewardlab_api, dom, and response sources."""
        if step.source == "rewardlab_api":
            # Determine API endpoint
            step_ep = getattr(step, "endpoint", None)
            if step_ep and isinstance(step_ep, str):
                endpoint = step_ep.strip()
            elif step.task_id or context.task_id:
                target_task_id = step.task_id or context.task_id
                endpoint = f"/api/accounts/{context.account_id}/tasks/{target_task_id}"
            else:
                endpoint = f"/api/accounts/{context.account_id}"

            # Strict API endpoint validation against SSRF and foreign schemes
            is_valid, reason, _ = self.policy_evaluator.validate_api_endpoint(
                context.base_url, endpoint, context.workflow.provider
            )
            if not is_valid:
                raise RuntimePolicyViolationError(
                    message=f"API verification endpoint violates security policy: {reason}",
                    step_id=step.id,
                    details={"endpoint": endpoint, "reason": reason},
                )

            base = context.base_url.rstrip("/")
            path = endpoint.lstrip("/")
            full_url = f"{base}/{path}"

            # Make loopback query to RewardLab API
            client = self._http_client
            own_client = False
            if client is None:
                client = httpx.AsyncClient(timeout=float(timeout_ms / 1000))
                own_client = True

            try:
                resp = await client.get(full_url)
                if resp.status_code == 401:
                    raise StepVerificationError(
                        message=f"RewardLab API returned HTTP 401 Unauthorized on {endpoint}",
                        code="HTTP_401_UNAUTHORIZED",
                        step_id=step.id,
                        details={"status_code": 401, "error_code": "HTTP_401_UNAUTHORIZED"},
                    )
                elif resp.status_code == 403:
                    raise StepVerificationError(
                        message=f"RewardLab API returned HTTP 403 Forbidden on {endpoint}",
                        code="HTTP_403_FORBIDDEN",
                        step_id=step.id,
                        details={"status_code": 403, "error_code": "HTTP_403_FORBIDDEN"},
                    )
                elif resp.status_code == 429:
                    retry_after = resp.headers.get("retry-after", "2")
                    raise StepVerificationError(
                        message=f"RewardLab API returned HTTP 429 Rate Limited on {endpoint}",
                        code="HTTP_429_RATE_LIMITED",
                        step_id=step.id,
                        details={
                            "status_code": 429,
                            "retry_after": retry_after,
                            "error_code": "HTTP_429_RATE_LIMITED",
                        },
                    )
                elif resp.status_code != 200:
                    raise StepVerificationError(
                        message=f"RewardLab API returned HTTP {resp.status_code} on {endpoint}",
                        code=f"HTTP_{resp.status_code}",
                        step_id=step.id,
                        details={"status_code": resp.status_code, "body": resp.text[:200]},
                    )
                data = resp.json()
            finally:
                if own_client:
                    await client.aclose()

            actual_val = data.get(step.field)
            if actual_val != step.equals:
                raise StepVerificationError(
                    message=(
                        f"State verification failed for field '{step.field}': "
                        f"expected {step.equals!r}, got {actual_val!r}"
                    ),
                    step_id=step.id,
                    details={
                        "field": step.field,
                        "expected": step.equals,
                        "actual": actual_val,
                    },
                )
            return {"verified": True, "field": step.field, "value": actual_val}

        elif step.source == "dom":
            if not step.target:
                raise StepVerificationError(
                    message="DOM verification requires a valid target locator",
                    step_id=step.id,
                )
            loc = resolve_locator(page, step.target).first
            await loc.wait_for(state="visible", timeout=timeout_ms)
            if step.field == "text":
                actual_val = (await loc.inner_text(timeout=timeout_ms)).strip()
            else:
                actual_val = await loc.get_attribute(step.field, timeout=timeout_ms)

            if actual_val != step.equals:
                msg = f"DOM state verification failed: expected {step.equals!r}, got {actual_val!r}"
                raise StepVerificationError(
                    message=msg,
                    step_id=step.id,
                    details={"field": step.field, "expected": step.equals, "actual": actual_val},
                )
            return {"verified": True, "field": step.field, "value": actual_val}

        elif step.source == "response":
            if context.last_response is None:
                raise StepVerificationError(
                    message="No preceding HTTP response available to verify",
                    step_id=step.id,
                )
            actual_val = getattr(context.last_response, step.field, None)
            if actual_val != step.equals:
                raise StepVerificationError(
                    message=(
                        f"Response verification failed for '{step.field}': "
                        f"expected {step.equals!r}, got {actual_val!r}"
                    ),
                    step_id=step.id,
                    details={"field": step.field, "expected": step.equals, "actual": actual_val},
                )
            return {"verified": True, "field": step.field, "value": actual_val}

        else:
            raise StepVerificationError(
                message=f"Unsupported verification source '{step.source}'",
                step_id=step.id,
            )
