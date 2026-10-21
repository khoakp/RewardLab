"""Job orchestration service coordinating validation, submission, and transitions (M3.3).

Invariants:
- Rejects Microsoft Rewards automation (PolicyDeniedException).
- Idempotent submission: identical request returns existing; conflict raises error.
- Enforces approved workflow status and provider-account match before queueing.
- Atomic job claim acquires account execution lock in SQLite (one run per account).
- Side effects that fail or time out enter PAUSED_FOR_REVIEW and are non-retryable.
- Terminal transitions automatically release the account execution lock.
- Emits structured, sanitized audit events for all lifecycle actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import POLICY_DENIED, PolicyDeniedException
from app.orchestration.state_machine import (
    IdempotencyConflictError,
    classify_failure,
    compute_backoff_seconds,
)
from app.persistence.models import (
    AuditEventModel,
    FailureCategory,
    JobModel,
    JobStatus,
    WorkflowApprovalStatus,
    WorkflowVersionModel,
    utc_now,
)
from app.persistence.repository import (
    AccountRepository,
    AuditEventRepository,
    JobRepository,
    WorkflowVersionRepository,
)


@dataclass
class ReconciliationReport:
    """Consolidated report of a job reconciliation sweep (Milestone M3.5)."""

    leased_recovered: list[str] = field(default_factory=list)
    running_paused: list[str] = field(default_factory=list)
    running_retried: list[str] = field(default_factory=list)
    running_failed: list[str] = field(default_factory=list)
    retry_wait_queued: list[str] = field(default_factory=list)

    @property
    def total_reconciled(self) -> int:
        return (
            len(self.leased_recovered)
            + len(self.running_paused)
            + len(self.running_retried)
            + len(self.running_failed)
            + len(self.retry_wait_queued)
        )

    def __iter__(self):
        """Allows iterating over all recovered job IDs for backward compatibility."""
        all_ids = (
            self.leased_recovered
            + self.running_paused
            + self.running_retried
            + self.running_failed
            + self.retry_wait_queued
        )
        return iter(all_ids)

    def __len__(self) -> int:
        return self.total_reconciled

    def __bool__(self) -> bool:
        return self.total_reconciled > 0


def is_workflow_mutating(definition_json: dict[str, Any] | None) -> bool:
    """Inspects workflow definition to determine if it contains mutating side effects.

    Actions with side effects (or steps explicitly marked side_effect=True)
    must fail-closed into PAUSED_FOR_REVIEW if interrupted to prevent duplicate execution.
    """
    if not definition_json or not isinstance(definition_json, dict):
        return True  # Fail-closed: assume mutating if missing or invalid

    steps = definition_json.get("steps", [])
    if not isinstance(steps, list):
        return True

    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("side_effect") is True:
            return True
        action = str(step.get("action", "")).strip().lower()
        if action in ("click", "fill", "submit", "press"):
            return True
        if action == "press_key":
            key = str(step.get("key", "")).strip().lower()
            if key in ("enter", "numpadenter", "return") or step.get("side_effect") is True:
                return True

    return False


class JobService:
    """Service layer coordinating persistent job lifecycle, locking, and auditing."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.job_repo = JobRepository(session)
        self.account_repo = AccountRepository(session)
        self.workflow_repo = WorkflowVersionRepository(session)
        self.audit_repo = AuditEventRepository(session)

    async def submit_job(
        self,
        workflow_version_id: str,
        account_id: str,
        idempotency_key: str,
        scheduled_at: datetime | None = None,
        max_attempts: int = 1,
        actor: str = "orchestrator",
    ) -> tuple[JobModel, bool]:
        """Submits a job for execution with strict idempotency and validation.

        Returns:
            (JobModel, is_duplicate) tuple.
        """
        # 1. Check existing job by idempotency_key
        existing = await self.job_repo.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            if (
                existing.workflow_version_id == workflow_version_id
                and existing.account_id == account_id
            ):
                await self.audit_repo.record_event(
                    actor=actor,
                    action="job.submit",
                    resource=f"job:{existing.id}",
                    outcome="idempotent_replay",
                    sanitized_details={"idempotency_key": idempotency_key},
                )
                return existing, True
            raise IdempotencyConflictError(
                f"Idempotency key '{idempotency_key}' already used with different parameters."
            )

        # 2. Validate workflow version exists and is approved
        stmt = select(WorkflowVersionModel).where(WorkflowVersionModel.id == workflow_version_id)
        res = await self.session.execute(stmt)
        wf = res.scalar_one_or_none()
        if wf is None:
            raise ValueError(f"Workflow version '{workflow_version_id}' not found")

        # 3. Deny Microsoft Rewards automation unconditionally
        if wf.provider_id == "microsoft_rewards":
            await self.audit_repo.record_event(
                actor=actor,
                action="job.submit",
                resource=f"workflow:{workflow_version_id}",
                outcome="policy_denied",
                sanitized_details={"provider_id": "microsoft_rewards"},
            )
            raise PolicyDeniedException(
                "Microsoft Rewards automation is strictly prohibited. Companion-only.",
                details={"provider_id": "microsoft_rewards", "code": POLICY_DENIED},
            )

        if wf.approval_status != WorkflowApprovalStatus.APPROVED:
            raise ValueError(
                f"Workflow version '{workflow_version_id}' is not approved "
                f"(status: {wf.approval_status})"
            )

        # 4. Validate account exists and matches provider
        account = await self.account_repo.get_by_id(account_id)
        if account is None:
            raise ValueError(f"Account '{account_id}' not found")

        if account.provider_id != wf.provider_id:
            raise ValueError(
                f"Account provider '{account.provider_id}' does not match "
                f"workflow provider '{wf.provider_id}'"
            )

        # 5. Create job with nested savepoint to handle concurrency races
        try:
            async with self.session.begin_nested():
                job = await self.job_repo.create_job(
                    workflow_version_id=workflow_version_id,
                    account_id=account_id,
                    idempotency_key=idempotency_key,
                    scheduled_at=scheduled_at,
                    max_attempts=max_attempts,
                )
                # Validated transition: PENDING -> VALIDATING -> QUEUED
                await self.job_repo.transition_job_status(job.id, JobStatus.VALIDATING)
                await self.job_repo.transition_job_status(job.id, JobStatus.QUEUED)
        except IntegrityError as err:
            # Race condition: another thread/process inserted idempotency_key simultaneously
            existing = await self.job_repo.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.workflow_version_id == workflow_version_id
                    and existing.account_id == account_id
                ):
                    return existing, True
                raise IdempotencyConflictError(
                    f"Idempotency key '{idempotency_key}' conflict on concurrent insert."
                ) from err
            raise

        await self.audit_repo.record_event(
            actor=actor,
            action="job.submit",
            resource=f"job:{job.id}",
            outcome="queued",
            sanitized_details={
                "workflow_version_id": workflow_version_id,
                "account_id": account_id,
                "idempotency_key": idempotency_key,
            },
        )
        return job, False

    async def claim_job(
        self,
        worker_id: str,
        provider_id: str | None = None,
        lease_duration_seconds: int = 60,
        actor: str = "worker",
    ) -> JobModel | None:
        """Claims next eligible queued job, acquiring per-account execution lock."""
        job = await self.job_repo.claim_next_job(
            worker_id=worker_id,
            provider_id=provider_id,
            lease_duration_seconds=lease_duration_seconds,
        )
        if job is not None:
            await self.audit_repo.record_event(
                actor=actor,
                action="job.claim",
                resource=f"job:{job.id}",
                outcome="claimed",
                sanitized_details={
                    "worker_id": worker_id,
                    "account_id": job.account_id,
                    "lease_token": job.lease_token,
                },
            )
            await self.session.commit()
        else:
            await self.session.commit()
        return job

    async def start_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: int,
        actor: str = "worker",
    ) -> JobModel:
        """Transitions claimed job to RUNNING, verifying lease ownership."""
        job = await self.job_repo.get_by_id(job_id)
        if job is None:
            raise ValueError(f"Job '{job_id}' not found")
        if job.lease_owner != worker_id or job.lease_token != lease_token:
            raise ValueError(
                f"Lease fence violation: job {job_id} lease_owner={job.lease_owner}, "
                f"lease_token={job.lease_token} (worker: {worker_id}, token: {lease_token})"
            )

        job = await self.job_repo.transition_job_status(job_id, JobStatus.RUNNING)
        await self.audit_repo.record_event(
            actor=actor,
            action="job.start",
            resource=f"job:{job_id}",
            outcome="running",
            sanitized_details={"worker_id": worker_id, "attempt": job.attempt},
        )
        return job

    async def complete_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: int,
        result_ref: str | None = None,
        actor: str = "worker",
    ) -> JobModel:
        """Marks job as SUCCEEDED and automatically releases account lock."""
        job = await self.job_repo.get_by_id(job_id)
        if job is None:
            raise ValueError(f"Job '{job_id}' not found")
        if job.lease_owner != worker_id or job.lease_token != lease_token:
            raise ValueError(
                f"Lease fence violation on complete: job {job_id} lease_owner={job.lease_owner}, "
                f"lease_token={job.lease_token}"
            )

        job = await self.job_repo.transition_job_status(
            job_id, JobStatus.SUCCEEDED, result_ref=result_ref
        )
        await self.audit_repo.record_event(
            actor=actor,
            action="job.complete",
            resource=f"job:{job_id}",
            outcome="succeeded",
            sanitized_details={"result_ref": result_ref},
        )
        return job

    async def fail_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: int,
        error_code: str,
        error_message: str | None = None,
        requires_verification: bool = False,
        actor: str = "worker",
    ) -> tuple[JobModel, FailureCategory, bool]:
        """Handles job failure with failure classification and backoff.

        If requires_verification is True:
        - Job transitions to PAUSED_FOR_REVIEW.
        - can_retry is False (strictly prohibits automated retry).
        - Account execution lock is released so other workflows can proceed.

        If retryable and attempts remain:
        - Job transitions to RETRY_WAIT with computed backoff delay.
        - Account execution lock is released while waiting.

        Otherwise:
        - Job transitions to FAILED.
        - Account execution lock is released.
        """
        job = await self.job_repo.get_by_id(job_id)
        if job is None:
            raise ValueError(f"Job '{job_id}' not found")
        if job.lease_owner != worker_id or job.lease_token != lease_token:
            raise ValueError(
                f"Lease fence violation on fail: job {job_id} lease_owner={job.lease_owner}, "
                f"lease_token={job.lease_token}"
            )

        category, can_retry = classify_failure(
            error_code, requires_verification=requires_verification
        )

        if requires_verification:
            # Side effect was unverified: pause for review, prohibit retry
            job = await self.job_repo.transition_job_status(
                job_id,
                JobStatus.PAUSED_FOR_REVIEW,
                error_code=error_code,
                error_message=f"[{category.value}] {error_message or ''} (requires review)",
            )
            await self.audit_repo.record_event(
                actor=actor,
                action="job.pause_for_review",
                resource=f"job:{job_id}",
                outcome="paused",
                sanitized_details={
                    "error_code": error_code,
                    "category": category.value,
                    "requires_verification": True,
                },
            )
            return job, category, False

        if can_retry and job.attempt < job.max_attempts:
            backoff_delay = compute_backoff_seconds(job.attempt)
            job.attempt += 1
            job.scheduled_at = utc_now() + timedelta(seconds=backoff_delay)
            job = await self.job_repo.transition_job_status(
                job_id,
                JobStatus.RETRY_WAIT,
                error_code=error_code,
                error_message=error_message,
            )
            await self.audit_repo.record_event(
                actor=actor,
                action="job.retry_wait",
                resource=f"job:{job_id}",
                outcome="retry_scheduled",
                sanitized_details={
                    "error_code": error_code,
                    "category": category.value,
                    "next_attempt": job.attempt,
                    "backoff_seconds": backoff_delay,
                },
            )
            return job, category, True

        # Non-retryable or attempts exhausted -> FAILED
        job = await self.job_repo.transition_job_status(
            job_id,
            JobStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
        )
        await self.audit_repo.record_event(
            actor=actor,
            action="job.fail",
            resource=f"job:{job_id}",
            outcome="failed",
            sanitized_details={"error_code": error_code, "category": category.value},
        )
        return job, category, False

    async def cancel_job(
        self,
        job_id: str,
        reason: str = "Cancelled by user",
        actor: str = "operator",
    ) -> JobModel:
        """Cancels a job and releases its lock."""
        job = await self.job_repo.transition_job_status(
            job_id,
            JobStatus.CANCELLED,
            error_message=reason,
        )
        await self.audit_repo.record_event(
            actor=actor,
            action="job.cancel",
            resource=f"job:{job_id}",
            outcome="cancelled",
            sanitized_details={"reason": reason},
        )
        return job

    async def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_token: int,
        duration_seconds: int = 60,
    ) -> bool:
        """Atomically extends lease expiration if worker and token match and job is still RUNNING.

        Returns True if renewed successfully, False if worker lost lease or job was fenced.
        """
        new_lease_until = utc_now() + timedelta(seconds=duration_seconds)
        stmt = (
            update(JobModel)
            .where(
                JobModel.id == job_id,
                JobModel.lease_owner == worker_id,
                JobModel.lease_token == lease_token,
                JobModel.status == JobStatus.RUNNING.value,
            )
            .values(
                lease_until=new_lease_until,
                updated_at=utc_now(),
            )
        )
        res = await self.session.execute(stmt)
        if (res.rowcount or 0) == 0:
            return False

        job = await self.job_repo.get_by_id(job_id)
        if job and job.account_id:
            await self.job_repo.refresh_account_execution_lock(
                account_id=job.account_id,
                job_id=job_id,
                expires_at=new_lease_until,
            )
        await self.session.flush()
        return True

    async def reconcile_stale_jobs(self, actor: str = "reconciler") -> ReconciliationReport:
        """Sweeps database to recover stale LEASED and RUNNING jobs (Milestone M3.5).

        Invariants:
        1. Stale LEASED jobs (unstarted, zero browser actions executed) return to QUEUED
           with lease_token incremented to fence the dead worker.
        2. Stale RUNNING jobs:
           - Mutating workflows transition to PAUSED_FOR_REVIEW with account safety hold
             preserved (can_retry=False) to prevent duplicate external side effects.
           - Strictly read-only workflows transition to RETRY_WAIT if attempts remain,
             or FAILED if exhausted.
        3. Expired RETRY_WAIT jobs with scheduled_at <= now are promoted to QUEUED.
        4. Account execution locks are released from the transient locks table, while account safety
           holds for PAUSED_FOR_REVIEW remain persistently active in claim_next_job.
        5. Monotonic lease_token increments guarantee that any resurrected worker's writes fail.
        6. Fully idempotent and safe against concurrent reconcilers.
        """
        report = ReconciliationReport()
        await self.job_repo.cleanup_expired_locks()
        now = utc_now()

        # 1. Promote eligible RETRY_WAIT jobs whose backoff timer has elapsed
        stmt_retry = select(JobModel).where(
            JobModel.status == JobStatus.RETRY_WAIT.value,
            (JobModel.scheduled_at.is_(None)) | (JobModel.scheduled_at <= now),
        )
        retry_res = await self.session.execute(stmt_retry)
        for r_job in retry_res.scalars().all():
            upd_retry = (
                update(JobModel)
                .where(
                    JobModel.id == r_job.id,
                    JobModel.status == JobStatus.RETRY_WAIT.value,
                )
                .values(
                    status=JobStatus.QUEUED.value,
                    updated_at=utc_now(),
                )
            )
            upd_res = await self.session.execute(upd_retry)
            if (upd_res.rowcount or 0) > 0:
                report.retry_wait_queued.append(r_job.id)

        # 2. Reconcile stale LEASED jobs (claimed but worker crashed before RUNNING)
        stmt_leased = select(JobModel).where(
            JobModel.status == JobStatus.LEASED.value,
            JobModel.lease_until.is_not(None),
            JobModel.lease_until < now,
        )
        leased_res = await self.session.execute(stmt_leased)
        stale_leased = list(leased_res.scalars().all())

        for j in stale_leased:
            upd = (
                update(JobModel)
                .where(
                    JobModel.id == j.id,
                    JobModel.status == JobStatus.LEASED.value,
                    JobModel.lease_token == j.lease_token,
                )
                .values(
                    status=JobStatus.QUEUED.value,
                    lease_owner=None,
                    lease_token=JobModel.lease_token + 1,  # Fencing increment
                    lease_until=None,
                    updated_at=utc_now(),
                )
            )
            upd_res = await self.session.execute(upd)
            if (upd_res.rowcount or 0) > 0:
                await self.job_repo.release_account_execution_lock(
                    account_id=j.account_id, job_id=j.id
                )
                report.leased_recovered.append(j.id)

        # 3. Reconcile stale RUNNING jobs (worker died during execution)
        stmt_running = select(JobModel).where(
            JobModel.status == JobStatus.RUNNING.value,
            JobModel.lease_until.is_not(None),
            JobModel.lease_until < now,
        )
        running_res = await self.session.execute(stmt_running)
        stale_running = list(running_res.scalars().all())

        for j in stale_running:
            wf = await self.workflow_repo.get_by_id(j.workflow_version_id)
            is_mutating = True
            if wf is not None and wf.definition_json:
                is_mutating = is_workflow_mutating(wf.definition_json)

            if is_mutating:
                # Mutating side effects: Fail-closed into PAUSED_FOR_REVIEW
                upd = (
                    update(JobModel)
                    .where(
                        JobModel.id == j.id,
                        JobModel.status == JobStatus.RUNNING.value,
                        JobModel.lease_token == j.lease_token,
                    )
                    .values(
                        status=JobStatus.PAUSED_FOR_REVIEW.value,
                        lease_owner=None,
                        lease_token=JobModel.lease_token + 1,  # Fencing increment
                        lease_until=None,
                        error_code="WORKER_CRASHED_MID_MUTATION",
                        error_message=(
                            "Worker lease expired while executing potentially mutating workflow. "
                            "Paused for review to prevent duplicate side effects."
                        ),
                        updated_at=utc_now(),
                    )
                )
                upd_res = await self.session.execute(upd)
                if (upd_res.rowcount or 0) > 0:
                    await self.job_repo.release_account_execution_lock(
                        account_id=j.account_id, job_id=j.id
                    )
                    report.running_paused.append(j.id)
            else:
                # Strictly read-only workflow: can retry if attempts remain
                if j.attempt < j.max_attempts:
                    backoff_delay = compute_backoff_seconds(j.attempt)
                    upd = (
                        update(JobModel)
                        .where(
                            JobModel.id == j.id,
                            JobModel.status == JobStatus.RUNNING.value,
                            JobModel.lease_token == j.lease_token,
                        )
                        .values(
                            status=JobStatus.RETRY_WAIT.value,
                            attempt=JobModel.attempt + 1,
                            scheduled_at=utc_now() + timedelta(seconds=backoff_delay),
                            lease_owner=None,
                            lease_token=JobModel.lease_token + 1,  # Fencing increment
                            lease_until=None,
                            error_code="WORKER_CRASHED_READ_ONLY",
                            error_message=(
                                f"Worker lease expired during read-only workflow. "
                                f"Scheduled retry attempt {j.attempt + 1}."
                            ),
                            updated_at=utc_now(),
                        )
                    )
                    upd_res = await self.session.execute(upd)
                    if (upd_res.rowcount or 0) > 0:
                        await self.job_repo.release_account_execution_lock(
                            account_id=j.account_id, job_id=j.id
                        )
                        report.running_retried.append(j.id)
                else:
                    # Retry attempts exhausted -> FAILED
                    upd = (
                        update(JobModel)
                        .where(
                            JobModel.id == j.id,
                            JobModel.status == JobStatus.RUNNING.value,
                            JobModel.lease_token == j.lease_token,
                        )
                        .values(
                            status=JobStatus.FAILED.value,
                            lease_owner=None,
                            lease_token=JobModel.lease_token + 1,
                            lease_until=None,
                            error_code="WORKER_CRASHED_EXHAUSTED",
                            error_message="Worker lease expired and retry attempts exhausted.",
                            finished_at=utc_now(),
                            updated_at=utc_now(),
                        )
                    )
                    upd_res = await self.session.execute(upd)
                    if (upd_res.rowcount or 0) > 0:
                        await self.job_repo.release_account_execution_lock(
                            account_id=j.account_id, job_id=j.id
                        )
                        report.running_failed.append(j.id)

        if report.total_reconciled > 0:
            await self.audit_repo.record_event(
                actor=actor,
                action="jobs.reconcile",
                resource="jobs",
                outcome="reconciled",
                sanitized_details={
                    "total": report.total_reconciled,
                    "leased_recovered": report.leased_recovered,
                    "running_paused": report.running_paused,
                    "running_retried": report.running_retried,
                    "running_failed": report.running_failed,
                    "retry_wait_queued": report.retry_wait_queued,
                },
            )

        await self.session.flush()
        return report

    async def list_jobs(
        self,
        status: JobStatus | str | None = None,
        account_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[JobModel]:
        """Lists jobs with optional status/account filter and pagination."""
        return await self.job_repo.list_jobs(
            status=status,
            account_id=account_id,
            limit=limit,
            offset=offset,
        )

    async def get_job_detail(self, job_id: str) -> dict[str, Any] | None:
        """Retrieves comprehensive, sanitized details of a job for operator inspection."""
        job = await self.job_repo.get_by_id(job_id)
        if job is None:
            return None

        wf = await self.workflow_repo.get_by_id(job.workflow_version_id)
        steps = await self.job_repo.list_steps_for_job(job.id)

        stmt_audit = (
            select(AuditEventModel)
            .where(
                (AuditEventModel.resource == f"job:{job.id}") | (AuditEventModel.resource == job.id)
            )
            .order_by(AuditEventModel.created_at.asc())
        )
        audit_res = await self.session.execute(stmt_audit)
        audit_records = list(audit_res.scalars().all())

        from app.observability.logging import redact_data

        serialized_steps = []
        for s in steps:
            serialized_steps.append(
                {
                    "step_key": s.step_key,
                    "attempt": s.attempt,
                    "status": s.status,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "finished_at": s.finished_at.isoformat() if s.finished_at else None,
                    "error_code": s.error_code,
                    "error_message": s.error_message,
                    "details": redact_data(s.details_json) if s.details_json else None,
                }
            )

        serialized_audits = []
        for a in audit_records:
            serialized_audits.append(
                {
                    "id": a.id,
                    "actor": a.actor,
                    "action": a.action,
                    "outcome": a.outcome,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                    "details": redact_data(a.sanitized_details) if a.sanitized_details else None,
                }
            )

        detail = {
            "job_id": job.id,
            "account_id": job.account_id,
            "provider_id": wf.provider_id if wf else None,
            "workflow_version_id": job.workflow_version_id,
            "workflow_key": wf.workflow_key if wf else None,
            "workflow_version": wf.version if wf else None,
            "status": job.status,
            "is_paused_for_review": job.status == JobStatus.PAUSED_FOR_REVIEW.value,
            "pause_reason": job.error_message
            if job.status == JobStatus.PAUSED_FOR_REVIEW.value
            else None,
            "error_code": job.error_code,
            "error_message": job.error_message,
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "result_ref": job.result_ref,
            "steps": serialized_steps,
            "audit_events": serialized_audits,
        }
        return redact_data(detail)

    async def review_job(
        self,
        job_id: str,
        decision: str,
        reason: str,
        actor: str = "operator",
    ) -> JobModel:
        """Processes an operator review decision for a job in PAUSED_FOR_REVIEW.

        Enforces:
        - Job must currently be in PAUSED_FOR_REVIEW.
        - Allowed decisions: 'cancel' -> CANCELLED, 'reject' -> REJECTED.
        - Reason cannot be empty.
        - Direct transition to SUCCEEDED is prohibited.
        - Records immutable audit event.
        - Transitions job state via state machine.
        """
        clean_dec = decision.strip().lower()
        if clean_dec not in ("cancel", "reject"):
            raise ValueError(
                f"Invalid review decision '{decision}'. Allowed decisions are 'cancel' or 'reject'."
            )

        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("Operator review requires a non-empty reason.")

        job = await self.job_repo.get_by_id(job_id)
        if job is None:
            raise ValueError(f"Job '{job_id}' not found.")

        current_status = JobStatus(job.status)
        if current_status != JobStatus.PAUSED_FOR_REVIEW:
            from app.orchestration.state_machine import IllegalStateTransitionError

            raise IllegalStateTransitionError(
                f"Job '{job_id}' is in status '{current_status.value}', not 'PAUSED_FOR_REVIEW'. "
                "Only paused jobs can be reviewed."
            )

        target_status = JobStatus.CANCELLED if clean_dec == "cancel" else JobStatus.REJECTED

        updated_job = await self.job_repo.transition_job_status(
            job_id=job.id,
            new_status=target_status,
            error_code=f"OPERATOR_{clean_dec.upper()}",
            error_message=f"Operator review decision [{clean_dec.upper()}]: {clean_reason}",
        )

        await self.audit_repo.record_event(
            actor=actor,
            action=f"jobs.review.{clean_dec}",
            resource=f"job:{job.id}",
            outcome=clean_dec,
            sanitized_details={
                "job_id": job.id,
                "account_id": job.account_id,
                "decision": clean_dec,
                "reason": clean_reason,
                "previous_status": JobStatus.PAUSED_FOR_REVIEW.value,
                "target_status": target_status.value,
            },
        )
        await self.session.flush()
        return updated_job
