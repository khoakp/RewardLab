"""Repository layer for async database persistence and transactions (Milestone M3.1).

Enforces:
- Atomicity across database transactions
- Valid state machine transitions for jobs
- Per-account single active job mutual exclusion
- Sanitized audit logging
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, desc, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import (
    AccountAuthState,
    AccountExecutionLockModel,
    AccountModel,
    AuditEventModel,
    JobModel,
    JobStatus,
    JobStepModel,
    ProviderModel,
    SessionRefModel,
    SessionStatus,
    StepExecutionStatus,
    WorkflowApprovalStatus,
    WorkflowVersionModel,
    generate_uuid,
    utc_now,
)


class ProviderRepository:
    """Persistence operations for providers."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_or_update(
        self,
        provider_id: str,
        adapter_kind: str,
        policy_version: int = 1,
        capabilities: list[str] | None = None,
    ) -> ProviderModel:
        prov = await self.get_by_id(provider_id)
        if prov is None:
            prov = ProviderModel(
                id=provider_id,
                adapter_kind=adapter_kind,
                policy_version=policy_version,
                capabilities_json=capabilities or [],
            )
            self.session.add(prov)
        else:
            prov.adapter_kind = adapter_kind
            prov.policy_version = policy_version
            prov.capabilities_json = capabilities or []
            prov.updated_at = utc_now()
        await self.session.flush()
        return prov

    async def get_by_id(self, provider_id: str) -> ProviderModel | None:
        stmt = select(ProviderModel).where(ProviderModel.id == provider_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_all(self) -> list[ProviderModel]:
        stmt = select(ProviderModel).order_by(ProviderModel.id)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())


class AccountRepository:
    """Persistence operations for accounts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_account(
        self,
        account_id: str,
        provider_id: str,
        owner_id: str,
        label: str,
        auth_state: str = AccountAuthState.ACTIVE,
    ) -> AccountModel:
        acc = AccountModel(
            id=account_id or generate_uuid(),
            provider_id=provider_id,
            owner_id=owner_id,
            label=label,
            auth_state=auth_state,
        )
        self.session.add(acc)
        await self.session.flush()
        return acc

    async def get_by_id(self, account_id: str) -> AccountModel | None:
        stmt = select(AccountModel).where(AccountModel.id == account_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def find_by_provider_and_label(
        self, provider_id: str, owner_id: str, label: str
    ) -> AccountModel | None:
        stmt = select(AccountModel).where(
            AccountModel.provider_id == provider_id,
            AccountModel.owner_id == owner_id,
            AccountModel.label == label,
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_provider(self, provider_id: str) -> list[AccountModel]:
        stmt = select(AccountModel).where(AccountModel.provider_id == provider_id)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())


class SessionRefRepository:
    """Persistence operations for encrypted session pointers."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_session_ref(
        self,
        account_id: str,
        vault_ref: str,
        key_version: int = 1,
        expires_at: datetime | None = None,
        session_ref_id: str | None = None,
        encrypted_payload: str | None = None,
        status: str = SessionStatus.ACTIVE,
    ) -> SessionRefModel:
        ref = SessionRefModel(
            id=session_ref_id or generate_uuid(),
            account_id=account_id,
            vault_ref=vault_ref,
            key_version=key_version,
            expires_at=expires_at,
            encrypted_payload=encrypted_payload,
            status=status,
        )
        self.session.add(ref)
        await self.session.flush()
        return ref

    async def get_active_session_ref(self, account_id: str) -> SessionRefModel | None:
        stmt = (
            select(SessionRefModel)
            .where(
                SessionRefModel.account_id == account_id,
                SessionRefModel.status == SessionStatus.ACTIVE,
            )
            .order_by(desc(SessionRefModel.created_at))
            .limit(1)
        )
        res = await self.session.execute(stmt)
        ref = res.scalar_one_or_none()
        if ref and ref.expires_at:
            exp = ref.expires_at if ref.expires_at.tzinfo else ref.expires_at.replace(tzinfo=UTC)
            if exp <= datetime.now(UTC):
                ref.status = SessionStatus.EXPIRED
                await self.session.flush()
                return None
        return ref

    async def revoke_session_ref(
        self, ref_id: str, revoked_at: datetime | None = None
    ) -> SessionRefModel | None:
        """Revokes a session reference immediately."""
        stmt = select(SessionRefModel).where(SessionRefModel.id == ref_id)
        res = await self.session.execute(stmt)
        ref = res.scalar_one_or_none()
        if ref:
            ref.status = SessionStatus.REVOKED
            ref.revoked_at = revoked_at or utc_now()
            await self.session.flush()
        return ref

    async def invalidate_session_ref(self, ref_id: str) -> SessionRefModel | None:
        """Marks a session reference as invalid (server-rejected or auth expired)."""
        stmt = select(SessionRefModel).where(SessionRefModel.id == ref_id)
        res = await self.session.execute(stmt)
        ref = res.scalar_one_or_none()
        if ref:
            ref.status = SessionStatus.INVALID
            await self.session.flush()
        return ref

    async def list_session_refs_for_account(
        self, account_id: str
    ) -> list[SessionRefModel]:
        """Lists all session references for an account in reverse chronological order."""
        stmt = (
            select(SessionRefModel)
            .where(SessionRefModel.account_id == account_id)
            .order_by(desc(SessionRefModel.created_at))
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())


class WorkflowVersionRepository:
    """Persistence operations for immutable workflow definitions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_version(
        self,
        workflow_key: str,
        version: int,
        provider_id: str,
        definition_json: dict[str, Any],
        schema_version: int = 1,
        approval_status: str = WorkflowApprovalStatus.DRAFT,
        version_id: str | None = None,
    ) -> WorkflowVersionModel:
        wf = WorkflowVersionModel(
            id=version_id or generate_uuid(),
            workflow_key=workflow_key,
            version=version,
            provider_id=provider_id,
            schema_version=schema_version,
            definition_json=definition_json,
            approval_status=approval_status,
            is_immutable=(approval_status == WorkflowApprovalStatus.APPROVED),
        )
        self.session.add(wf)
        await self.session.flush()
        return wf

    async def get_by_key_and_version(
        self, workflow_key: str, version: int
    ) -> WorkflowVersionModel | None:
        stmt = select(WorkflowVersionModel).where(
            WorkflowVersionModel.workflow_key == workflow_key,
            WorkflowVersionModel.version == version,
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_latest_version(self, workflow_key: str) -> WorkflowVersionModel | None:
        stmt = (
            select(WorkflowVersionModel)
            .where(WorkflowVersionModel.workflow_key == workflow_key)
            .order_by(desc(WorkflowVersionModel.version))
            .limit(1)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_id(self, version_id: str) -> WorkflowVersionModel | None:
        stmt = select(WorkflowVersionModel).where(WorkflowVersionModel.id == version_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def approve_version(self, version_id: str) -> WorkflowVersionModel:
        stmt = select(WorkflowVersionModel).where(WorkflowVersionModel.id == version_id)
        res = await self.session.execute(stmt)
        wf = res.scalar_one_or_none()
        if wf is None:
            raise ValueError(f"Workflow version '{version_id}' not found")
        wf.approval_status = WorkflowApprovalStatus.APPROVED
        wf.is_immutable = True
        await self.session.flush()
        return wf


class JobRepository:
    """Persistence operations for jobs, enforcing idempotency and state transitions."""

    ACTIVE_STATUSES = {
        JobStatus.PENDING,
        JobStatus.VALIDATING,
        JobStatus.QUEUED,
        JobStatus.LEASED,
        JobStatus.RUNNING,
    }

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_job(
        self,
        workflow_version_id: str,
        account_id: str,
        idempotency_key: str,
        scheduled_at: datetime | None = None,
        max_attempts: int = 1,
        job_id: str | None = None,
    ) -> JobModel:
        job = JobModel(
            id=job_id or generate_uuid(),
            workflow_version_id=workflow_version_id,
            account_id=account_id,
            status=JobStatus.PENDING,
            idempotency_key=idempotency_key,
            scheduled_at=scheduled_at or utc_now(),
            max_attempts=max_attempts,
            attempt=1,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_by_id(self, job_id: str) -> JobModel | None:
        stmt = select(JobModel).where(JobModel.id == job_id)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_by_idempotency_key(self, idempotency_key: str) -> JobModel | None:
        stmt = select(JobModel).where(JobModel.idempotency_key == idempotency_key)
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def get_active_job_for_account(self, account_id: str) -> JobModel | None:
        """Retrieves active job for an account to enforce single-concurrency mutual exclusion."""
        stmt = (
            select(JobModel)
            .where(
                JobModel.account_id == account_id,
                JobModel.status.in_([s.value for s in self.ACTIVE_STATUSES]),
            )
            .limit(1)
        )
        res = await self.session.execute(stmt)
        return res.scalar_one_or_none()

    async def cleanup_expired_locks(self, account_id: str | None = None) -> int:
        """Cleans up expired account execution locks."""
        now = utc_now()
        check_stmt = select(AccountExecutionLockModel.account_id).where(
            AccountExecutionLockModel.expires_at.is_not(None),
            AccountExecutionLockModel.expires_at <= now,
        )
        if account_id:
            check_stmt = check_stmt.where(AccountExecutionLockModel.account_id == account_id)
        has_expired = (await self.session.execute(check_stmt.limit(1))).scalar_one_or_none()
        if not has_expired:
            return 0

        stmt = delete(AccountExecutionLockModel).where(
            AccountExecutionLockModel.expires_at.is_not(None),
            AccountExecutionLockModel.expires_at <= now,
        )
        if account_id:
            stmt = stmt.where(AccountExecutionLockModel.account_id == account_id)
        res = await self.session.execute(stmt)
        await self.session.flush()
        return res.rowcount or 0

    async def acquire_account_execution_lock(
        self,
        account_id: str,
        job_id: str,
        owner_id: str,
        expires_at: datetime | None = None,
    ) -> bool:
        """Attempts to atomically acquire per-account execution lock in the database.

        Guarantees:
        - Exactly one active execution lock per account across all connections/workers.
        - Primary key constraint on account_id ensures zero concurrent runs.
        - Uses begin_nested() to catch IntegrityError without aborting outer transaction.

        Returns:
            True if acquired, False if already held by another job.
        """
        await self.cleanup_expired_locks(account_id=account_id)

        try:
            async with self.session.begin_nested():
                lock = AccountExecutionLockModel(
                    account_id=account_id,
                    job_id=job_id,
                    owner_id=owner_id,
                    acquired_at=utc_now(),
                    expires_at=expires_at,
                )
                self.session.add(lock)
                await self.session.flush()
            return True
        except IntegrityError:
            return False

    async def release_account_execution_lock(
        self,
        account_id: str,
        job_id: str | None = None,
    ) -> bool:
        """Releases the per-account execution lock if held.

        If job_id is specified, only releases if the lock is held by that specific job.
        """
        stmt = delete(AccountExecutionLockModel).where(
            AccountExecutionLockModel.account_id == account_id
        )
        if job_id is not None:
            stmt = stmt.where(AccountExecutionLockModel.job_id == job_id)
        res = await self.session.execute(stmt)
        await self.session.flush()
        return (res.rowcount or 0) > 0

    async def get_account_execution_lock(
        self, account_id: str
    ) -> AccountExecutionLockModel | None:
        """Retrieves active execution lock for account, cleaning up if expired."""
        stmt = select(AccountExecutionLockModel).where(
            AccountExecutionLockModel.account_id == account_id
        )
        res = await self.session.execute(stmt)
        lock = res.scalar_one_or_none()
        if lock and lock.expires_at:
            exp = lock.expires_at if lock.expires_at.tzinfo else lock.expires_at.replace(tzinfo=UTC)
            if exp <= datetime.now(UTC):
                await self.session.delete(lock)
                await self.session.flush()
                return None
        return lock

    async def refresh_account_execution_lock(
        self,
        account_id: str,
        job_id: str,
        expires_at: datetime,
    ) -> bool:
        """Extends expiration time for an existing account execution lock."""
        stmt = (
            update(AccountExecutionLockModel)
            .where(
                AccountExecutionLockModel.account_id == account_id,
                AccountExecutionLockModel.job_id == job_id,
            )
            .values(expires_at=expires_at)
        )
        res = await self.session.execute(stmt)
        await self.session.flush()
        return (res.rowcount or 0) > 0

    async def claim_next_job(
        self,
        worker_id: str,
        provider_id: str | None = None,
        lease_duration_seconds: int = 60,
    ) -> JobModel | None:
        """Atomically claims the next eligible queued job for execution.

        Enforces:
        - Job must be in QUEUED status.
        - Scheduled time must be in the past or now.
        - Provider must match (if filtered).
        - Account must NOT have an active execution lock.
        - Acquires account_execution_lock atomically.
        - Updates job status to LEASED with new lease_token and lease_until.
        - Rolls back lock acquisition if another worker won the race for the job.

        Returns:
            Claimed JobModel, or None if no eligible job can be claimed.
        """
        await self.cleanup_expired_locks()

        now = utc_now()
        stmt = (
            select(JobModel)
            .where(
                JobModel.status == JobStatus.QUEUED.value,
                (JobModel.scheduled_at.is_(None)) | (JobModel.scheduled_at <= now),
            )
            .order_by(JobModel.scheduled_at.asc(), JobModel.created_at.asc())
            .limit(10)
        )
        if provider_id is not None:
            stmt = (
                stmt.join(
                    WorkflowVersionModel,
                    JobModel.workflow_version_id == WorkflowVersionModel.id,
                )
                .where(WorkflowVersionModel.provider_id == provider_id)
            )

        res = await self.session.execute(stmt)
        candidates = list(res.scalars().all())

        for candidate in candidates:
            # Account-level safety hold: skip accounts with any job paused for review
            paused_stmt = (
                select(JobModel.id)
                .where(
                    JobModel.account_id == candidate.account_id,
                    JobModel.status == JobStatus.PAUSED_FOR_REVIEW.value,
                )
                .limit(1)
            )
            has_paused = (await self.session.execute(paused_stmt)).scalar_one_or_none()
            if has_paused is not None:
                continue

            lease_until = now + timedelta(seconds=lease_duration_seconds)
            locked = await self.acquire_account_execution_lock(
                account_id=candidate.account_id,
                job_id=candidate.id,
                owner_id=worker_id,
                expires_at=lease_until,
            )
            if not locked:
                continue

            # Atomically transition job status from QUEUED to LEASED
            update_stmt = (
                update(JobModel)
                .where(
                    JobModel.id == candidate.id,
                    JobModel.status == JobStatus.QUEUED.value,
                )
                .values(
                    status=JobStatus.LEASED.value,
                    lease_owner=worker_id,
                    lease_token=JobModel.lease_token + 1,
                    lease_until=lease_until,
                    updated_at=utc_now(),
                )
            )
            upd_res = await self.session.execute(update_stmt)
            if (upd_res.rowcount or 0) == 0:
                # Race condition: another worker updated this job first
                await self.release_account_execution_lock(
                    account_id=candidate.account_id, job_id=candidate.id
                )
                continue

            await self.session.flush()
            await self.session.refresh(candidate)
            return candidate

        return None

    async def transition_job_status(
        self,
        job_id: str,
        new_status: JobStatus,
        lease_owner: str | None = None,
        lease_until: datetime | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        result_ref: str | None = None,
    ) -> JobModel:
        """Applies validated state machine transition to a job."""
        from app.orchestration.state_machine import validate_transition

        job = await self.get_by_id(job_id)
        if job is None:
            raise ValueError(f"Job '{job_id}' not found")

        current = JobStatus(job.status)
        validate_transition(current, new_status)

        job.status = new_status.value
        job.updated_at = utc_now()

        if new_status == JobStatus.RUNNING and job.started_at is None:
            job.started_at = utc_now()
        elif new_status in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.REJECTED,
        ):
            job.finished_at = utc_now()

        # Release account execution lock on terminal states or when paused/retrying
        if new_status in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.REJECTED,
            JobStatus.PAUSED_FOR_REVIEW,
            JobStatus.RETRY_WAIT,
        ):
            await self.release_account_execution_lock(
                account_id=job.account_id, job_id=job.id
            )

        if lease_owner is not None:
            job.lease_owner = lease_owner
            job.lease_token += 1
        if lease_until is not None:
            job.lease_until = lease_until
        if error_code is not None:
            job.error_code = error_code
        if error_message is not None:
            job.error_message = error_message
        if result_ref is not None:
            job.result_ref = result_ref

        await self.session.flush()
        return job

    async def record_step_result(
        self,
        job_id: str,
        step_key: str,
        attempt: int,
        status: StepExecutionStatus,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> JobStepModel:
        """Records or updates step execution status."""
        stmt = select(JobStepModel).where(
            JobStepModel.job_id == job_id,
            JobStepModel.step_key == step_key,
            JobStepModel.attempt == attempt,
        )
        res = await self.session.execute(stmt)
        step = res.scalar_one_or_none()

        if step is None:
            step = JobStepModel(
                id=generate_uuid(),
                job_id=job_id,
                step_key=step_key,
                attempt=attempt,
                status=status.value,
                started_at=started_at or utc_now(),
                finished_at=finished_at,
                error_code=error_code,
                error_message=error_message,
                details_json=details,
            )
            self.session.add(step)
        else:
            step.status = status.value
            step.finished_at = finished_at or utc_now()
            if error_code:
                step.error_code = error_code
            if error_message:
                step.error_message = error_message
            if details:
                step.details_json = details

        await self.session.flush()
        return step

    async def list_steps_for_job(self, job_id: str) -> list[JobStepModel]:
        stmt = (
            select(JobStepModel)
            .where(JobStepModel.job_id == job_id)
            .order_by(JobStepModel.started_at)
        )
        res = await self.session.execute(stmt)
        return list(res.scalars().all())


class AuditEventRepository:
    """Persistence operations for immutable audit logs."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record_event(
        self,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        sanitized_details: dict[str, Any] | None = None,
    ) -> AuditEventModel:
        event = AuditEventModel(
            actor=actor,
            action=action,
            resource=resource,
            outcome=outcome,
            sanitized_details=sanitized_details,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_events(
        self, limit: int = 50, resource: str | None = None
    ) -> list[AuditEventModel]:
        stmt = select(AuditEventModel)
        if resource is not None:
            stmt = stmt.where(AuditEventModel.resource == resource)
        stmt = stmt.order_by(desc(AuditEventModel.created_at)).limit(limit)
        res = await self.session.execute(stmt)
        return list(res.scalars().all())
