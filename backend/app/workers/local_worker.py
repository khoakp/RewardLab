"""Local worker process and bounded execution scheduler (Milestone M3.4).

Invariants:
- Dedicated worker process running decoupled from HTTP request lifecycles.
- Bounded concurrency enforced via asyncio.Semaphore(max_concurrency=2 by default).
- Zero database write transactions held during browser execution (4-phase transaction split).
- Account mutual exclusion at the database level prevents concurrent jobs on the same account.
- Reuses a single shared BrowserManager while isolating BrowserContext per execution.
- Verifies lease tokens and monotonic fencing before recording completions or failures.
- Unverified mutating actions pause execution into PAUSED_FOR_REVIEW with account safety hold.
- Graceful shutdown stops job polling and drains in-flight executions within timeout.
- Unconditionally rejects unauthorized providers (e.g. Microsoft Rewards companion-only).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli import try_auto_authenticate
from app.core.config import settings
from app.core.policy import PolicyEvaluator, default_evaluator
from app.execution.browser_manager import BrowserManager
from app.observability.logging import (
    get_logger,
    redact_data,
    redact_url_credentials,
)
from app.observability.metrics import WorkerMetricsTracker
from app.orchestration.job_service import JobService, ReconciliationReport
from app.persistence.database import get_session_factory
from app.persistence.models import (
    StepExecutionStatus,
    WorkflowApprovalStatus,
    WorkflowVersionModel,
)
from app.security.session_store import SecureSessionStore
from app.workflows.results import (
    WorkflowExecutionResult,
    WorkflowExecutionStatus,
)
from app.workflows.schema import WorkflowDefinition

logger = get_logger("adbap.workers.local_worker")


# =============================================================================
# Detached DTO for Claimed Job
# =============================================================================


@dataclass(frozen=True)
class ClaimedJobInfo:
    """Detached data transfer object carrying job metadata outside of DB sessions."""

    job_id: str
    account_id: str
    workflow_version_id: str
    provider_id: str
    definition_json: dict[str, Any]
    lease_token: int
    attempt: int
    session_token: str | None = None


# =============================================================================
# Local Worker Implementation
# =============================================================================


class LocalWorker:
    """Local asynchronous background worker executing queued jobs with bounded concurrency."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        browser_manager: BrowserManager | None = None,
        worker_id: str | None = None,
        max_concurrency: int = 2,
        poll_interval_seconds: float = 1.0,
        lease_duration_seconds: int = 60,
        provider_filter: str | None = None,
        base_url: str = "http://127.0.0.1:8001",
        policy_evaluator: PolicyEvaluator | None = None,
        drain_timeout_seconds: float = 30.0,
        reconcile_interval_seconds: float = 30.0,
        headless: bool = True,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError(f"max_concurrency must be at least 1, got {max_concurrency}")

        self.session_factory = session_factory
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.max_concurrency = max_concurrency
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_duration_seconds = lease_duration_seconds
        self.provider_filter = provider_filter
        self.base_url = base_url
        self.policy_evaluator = policy_evaluator or default_evaluator
        self.drain_timeout_seconds = drain_timeout_seconds
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self.headless = headless

        self._browser_manager: BrowserManager | None = browser_manager
        self._owns_browser_manager: bool = browser_manager is None
        self._running: bool = False
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(max_concurrency)
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._poll_task: asyncio.Task[None] | None = None
        self._sweeper_task: asyncio.Task[None] | None = None
        self._metrics_tracker: WorkerMetricsTracker = WorkerMetricsTracker()
        self._stats: dict[str, int] = {
            "claimed": 0,
            "succeeded": 0,
            "failed": 0,
            "paused_for_review": 0,
            "cancelled": 0,
        }

    @property
    def is_running(self) -> bool:
        """Returns True if the worker is currently polling or executing."""
        return self._running

    @property
    def active_jobs_count(self) -> int:
        """Returns the number of currently executing jobs."""
        return len(self._active_tasks)

    @property
    def stats(self) -> dict[str, int]:
        """Returns read-only copy of worker execution statistics."""
        return dict(self._stats)

    @property
    def metrics(self) -> dict[str, Any]:
        """Returns consolidated runtime observability metrics."""
        return self._metrics_tracker.get_snapshot(active_jobs_count=self.active_jobs_count)

    @property
    def browser_manager(self) -> BrowserManager:
        """Returns the active BrowserManager instance."""
        if self._browser_manager is None:
            raise RuntimeError("BrowserManager is not initialized. Call start() first.")
        return self._browser_manager

    async def start(self) -> None:
        """Initializes BrowserManager and starts background polling and sweeper."""
        if self._running:
            return

        logger.info(
            "Starting LocalWorker '%s' (max_concurrency=%d, poll_interval=%.1fs, provider=%s)",
            self.worker_id,
            self.max_concurrency,
            self.poll_interval_seconds,
            self.provider_filter or "all",
        )

        if self._browser_manager is None:
            self._browser_manager = BrowserManager(headless=self.headless)
            self._owns_browser_manager = True

        if not self._browser_manager.is_healthy():
            await self._browser_manager.start()

        # 1. Startup crash recovery sweep
        try:
            await self.reconcile()
        except Exception as rec_err:
            logger.warning(
                "Initial reconciliation on startup encountered error: %s",
                redact_url_credentials(str(rec_err)),
            )

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name=f"{self.worker_id}-poll")
        if self.reconcile_interval_seconds > 0:
            self._sweeper_task = asyncio.create_task(
                self._sweeper_loop(), name=f"{self.worker_id}-sweeper"
            )

    async def stop(self) -> None:
        """Stops accepting new jobs, drains active executions, and cleans up resources."""
        if not self._running:
            return

        logger.info(
            "Stopping LocalWorker '%s'; waiting for %d active job(s) to drain (timeout=%.1fs)...",
            self.worker_id,
            len(self._active_tasks),
            self.drain_timeout_seconds,
        )
        self._running = False

        # Cancel polling task
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

        # Cancel sweeper task
        if self._sweeper_task is not None and not self._sweeper_task.done():
            self._sweeper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper_task
            self._sweeper_task = None

        # Drain in-flight jobs gracefully
        if self._active_tasks:
            done, pending = await asyncio.wait(
                self._active_tasks,
                timeout=self.drain_timeout_seconds,
            )
            if pending:
                logger.warning(
                    "Drain timeout exceeded for LocalWorker '%s'; cancelling %d in-flight task(s)",
                    self.worker_id,
                    len(pending),
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        self._active_tasks.clear()

        # Stop browser manager if owned
        if self._owns_browser_manager and self._browser_manager is not None:
            await self._browser_manager.stop()
            self._browser_manager = None

        logger.info(
            "LocalWorker '%s' stopped successfully (stats: %s)",
            self.worker_id,
            self._stats,
        )

    async def reconcile(self) -> ReconciliationReport:
        """Executes a database reconciliation sweep to recover stale jobs and expired locks."""
        async with self.session_factory() as session:
            try:
                job_service = JobService(session)
                report = await job_service.reconcile_stale_jobs(actor=self.worker_id)
                await session.commit()

                if report.total_reconciled > 0:
                    self._metrics_tracker.record_recovered_jobs(report.total_reconciled)
                    logger.info(
                        "Reconciliation sweep by '%s': recovered %d job(s) "
                        "(leased: %d, paused: %d, retried: %d, failed: %d, retry_wait: %d)",
                        self.worker_id,
                        report.total_reconciled,
                        len(report.leased_recovered),
                        len(report.running_paused),
                        len(report.running_retried),
                        len(report.running_failed),
                        len(report.retry_wait_queued),
                    )
                return report
            except BaseException:
                with contextlib.suppress(Exception):
                    await session.rollback()
                raise

    async def _sweeper_loop(self) -> None:
        """Background periodic loop recovering orphaned or stale jobs."""
        while self._running:
            try:
                await asyncio.sleep(self.reconcile_interval_seconds)
                if not self._running:
                    break
                await self.reconcile()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    "Reconciliation sweeper error for worker '%s': %s",
                    self.worker_id,
                    redact_url_credentials(str(exc)),
                )

    async def __aenter__(self) -> LocalWorker:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.stop()

    async def _poll_loop(self) -> None:
        """Continuous background loop claiming jobs within available concurrency slots."""
        while self._running:
            # 1. Acquire execution slot permit before claiming
            try:
                await asyncio.wait_for(
                    self._semaphore.acquire(),
                    timeout=min(self.poll_interval_seconds, 0.5),
                )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if not self._running:
                self._semaphore.release()
                break

            # 2. Claim job inside a short DB transaction (Phase 1)
            claimed_info: ClaimedJobInfo | None = None
            try:
                claimed_info = await self._claim_job_phase()
            except Exception as exc:
                logger.error(
                    "Error during job claim phase for worker '%s': %s",
                    self.worker_id,
                    redact_url_credentials(str(exc)),
                )
                self._semaphore.release()
                await asyncio.sleep(self.poll_interval_seconds)
                continue

            # 3. If no eligible job was available, release slot and back off
            if claimed_info is None:
                self._semaphore.release()
                await asyncio.sleep(self.poll_interval_seconds)
                continue

            # 4. Spawn detached task to execute job under acquired slot permit
            self._stats["claimed"] += 1
            task = asyncio.create_task(
                self._execute_job_slot(claimed_info),
                name=f"job-{claimed_info.job_id}",
            )
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

    async def _execute_job_slot(self, info: ClaimedJobInfo) -> None:
        """Executes a single claimed job and guarantees semaphore permit release."""
        try:
            await self._execute_job(info)
        finally:
            self._semaphore.release()

    async def _claim_job_phase(self) -> ClaimedJobInfo | None:
        """Phase 1: Claim next eligible queued job and acquire account execution lock.

        Opens a short database transaction, claims the job, loads detached metadata,
        commits, and releases the DB connection immediately.
        """
        async with self.session_factory() as session:
            try:
                job_service = JobService(session)
                job = await job_service.claim_job(
                    worker_id=self.worker_id,
                    provider_id=self.provider_filter,
                    lease_duration_seconds=self.lease_duration_seconds,
                    actor=self.worker_id,
                )
                if job is None:
                    await session.rollback()
                    return None

                # Fetch associated workflow version
                stmt = select(WorkflowVersionModel).where(
                    WorkflowVersionModel.id == job.workflow_version_id
                )
                res = await session.execute(stmt)
                wf_model = res.scalar_one_or_none()

                if wf_model is None or wf_model.approval_status != WorkflowApprovalStatus.APPROVED:
                    logger.warning(
                        "Claimed job %s references missing or unapproved workflow version %s",
                        job.id,
                        job.workflow_version_id,
                    )
                    await job_service.fail_job(
                        job_id=job.id,
                        worker_id=self.worker_id,
                        lease_token=job.lease_token,
                        error_code="WORKFLOW_NOT_APPROVED",
                        error_message=(
                            f"Workflow version {job.workflow_version_id} is missing or unapproved"
                        ),
                        actor=self.worker_id,
                    )
                    await session.commit()
                    return None

                # Retrieve active encrypted session token if available
                session_token: str | None = None
                try:
                    store = SecureSessionStore(session)
                    session_data = await store.get_valid_session(
                        account_id=job.account_id,
                        provider_id=wf_model.provider_id,
                    )
                    if session_data is not None:
                        session_token = session_data.session_token
                except Exception as sess_err:
                    logger.debug(
                        "Could not load session for '%s' from SecureSessionStore: %s",
                        job.account_id,
                        redact_url_credentials(str(sess_err)),
                    )

                info = ClaimedJobInfo(
                    job_id=job.id,
                    account_id=job.account_id,
                    workflow_version_id=job.workflow_version_id,
                    provider_id=wf_model.provider_id,
                    definition_json=wf_model.definition_json,
                    lease_token=job.lease_token,
                    attempt=job.attempt,
                    session_token=session_token,
                )
                await session.commit()
                return info
            except BaseException:
                with contextlib.suppress(Exception):
                    await session.rollback()
                raise

    async def _execute_job(self, info: ClaimedJobInfo) -> None:
        """Executes workflow with transaction boundaries strictly decoupled from browser runs.

        Phases:
        - Phase 2: Start transition (LEASED -> RUNNING) in short DB transaction.
        - Phase 3: Playwright workflow execution (ZERO DB connections or locks held).
        - Phase 4: Finalize transition (record steps, complete/fail job) in short DB transaction.
        """
        logger.info(
            "Worker '%s' starting execution of job '%s' (account: %s, provider: %s, attempt: %d)",
            self.worker_id,
            info.job_id,
            info.account_id,
            info.provider_id,
            info.attempt,
        )

        # ---------------------------------------------------------------------
        # Phase 2: Transition LEASED -> RUNNING in short DB transaction
        # ---------------------------------------------------------------------
        started = False
        try:
            async with self.session_factory() as session:
                job_service = JobService(session)
                await job_service.start_job(
                    job_id=info.job_id,
                    worker_id=self.worker_id,
                    lease_token=info.lease_token,
                    actor=self.worker_id,
                )
                await session.commit()
                started = True
        except ValueError as fence_err:
            logger.error(
                "Lease fence check failed starting job '%s': %s",
                info.job_id,
                redact_url_credentials(str(fence_err)),
            )
            return
        except Exception as start_err:
            logger.error(
                "Failed to transition job '%s' to RUNNING: %s",
                info.job_id,
                redact_url_credentials(str(start_err)),
            )
            return

        if not started:
            return

        # ---------------------------------------------------------------------
        # Phase 3: Browser Execution (ZERO DB sessions or write transactions held)
        # ---------------------------------------------------------------------
        result: WorkflowExecutionResult
        start_time = time.monotonic()
        stop_heartbeat = asyncio.Event()
        lost_heartbeat = asyncio.Event()

        async def _heartbeat_loop() -> None:
            renew_interval = max(2.0, self.lease_duration_seconds / 3.0)
            while not stop_heartbeat.is_set():
                try:
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=renew_interval)
                    break
                except TimeoutError:
                    pass
                if stop_heartbeat.is_set():
                    break
                try:
                    async with self.session_factory() as h_session:
                        h_svc = JobService(h_session)
                        renewed = await h_svc.renew_lease(
                            job_id=info.job_id,
                            worker_id=self.worker_id,
                            lease_token=info.lease_token,
                            duration_seconds=self.lease_duration_seconds,
                        )
                        if renewed:
                            await h_session.commit()
                        else:
                            logger.warning(
                                "Worker '%s' lost lease for job '%s' (fenced or expired)!",
                                self.worker_id,
                                info.job_id,
                            )
                            lost_heartbeat.set()
                            break
                except Exception as hb_err:
                    logger.warning(
                        "Error in heartbeat renewal for job '%s': %s",
                        info.job_id,
                        redact_url_credentials(str(hb_err)),
                    )

        heartbeat_task = asyncio.create_task(_heartbeat_loop(), name=f"heartbeat-{info.job_id}")

        try:
            # 1. Evaluate provider policy
            prov_policy = self.policy_evaluator.get_policy(info.provider_id)
            if not prov_policy or not prov_policy.automation_allowed:
                deny_msg = (
                    f"Automation is prohibited for provider '{info.provider_id}'. "
                    "Microsoft Rewards is companion-only and unknown providers are denied."
                )
                logger.warning("Policy denial for job %s: %s", info.job_id, deny_msg)
                result = WorkflowExecutionResult(
                    workflow_id=info.workflow_version_id,
                    provider=info.provider_id,
                    status=WorkflowExecutionStatus.POLICY_DENIED,
                    error_code="POLICY_DENIED",
                    error_message=deny_msg,
                    duration_seconds=0.0,
                    can_retry=False,
                    requires_verification=False,
                )
            else:
                # 2. Parse and validate workflow definition
                wf_def = WorkflowDefinition.model_validate(info.definition_json)

                # 3. Resolve session ID: stored token -> env -> HTTP auto-login
                session_id = info.session_token or os.environ.get("REWARDLAB_SESSION_ID")
                if not session_id and info.provider_id == "rewardlab":
                    session_id = await try_auto_authenticate(
                        base_url=self.base_url,
                        account_id=info.account_id,
                        provider=info.provider_id,
                    )

                # 4. Execute workflow with BrowserManager (isolated context per run)
                timeout_val = wf_def.execution.timeout_seconds if wf_def.execution else None
                browser_task = asyncio.create_task(
                    self.browser_manager.execute_workflow(
                        workflow=wf_def,
                        account_id=info.account_id,
                        session_id=session_id,
                        base_url=self.base_url,
                        context_timeout_seconds=timeout_val,
                        policy_evaluator=self.policy_evaluator,
                    )
                )

                lost_waiter = asyncio.create_task(lost_heartbeat.wait())
                done, _ = await asyncio.wait(
                    [browser_task, lost_waiter],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if lost_waiter in done and lost_heartbeat.is_set():
                    browser_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await browser_task
                    logger.error(
                        "Job '%s' lease lost; aborted browser execution immediately.",
                        info.job_id,
                    )
                    return

                lost_waiter.cancel()
                result = await browser_task

        except asyncio.CancelledError:
            logger.warning("Job '%s' execution cancelled via cancellation signal", info.job_id)
            result = WorkflowExecutionResult(
                workflow_id=info.workflow_version_id,
                provider=info.provider_id,
                status=WorkflowExecutionStatus.CANCELLED,
                error_code="EXECUTION_CANCELLED",
                error_message="Execution cancelled by worker shutdown or user signal",
                duration_seconds=0.0,
                can_retry=False,
                requires_verification=False,
            )
        except Exception as exc:
            clean_err = redact_url_credentials(str(exc))
            logger.exception("Unexpected execution error for job '%s': %s", info.job_id, clean_err)
            result = WorkflowExecutionResult(
                workflow_id=info.workflow_version_id,
                provider=info.provider_id,
                status=WorkflowExecutionStatus.FAILED,
                error_code="SYSTEM_ERROR",
                error_message=clean_err,
                duration_seconds=0.0,
                can_retry=False,
                requires_verification=True,  # Safety hold on unexpected runtime failure
            )
        finally:
            stop_heartbeat.set()
            if not heartbeat_task.done():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

        if lost_heartbeat.is_set():
            logger.error(
                "Job '%s' lease was lost/fenced; dropping stale results without DB write.",
                info.job_id,
            )
            return

        # Record execution duration metric
        duration = max(0.0, time.monotonic() - start_time)
        self._metrics_tracker.record_job_duration(duration)

        # ---------------------------------------------------------------------
        # Phase 4: Finalize (Record steps and update job status in short DB transaction)
        # ---------------------------------------------------------------------
        try:
            async with self.session_factory() as session:
                job_service = JobService(session)

                # Record individual step execution results
                for step_res in result.step_results:
                    st_status_str = step_res.status.value.upper()
                    st_status = (
                        StepExecutionStatus[st_status_str]
                        if st_status_str in StepExecutionStatus.__members__
                        else StepExecutionStatus.FAILED
                    )
                    await job_service.job_repo.record_step_result(
                        job_id=info.job_id,
                        step_key=step_res.step_id,
                        attempt=info.attempt,
                        status=st_status,
                        error_code=step_res.error_code,
                        error_message=(
                            redact_url_credentials(step_res.error_message)
                            if step_res.error_message
                            else None
                        ),
                        details={
                            "action": step_res.action,
                            "duration_ms": step_res.duration_ms,
                            "output_data": redact_data(step_res.output_data),
                        },
                    )

                # Final state transition with lease token fencing check
                if result.status == WorkflowExecutionStatus.SUCCESS:
                    await job_service.complete_job(
                        job_id=info.job_id,
                        worker_id=self.worker_id,
                        lease_token=info.lease_token,
                        result_ref=result.execution_id,
                        actor=self.worker_id,
                    )
                    self._stats["succeeded"] += 1
                    self._metrics_tracker.record_job_outcome("succeeded")
                    logger.info("Job '%s' completed successfully", info.job_id)

                elif result.status == WorkflowExecutionStatus.CANCELLED:
                    await job_service.cancel_job(
                        job_id=info.job_id,
                        reason=redact_url_credentials(result.error_message) or "Cancelled",
                        actor=self.worker_id,
                    )
                    self._stats["cancelled"] += 1
                    self._metrics_tracker.record_job_outcome("cancelled")
                    logger.info("Job '%s' recorded as CANCELLED", info.job_id)

                else:
                    # FAILED, TIMEOUT, or POLICY_DENIED
                    _, category, can_retry = await job_service.fail_job(
                        job_id=info.job_id,
                        worker_id=self.worker_id,
                        lease_token=info.lease_token,
                        error_code=result.error_code or "EXECUTION_FAILED",
                        error_message=redact_url_credentials(result.error_message),
                        requires_verification=result.requires_verification,
                        actor=self.worker_id,
                    )
                    if result.requires_verification:
                        self._stats["paused_for_review"] += 1
                        self._metrics_tracker.record_job_outcome("paused")
                        logger.warning(
                            "Job '%s' placed in PAUSED_FOR_REVIEW "
                            "(side effect ambiguous; account hold active)",
                            info.job_id,
                        )
                    else:
                        self._stats["failed"] += 1
                        self._metrics_tracker.record_job_outcome("failed")
                        logger.info(
                            "Job '%s' transitioned to failure state (category: %s, can_retry: %s)",
                            info.job_id,
                            category.value,
                            can_retry,
                        )

                await session.commit()

        except ValueError as fence_err:
            logger.error(
                "Lease fence violation finalizing job '%s': %s (stale results dropped)",
                info.job_id,
                redact_url_credentials(str(fence_err)),
            )
        except Exception as fin_err:
            logger.exception(
                "Error finalizing job '%s' in database: %s",
                info.job_id,
                redact_url_credentials(str(fin_err)),
            )


# =============================================================================
# CLI Entrypoint for Local Worker
# =============================================================================


def build_worker_parser() -> argparse.ArgumentParser:
    """Configures argument parser for the local worker process."""
    parser = argparse.ArgumentParser(
        prog="run-worker",
        description=(
            "ADBAP Local Worker: Polling and executing queued workflows with bounded concurrency."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Notes:\n"
            "  - Runs decoupled from the API control plane.\n"
            "  - Concurrency defaults to 2 with strict per-account mutual exclusion.\n"
            "  - Browser executions hold zero database locks.\n"
        ),
    )

    parser.add_argument(
        "--concurrency",
        dest="max_concurrency",
        type=int,
        default=2,
        help="Maximum concurrent execution slots (default: 2).",
    )

    parser.add_argument(
        "--poll-interval",
        dest="poll_interval",
        type=float,
        default=1.0,
        help="Polling interval when idle in seconds (default: 1.0).",
    )

    parser.add_argument(
        "--reconcile-interval",
        dest="reconcile_interval",
        type=float,
        default=30.0,
        help="Stale job reconciliation sweep interval in seconds (default: 30.0, 0 to disable).",
    )

    parser.add_argument(
        "--provider",
        dest="provider",
        default=None,
        help="Filter jobs by provider ID (optional).",
    )

    parser.add_argument(
        "--base-url",
        dest="base_url",
        default="http://127.0.0.1:8001",
        help="Target base URL for web application (default: http://127.0.0.1:8001).",
    )

    parser.add_argument(
        "--worker-id",
        dest="worker_id",
        default=None,
        help="Explicit worker identifier (default: auto-generated worker-<uuid>).",
    )

    parser.add_argument(
        "--drain-timeout",
        dest="drain_timeout",
        type=float,
        default=30.0,
        help="Graceful shutdown drain timeout in seconds (default: 30.0).",
    )

    parser.add_argument(
        "--headless",
        dest="headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run Chromium browser in headless mode (default: --headless).",
    )

    parser.add_argument(
        "--database-url",
        dest="database_url",
        default=None,
        help="Database connection URL (defaults to settings.database_url).",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        help="Enable debug logging.",
    )

    return parser


async def run_worker_async(args: argparse.Namespace) -> int:
    """Asynchronous entry point managing worker lifecycle and signal interrupts."""
    db_url = args.database_url or settings.database_url
    session_factory = get_session_factory(db_url)

    worker = LocalWorker(
        session_factory=session_factory,
        worker_id=args.worker_id,
        max_concurrency=args.max_concurrency,
        poll_interval_seconds=args.poll_interval,
        reconcile_interval_seconds=args.reconcile_interval,
        provider_filter=args.provider,
        base_url=args.base_url,
        drain_timeout_seconds=args.drain_timeout,
        headless=args.headless,
    )

    stop_event = asyncio.Event()

    def _signal_handler(*_: Any) -> None:
        logger.info("Termination signal received; initiating worker graceful shutdown...")
        stop_event.set()

    with contextlib.suppress(ValueError, AttributeError):
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

    await worker.start()

    try:
        await stop_event.wait()
    finally:
        await worker.stop()

    return 0


def run_worker_cli(argv: list[str] | None = None) -> int:
    """Synchronous console entrypoint for the local background worker process."""
    raw_args = sys.argv[1:] if argv is None else argv
    parser = build_worker_parser()
    args = parser.parse_args(raw_args)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(run_worker_async(args))
    finally:
        loop.close()


def main() -> None:
    """Console script entrypoint."""
    sys.exit(run_worker_cli())


if __name__ == "__main__":
    main()
