"""Browser Manager and Chromium lifecycle manager (Milestone M2.3).

Provides a single managed Chromium process lifecycle with ephemeral, isolated
BrowserContexts per execution. Enforces context timeouts, guaranteed resource
cleanup in `finally`, cancellation safety, structured crash/disconnect handling,
and safe diagnostic artifact capture on failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import PolicyDeniedException
from app.core.policy import PolicyEvaluator
from app.execution.diagnostics import DiagnosticManager
from app.observability.logging import get_logger, redact_url_credentials
from app.workflows.errors import (
    BrowserDisconnectedError,
)
from app.workflows.interpreter import WorkflowInterpreter
from app.workflows.parser import parse_workflow_content
from app.workflows.results import (
    WorkflowExecutionResult,
    WorkflowExecutionStatus,
    WorkflowOutcomeType,
)
from app.workflows.schema import WorkflowDefinition
from app.workflows.validator import validate_workflow_definition

logger = get_logger("adbap.execution.browser_manager")


class ViewportConfig(BaseModel):
    """Explicit viewport configuration model."""

    model_config = ConfigDict(extra="forbid")
    width: int = Field(..., ge=240, le=7680)
    height: int = Field(..., ge=240, le=4320)


class BrowserContextConfig(BaseModel):
    """Typed browser context profile configuration (Milestone P1).

    Enables testing desktop and mobile responsive layouts, locales, device scale factors,
    and timezones through official Playwright context APIs.
    """

    model_config = ConfigDict(extra="forbid")
    viewport: ViewportConfig | None = None
    is_mobile: bool = False
    device_scale_factor: float = Field(default=1.0, ge=0.5, le=4.0)
    has_touch: bool = False
    locale: str = Field(default="en-US", min_length=2, max_length=32)
    timezone_id: str = Field(default="UTC", min_length=1, max_length=64)


class BrowserManager:
    """Manages the lifecycle of a Chromium process and short-lived BrowserContexts."""

    def __init__(
        self,
        headless: bool = True,
        launch_args: list[str] | None = None,
        diagnostic_manager: DiagnosticManager | None = None,
    ) -> None:
        self.headless = headless
        if launch_args is not None:
            self.launch_args = launch_args
        else:
            # Maintain standard Chromium OS sandbox by default on Windows/Linux.
            # Sandbox is disabled only if explicitly configured via ADBAP_DISABLE_SANDBOX.
            disable_sandbox = os.environ.get("ADBAP_DISABLE_SANDBOX", "false").lower() in (
                "1",
                "true",
                "yes",
            )
            if disable_sandbox:
                self.launch_args = [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ]
            else:
                self.launch_args = ["--disable-dev-shm-usage"]

        self.diagnostic_manager = diagnostic_manager or DiagnosticManager()

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._active_contexts: set[BrowserContext] = set()
        self._is_running: bool = False
        self._is_disconnected: bool = False
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        """Returns True if the browser manager is active and running."""
        return self._is_running and not self._is_disconnected and self._browser is not None

    @property
    def is_disconnected(self) -> bool:
        """Returns True if the underlying browser disconnected or crashed."""
        return self._is_disconnected

    @property
    def active_contexts_count(self) -> int:
        """Returns the number of currently active BrowserContexts."""
        return len(self._active_contexts)

    @property
    def browser(self) -> Browser | None:
        """Provides direct access to the underlying Browser instance."""
        return self._browser

    def is_healthy(self) -> bool:
        """Checks whether the browser process is currently healthy and connected."""
        if not self._is_running or self._is_disconnected:
            return False
        return not (self._browser is None or not self._browser.is_connected())

    def _on_browser_disconnected(self) -> None:
        """Callback invoked when the Playwright Browser emits 'disconnected'."""
        logger.warning("Chromium browser process disconnected unexpectedly or was closed")
        self._is_disconnected = True
        self._is_running = False

    async def start(self) -> None:
        """Starts Playwright and launches the shared Chromium browser process."""
        async with self._lock:
            if self.is_healthy():
                logger.debug("BrowserManager is already running")
                return

            logger.info(
                "Starting BrowserManager and launching Chromium (headless=%s)",
                self.headless,
            )
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=self.launch_args,
            )
            self._is_running = True
            self._is_disconnected = False

            # Attach disconnect listener
            self._browser.on("disconnected", self._on_browser_disconnected)
            logger.info("Chromium launched successfully (shared browser process ready)")

    async def stop(self) -> None:
        """Closes all active contexts, terminates the browser process, and stops Playwright."""
        async with self._lock:
            logger.info(
                "Stopping BrowserManager; cleaning up %d active context(s)",
                len(self._active_contexts),
            )

            # 1. Close all active contexts
            for ctx in list(self._active_contexts):
                try:
                    await ctx.close()
                except Exception as e:
                    logger.debug("Error closing context during shutdown: %s", e)
            self._active_contexts.clear()

            # 2. Close browser
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception as e:
                    logger.debug("Error closing browser during shutdown: %s", e)
                self._browser = None

            # 3. Stop Playwright
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception as e:
                    logger.debug("Error stopping Playwright during shutdown: %s", e)
                self._playwright = None

            self._is_running = False
            self._is_disconnected = False
            logger.info("BrowserManager stopped successfully")

    async def __aenter__(self) -> BrowserManager:
        if not self.is_healthy():
            await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()

    async def new_context(
        self,
        account_id: str = "test_01",
        session_id: str | None = None,
        base_url: str = "http://127.0.0.1:8001",
        ignore_https_errors: bool = False,
        service_workers: str = "block",
        context_config: BrowserContextConfig | None = None,
        **context_kwargs: Any,
    ) -> BrowserContext:
        """Creates an isolated, short-lived BrowserContext for an account execution.

        Never shares cookies, localStorage, or state across accounts.
        Defaults to service_workers='block' to enforce strict network isolation boundaries.
        Supports typed BrowserContextConfig for responsive viewport and locale testing.
        """
        if not self.is_healthy():
            raise BrowserDisconnectedError(
                message=(
                    "Cannot create BrowserContext: Browser process is not running or disconnected"
                )
            )

        assert self._browser is not None  # Guaranteed by is_healthy()

        profile_kwargs: dict[str, Any] = {}
        if context_config:
            if context_config.viewport is not None:
                profile_kwargs["viewport"] = {
                    "width": context_config.viewport.width,
                    "height": context_config.viewport.height,
                }
            if context_config.is_mobile:
                profile_kwargs["is_mobile"] = context_config.is_mobile
            if context_config.device_scale_factor != 1.0:
                profile_kwargs["device_scale_factor"] = context_config.device_scale_factor
            if context_config.has_touch:
                profile_kwargs["has_touch"] = context_config.has_touch
            if context_config.locale:
                profile_kwargs["locale"] = context_config.locale
            if context_config.timezone_id:
                profile_kwargs["timezone_id"] = context_config.timezone_id

        merged_kwargs = {**profile_kwargs, **context_kwargs}

        context = await self._browser.new_context(
            ignore_https_errors=ignore_https_errors,
            service_workers=service_workers,
            **merged_kwargs,
        )

        # Inject synthetic session cookie if provided
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
                "Injected session cookie into isolated context for account '%s'",
                account_id,
            )

        self._active_contexts.add(context)
        return context

    async def close_context(self, context: BrowserContext) -> None:
        """Safely closes a BrowserContext and deregisters it from active tracking."""
        try:
            await context.close()
        except Exception as e:
            logger.debug("Exception while closing context: %s", e)
        finally:
            self._active_contexts.discard(context)

    @contextlib.asynccontextmanager
    async def session_context(
        self,
        account_id: str = "test_01",
        session_id: str | None = None,
        base_url: str = "http://127.0.0.1:8001",
        ignore_https_errors: bool = False,
        context_config: BrowserContextConfig | None = None,
        **context_kwargs: Any,
    ):
        """Async context manager guaranteeing context creation and cleanup in `finally`."""
        ctx = await self.new_context(
            account_id=account_id,
            session_id=session_id,
            base_url=base_url,
            ignore_https_errors=ignore_https_errors,
            context_config=context_config,
            **context_kwargs,
        )
        try:
            yield ctx
        finally:
            await self.close_context(ctx)

    @staticmethod
    async def get_browser_diagnostics(page: Page) -> dict[str, Any]:
        """Reads official browser context environment attributes for diagnostics and testing."""
        return await page.evaluate(
            """() => ({
                viewport_width: window.innerWidth,
                viewport_height: window.innerHeight,
                device_pixel_ratio: window.devicePixelRatio,
                user_agent: navigator.userAgent,
                language: navigator.language,
                touch_points: navigator.maxTouchPoints || 0,
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            })"""
        )

    async def execute_workflow(
        self,
        workflow: WorkflowDefinition | str | bytes | dict[str, Any],
        account_id: str = "test_01",
        task_id: str | None = None,
        session_id: str | None = None,
        base_url: str = "http://127.0.0.1:8001",
        context_timeout_seconds: float | None = None,
        initial_outputs: dict[str, Any] | None = None,
        policy_evaluator: PolicyEvaluator | None = None,
        http_client: httpx.AsyncClient | None = None,
        record_trace_on_failure: bool = True,
        context_config: BrowserContextConfig | None = None,
        is_aborted_callback: Callable[[], bool] | None = None,
    ) -> WorkflowExecutionResult:
        """Executes a validated workflow within an isolated, short-lived BrowserContext.

        Guarantees:
        - Independent context lifecycle per execution
        - Cleanup in finally block under success, failure, timeout, or cancellation
        - No cross-account storage leakage
        - Timeout bound enforcement on context lifecycle
        - Cancellation safety without orphaned contexts
        - Diagnostic artifact capture on failure without secret leakage
        - Zero blind retries after side-effect actions
        """
        evaluator = policy_evaluator or PolicyEvaluator()
        start_time = datetime.now(UTC)

        # 1. Validate workflow definition
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
                outcome_type=WorkflowOutcomeType.PROVIDER_REJECTED,
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

        # 2. Check browser health
        if not self.is_healthy():
            logger.error("Execution rejected: browser is not running or disconnected")
            return WorkflowExecutionResult(
                workflow_id=wf_def.id,
                provider=wf_def.provider,
                status=WorkflowExecutionStatus.FAILED,
                outcome_type=WorkflowOutcomeType.OUTCOME_UNCONFIRMED,
                steps_executed=0,
                total_steps=len(wf_def.steps),
                step_results=[],
                outputs=initial_outputs or {},
                error_code="BROWSER_DISCONNECTED",
                error_message="Browser process is not running or disconnected unexpectedly",
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

        # 3. Create isolated context and execute
        context = await self.new_context(
            account_id=account_id,
            session_id=session_id,
            base_url=base_url,
            service_workers=sw_setting,
            context_config=context_config,
        )

        page = None
        result: WorkflowExecutionResult | None = None
        try:
            page = await context.new_page()

            interpreter = WorkflowInterpreter(
                policy_evaluator=evaluator,
                base_url=base_url,
                http_client=http_client,
            )

            # Determine execution timeout: if context_timeout_seconds provided, enforce it
            exec_coro = interpreter.execute(
                workflow=wf_def,
                page=page,
                account_id=account_id,
                task_id=task_id,
                initial_outputs=initial_outputs,
                is_aborted_callback=is_aborted_callback,
            )

            if context_timeout_seconds is not None and context_timeout_seconds > 0:
                try:
                    result = await asyncio.wait_for(exec_coro, timeout=context_timeout_seconds)
                except TimeoutError:
                    logger.warning(
                        "Context lifecycle timed out after %.1fs for account '%s'",
                        context_timeout_seconds,
                        account_id,
                    )
                    timeout_msg = (
                        f"Browser context lifecycle timed out after {context_timeout_seconds:.1f}s"
                    )
                    result = WorkflowExecutionResult(
                        workflow_id=wf_def.id,
                        provider=wf_def.provider,
                        status=WorkflowExecutionStatus.TIMEOUT,
                        outcome_type=WorkflowOutcomeType.OUTCOME_UNCONFIRMED,
                        steps_executed=0,
                        total_steps=len(wf_def.steps),
                        step_results=[],
                        outputs=initial_outputs or {},
                        error_code="CONTEXT_TIMEOUT",
                        error_message=timeout_msg,
                        started_at=start_time,
                        completed_at=datetime.now(UTC),
                        duration_seconds=context_timeout_seconds,
                        can_retry=False,
                        requires_verification=False,
                    )
            else:
                result = await exec_coro

            # Inspect if any executed step had side effects or was a mutating action (click, fill)
            had_side_effect = False
            for s in result.step_results:
                st_def = next((x for x in wf_def.steps if x.id == s.step_id), None)
                if st_def and s.status in ("SUCCESS", "FAILED", "TIMEOUT"):
                    is_mutating_action = st_def.action in ("click", "fill")
                    is_explicit_side_effect = getattr(st_def, "side_effect", False)
                    if is_explicit_side_effect or is_mutating_action:
                        had_side_effect = True
                        break

            if result.status != WorkflowExecutionStatus.SUCCESS and had_side_effect:
                result.requires_verification = True
                result.can_retry = False

            # Capture diagnostic artifacts on failure
            if record_trace_on_failure and result.status != WorkflowExecutionStatus.SUCCESS:
                artifacts = await self.diagnostic_manager.capture_failure_diagnostic(
                    workflow=wf_def,
                    result=result,
                    page=page,
                    account_id=account_id,
                    task_id=task_id,
                )
                result.artifacts.extend(artifacts)

            return result

        except asyncio.CancelledError:
            logger.warning("Workflow execution cancelled for account '%s'", account_id)
            result = WorkflowExecutionResult(
                workflow_id=wf_def.id,
                provider=wf_def.provider,
                status=WorkflowExecutionStatus.CANCELLED,
                outcome_type=WorkflowOutcomeType.OUTCOME_UNCONFIRMED,
                steps_executed=0,
                total_steps=len(wf_def.steps),
                step_results=[],
                outputs=initial_outputs or {},
                error_code="EXECUTION_CANCELLED",
                error_message="Workflow execution was cancelled via asyncio cancellation",
                started_at=start_time,
                completed_at=datetime.now(UTC),
                duration_seconds=round((datetime.now(UTC) - start_time).total_seconds(), 4),
                can_retry=False,
                requires_verification=False,
            )
            if record_trace_on_failure:
                artifacts = await self.diagnostic_manager.capture_failure_diagnostic(
                    workflow=wf_def,
                    result=result,
                    page=page,
                    account_id=account_id,
                    task_id=task_id,
                )
                result.artifacts.extend(artifacts)
            return result

        except PlaywrightError as pe:
            msg = redact_url_credentials(str(pe))
            lower_msg = msg.lower()
            disconnect_patterns = [
                "target closed",
                "browser closed",
                "connection closed",
            ]
            if any(s in lower_msg for s in disconnect_patterns):
                self._is_disconnected = True
                code = "BROWSER_DISCONNECTED"
            else:
                code = "ACTION_EXECUTION_FAILED"

            result = WorkflowExecutionResult(
                workflow_id=wf_def.id,
                provider=wf_def.provider,
                status=WorkflowExecutionStatus.FAILED,
                outcome_type=WorkflowOutcomeType.OUTCOME_UNCONFIRMED,
                steps_executed=0,
                total_steps=len(wf_def.steps),
                step_results=[],
                outputs=initial_outputs or {},
                error_code=code,
                error_message=msg,
                started_at=start_time,
                completed_at=datetime.now(UTC),
                duration_seconds=round((datetime.now(UTC) - start_time).total_seconds(), 4),
                can_retry=False,
                requires_verification=True,
            )
            if record_trace_on_failure:
                artifacts = await self.diagnostic_manager.capture_failure_diagnostic(
                    workflow=wf_def,
                    result=result,
                    page=page,
                    account_id=account_id,
                    task_id=task_id,
                )
                result.artifacts.extend(artifacts)
            return result

        finally:
            # Guarantee BrowserContext is released under ALL circumstances
            await self.close_context(context)
            logger.info(
                "Closed and released isolated context for account '%s' (active remaining: %d)",
                account_id,
                len(self._active_contexts),
            )


get_browser_diagnostics = BrowserManager.get_browser_diagnostics
