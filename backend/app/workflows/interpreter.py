import asyncio
import contextlib
import time
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
)
from app.workflows.schema import (
    AssertVisibleStep,
    ClickStep,
    ExtractTextStep,
    FillStep,
    NavigateStep,
    VerifyStateStep,
    WaitVisibleStep,
    WorkflowDefinition,
    WorkflowStep,
)

logger = get_logger("adbap.workflows.interpreter")

# Supported action types in Milestone M2.2
SUPPORTED_ACTIONS = {
    "navigate",
    "wait_visible",
    "extract_text",
    "click",
    "fill",
    "assert_visible",
    "verify_state",
}


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

        try:
            for idx, step in enumerate(workflow.steps):
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

        # Inspect if any executed step had side effects or was a state mutation
        had_side_effect = False
        for s_res in step_results:
            step_obj = next((st for st in workflow.steps if st.id == s_res.step_id), None)
            if step_obj:
                is_mutation = getattr(step_obj, "side_effect", False) or step_obj.action in (
                    "click",
                    "fill",
                )
                if is_mutation and s_res.status in (
                    StepStatus.SUCCESS,
                    StepStatus.FAILED,
                    StepStatus.TIMEOUT,
                ):
                    had_side_effect = True
                    break

        requires_verification = (
            had_side_effect if overall_status != WorkflowExecutionStatus.SUCCESS else False
        )

        return WorkflowExecutionResult(
            workflow_id=workflow.id,
            provider=workflow.provider,
            status=overall_status,
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
            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=StepStatus.SUCCESS,
                duration_ms=elapsed_ms,
                output_data=redact_data(output_data) if output_data else None,
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
            return StepExecutionResult(
                step_id=step_id,
                action=action,
                status=StepStatus.FAILED,
                duration_ms=elapsed_ms,
                error_code=e.code,
                error_message=e.message,
            )

        except PlaywrightError as e:
            elapsed_ms = round((time.monotonic() - t0) * 1000, 2)
            sanitized_msg = redact_url_credentials(str(e))
            lower_msg = sanitized_msg.lower()
            # Distinguish element not found/visible, timeout, or browser disconnect
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
                initial_url = page.url
                await loc.click(
                    button=step.button,
                    click_count=step.click_count,
                    timeout=timeout_ms,
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
                if step.clear_before:
                    await loc.fill(step.value, timeout=timeout_ms)
                else:
                    await loc.type(step.value, timeout=timeout_ms)
                # Redact sensitive input values from output record
                return {"filled": True, "length": len(step.value)}

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

            case _:
                raise UnsupportedActionError(action=step.action, step_id=step.id)

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
                if resp.status_code != 200:
                    raise StepVerificationError(
                        message=f"RewardLab API returned HTTP {resp.status_code} on {endpoint}",
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
