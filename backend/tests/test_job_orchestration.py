"""Comprehensive tests for Milestone M3.3: Job State Machine & Atomic Per-Account Locking.

Test coverage:
1. Valid and invalid job state transitions.
2. Terminal state immutability.
3. Failure classification and bounded backoff.
4. Idempotent job submission (exact replay vs conflict exception).
5. Provider policy enforcement (Microsoft Rewards rejected with PolicyDeniedException).
6. Cross-account isolation and provider-account match.
7. File-backed SQLite multi-connection atomic claim on same account (mutual exclusion).
8. File-backed SQLite multi-connection claim on different accounts (concurrency).
9. Transaction rollback safety (no orphaned locks).
10. Process restart and state persistence.
11. Lease fencing (stale token rejection).
12. Side-effect safety (PAUSED_FOR_REVIEW and zero auto-retry on unverified steps).
13. Audit event emission correctness.
14. Secret-free job persistence.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.errors import PolicyDeniedException
from app.orchestration.job_service import JobService
from app.orchestration.state_machine import (
    IdempotencyConflictError,
    IllegalStateTransitionError,
    TerminalStateModificationError,
    classify_failure,
    compute_backoff_seconds,
    validate_transition,
)
from app.persistence.database import Base, set_sqlite_pragma
from app.persistence.models import (
    AccountModel,
    FailureCategory,
    JobModel,
    JobStatus,
    ProviderModel,
    WorkflowApprovalStatus,
    WorkflowVersionModel,
    utc_now,
)
from app.persistence.repository import (
    AccountRepository,
    AuditEventRepository,
    JobRepository,
    ProviderRepository,
    WorkflowVersionRepository,
)

# =============================================================================
# Helper Fixtures
# =============================================================================


@pytest.fixture
async def in_memory_session() -> AsyncSession:
    """Provides an isolated in-memory SQLite session with foreign keys enabled."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    event.listen(Engine, "connect", set_sqlite_pragma)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_maker() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
def file_db_factory(tmp_path: Path):
    """Factory returning async session makers connected to a shared file-backed SQLite database."""
    db_file = tmp_path / "m3_3_concurrency.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    engine = create_async_engine(
        db_url,
        echo=False,
        future=True,
        connect_args={"timeout": 30.0},
    )
    event.listen(Engine, "connect", set_sqlite_pragma)

    session_maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def _init_schema():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    return engine, session_maker, _init_schema


async def seed_test_fixtures(
    session: AsyncSession,
) -> tuple[ProviderModel, AccountModel, WorkflowVersionModel]:
    """Seeds standard RewardLab provider, test account, and approved workflow."""
    prov_repo = ProviderRepository(session)
    prov = await prov_repo.create_or_update(
        provider_id="rewardlab",
        adapter_kind="mock_web",
        capabilities=["read_tasks", "mark_reviewed"],
    )

    acc_repo = AccountRepository(session)
    acc = await acc_repo.create_account(
        account_id="acc_rewardlab_01",
        provider_id="rewardlab",
        owner_id="operator_01",
        label="Test Account 01",
    )

    wf_repo = WorkflowVersionRepository(session)
    wf = await wf_repo.create_version(
        workflow_key="review_tasks",
        version=1,
        provider_id="rewardlab",
        definition_json={"steps": [{"action": "navigate", "path": "/tasks"}]},
        approval_status=WorkflowApprovalStatus.APPROVED,
    )
    await session.commit()
    return prov, acc, wf


# =============================================================================
# 1. State Machine & Transitions Tests
# =============================================================================


def test_valid_transitions_pass():
    """Verifies all valid transitions succeed without exception."""
    validate_transition(JobStatus.PENDING, JobStatus.VALIDATING)
    validate_transition(JobStatus.VALIDATING, JobStatus.QUEUED)
    validate_transition(JobStatus.QUEUED, JobStatus.LEASED)
    validate_transition(JobStatus.LEASED, JobStatus.RUNNING)
    validate_transition(JobStatus.RUNNING, JobStatus.SUCCEEDED)
    validate_transition(JobStatus.RUNNING, JobStatus.FAILED)
    validate_transition(JobStatus.RUNNING, JobStatus.RETRY_WAIT)
    validate_transition(JobStatus.RUNNING, JobStatus.PAUSED_FOR_REVIEW)
    validate_transition(JobStatus.RETRY_WAIT, JobStatus.QUEUED)
    validate_transition(JobStatus.PAUSED_FOR_REVIEW, JobStatus.QUEUED)


def test_invalid_transitions_rejected():
    """Verifies illegal jumps raise IllegalStateTransitionError."""
    with pytest.raises(IllegalStateTransitionError):
        validate_transition(JobStatus.PENDING, JobStatus.RUNNING)

    with pytest.raises(IllegalStateTransitionError):
        validate_transition(JobStatus.QUEUED, JobStatus.SUCCEEDED)

    with pytest.raises(IllegalStateTransitionError):
        validate_transition(JobStatus.LEASED, JobStatus.SUCCEEDED)


def test_terminal_states_immutable():
    """Verifies that terminal states can NEVER transition to any state."""
    terminals = [
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.REJECTED,
    ]
    for terminal in terminals:
        for target in JobStatus:
            with pytest.raises(TerminalStateModificationError):
                validate_transition(terminal, target)


def test_failure_classification_side_effects():
    """Verifies failure on mutating step with requires_verification=True prohibits retry."""
    cat, can_retry = classify_failure("STEP_TIMEOUT", requires_verification=True)
    assert cat == FailureCategory.SIDE_EFFECT_AMBIGUOUS
    assert can_retry is False

    cat, can_retry = classify_failure("STATE_VERIFICATION_FAILED", requires_verification=True)
    assert cat == FailureCategory.SIDE_EFFECT_AMBIGUOUS
    assert can_retry is False


def test_failure_classification_categories():
    """Verifies standard error codes map to appropriate failure categories and retryability."""
    assert classify_failure("POLICY_DENIED") == (FailureCategory.POLICY_DENIED, False)
    assert classify_failure("SCHEMA_VALIDATION_FAILED") == (FailureCategory.VALIDATION_ERROR, False)
    assert classify_failure("AUTH_EXPIRED") == (FailureCategory.AUTH_FAILED, False)
    assert classify_failure("ELEMENT_NOT_FOUND") == (FailureCategory.LOCATOR_NOT_FOUND, True)
    assert classify_failure("STEP_TIMEOUT") == (FailureCategory.ACTION_TIMEOUT, True)
    assert classify_failure("BROWSER_CRASHED") == (FailureCategory.INFRASTRUCTURE_FAILURE, True)


def test_bounded_exponential_backoff():
    """Verifies exponential backoff delay calculation and bounds."""
    assert compute_backoff_seconds(attempt=1, base_seconds=2.0) == 2.0
    assert compute_backoff_seconds(attempt=2, base_seconds=2.0) == 4.0
    assert compute_backoff_seconds(attempt=3, base_seconds=2.0) == 8.0
    assert compute_backoff_seconds(attempt=4, base_seconds=2.0) == 16.0
    # Capped at max_seconds (default 60.0)
    assert compute_backoff_seconds(attempt=10, base_seconds=2.0, max_seconds=60.0) == 60.0


# =============================================================================
# 2. Idempotency & Submission Tests
# =============================================================================


async def test_idempotent_submission_identical_params(in_memory_session: AsyncSession):
    """Submitting twice with same idempotency key and params returns existing job."""
    _, acc, wf = await seed_test_fixtures(in_memory_session)
    service = JobService(in_memory_session)

    job1, is_dup1 = await service.submit_job(
        workflow_version_id=wf.id,
        account_id=acc.id,
        idempotency_key="idemp_key_001",
    )
    assert is_dup1 is False
    assert job1.status == JobStatus.QUEUED.value

    job2, is_dup2 = await service.submit_job(
        workflow_version_id=wf.id,
        account_id=acc.id,
        idempotency_key="idemp_key_001",
    )
    assert is_dup2 is True
    assert job2.id == job1.id


async def test_idempotent_submission_conflict(in_memory_session: AsyncSession):
    """Submitting with same key but different account raises IdempotencyConflictError."""
    _, acc, wf = await seed_test_fixtures(in_memory_session)
    service = JobService(in_memory_session)

    await service.submit_job(
        workflow_version_id=wf.id,
        account_id=acc.id,
        idempotency_key="idemp_key_conflict_01",
    )

    acc_repo = AccountRepository(in_memory_session)
    acc2 = await acc_repo.create_account(
        account_id="acc_rewardlab_02",
        provider_id="rewardlab",
        owner_id="operator_01",
        label="Test Account 02",
    )
    await in_memory_session.commit()

    with pytest.raises(IdempotencyConflictError):
        await service.submit_job(
            workflow_version_id=wf.id,
            account_id=acc2.id,
            idempotency_key="idemp_key_conflict_01",
        )


async def test_microsoft_rewards_automation_rejected(in_memory_session: AsyncSession):
    """Microsoft Rewards workflows are unconditionally rejected with PolicyDeniedException."""
    prov_repo = ProviderRepository(in_memory_session)
    await prov_repo.create_or_update(
        provider_id="microsoft_rewards",
        adapter_kind="companion_only",
    )
    acc_repo = AccountRepository(in_memory_session)
    acc = await acc_repo.create_account(
        account_id="acc_ms_01",
        provider_id="microsoft_rewards",
        owner_id="operator_01",
        label="MS Account",
    )
    wf_repo = WorkflowVersionRepository(in_memory_session)
    wf = await wf_repo.create_version(
        workflow_key="ms_points",
        version=1,
        provider_id="microsoft_rewards",
        definition_json={"steps": []},
        approval_status=WorkflowApprovalStatus.APPROVED,
    )
    await in_memory_session.commit()

    service = JobService(in_memory_session)
    with pytest.raises(PolicyDeniedException) as exc_info:
        await service.submit_job(
            workflow_version_id=wf.id,
            account_id=acc.id,
            idempotency_key="ms_rewards_forbidden",
        )
    assert "strictly prohibited" in str(exc_info.value)

    # Verify policy denial was recorded in audit logs
    audit_repo = AuditEventRepository(in_memory_session)
    events = await audit_repo.list_events(resource=f"workflow:{wf.id}")
    assert len(events) >= 1
    assert events[0].outcome == "policy_denied"


async def test_unapproved_workflow_rejected(in_memory_session: AsyncSession):
    """Draft workflow versions cannot be submitted."""
    _, acc, _ = await seed_test_fixtures(in_memory_session)
    wf_repo = WorkflowVersionRepository(in_memory_session)
    draft_wf = await wf_repo.create_version(
        workflow_key="unapproved_wf",
        version=1,
        provider_id="rewardlab",
        definition_json={"steps": []},
        approval_status=WorkflowApprovalStatus.DRAFT,
    )
    await in_memory_session.commit()

    service = JobService(in_memory_session)
    with pytest.raises(ValueError, match="not approved"):
        await service.submit_job(
            workflow_version_id=draft_wf.id,
            account_id=acc.id,
            idempotency_key="draft_submission",
        )


async def test_provider_mismatch_rejected(in_memory_session: AsyncSession):
    """Account belonging to different provider than workflow is rejected."""
    _, acc, _ = await seed_test_fixtures(in_memory_session)
    prov_repo = ProviderRepository(in_memory_session)
    await prov_repo.create_or_update(provider_id="other_provider", adapter_kind="custom")
    wf_repo = WorkflowVersionRepository(in_memory_session)
    other_wf = await wf_repo.create_version(
        workflow_key="other_wf",
        version=1,
        provider_id="other_provider",
        definition_json={"steps": []},
        approval_status=WorkflowApprovalStatus.APPROVED,
    )
    await in_memory_session.commit()

    service = JobService(in_memory_session)
    with pytest.raises(ValueError, match="does not match"):
        await service.submit_job(
            workflow_version_id=other_wf.id,
            account_id=acc.id,
            idempotency_key="mismatch_submission",
        )


# =============================================================================
# 3. Lifecycle, Lease Fencing & Side-Effect Safety Tests
# =============================================================================


async def test_job_execution_lifecycle_success(in_memory_session: AsyncSession):
    """Full lifecycle: submit -> claim -> start -> complete (verifies lock release)."""
    _, acc, wf = await seed_test_fixtures(in_memory_session)
    service = JobService(in_memory_session)

    job, _ = await service.submit_job(
        workflow_version_id=wf.id,
        account_id=acc.id,
        idempotency_key="life_01",
    )
    assert job.status == JobStatus.QUEUED.value

    # Worker 1 claims
    claimed = await service.claim_job(worker_id="worker_A")
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == JobStatus.LEASED.value
    assert claimed.lease_owner == "worker_A"
    assert claimed.lease_token == 1

    # Verify lock exists in database
    job_repo = JobRepository(in_memory_session)
    lock = await job_repo.get_account_execution_lock(acc.id)
    assert lock is not None
    assert lock.job_id == job.id

    # Worker starts
    started = await service.start_job(
        job_id=job.id, worker_id="worker_A", lease_token=claimed.lease_token
    )
    assert started.status == JobStatus.RUNNING.value
    assert started.started_at is not None

    # Worker completes
    completed = await service.complete_job(
        job_id=job.id,
        worker_id="worker_A",
        lease_token=claimed.lease_token,
        result_ref="results/life_01.json",
    )
    assert completed.status == JobStatus.SUCCEEDED.value
    assert completed.finished_at is not None

    # Verify lock was automatically released on terminal state
    lock_after = await job_repo.get_account_execution_lock(acc.id)
    assert lock_after is None


async def test_lease_fencing_enforcement(in_memory_session: AsyncSession):
    """Stale lease token or mismatched worker cannot complete or fail job."""
    _, acc, wf = await seed_test_fixtures(in_memory_session)
    service = JobService(in_memory_session)

    job, _ = await service.submit_job(
        workflow_version_id=wf.id,
        account_id=acc.id,
        idempotency_key="fence_01",
    )
    claimed = await service.claim_job(worker_id="worker_A")
    assert claimed is not None

    # Impostor worker tries to start
    with pytest.raises(ValueError, match="Lease fence violation"):
        await service.start_job(
            job_id=job.id, worker_id="worker_B", lease_token=claimed.lease_token
        )

    # Wrong lease token tries to complete
    with pytest.raises(ValueError, match="Lease fence violation"):
        await service.complete_job(job_id=job.id, worker_id="worker_A", lease_token=999)


async def test_side_effect_failure_pauses_for_review(in_memory_session: AsyncSession):
    """When a step with requires_verification=True fails, job must pause and prohibit auto-retry."""
    _, acc, wf = await seed_test_fixtures(in_memory_session)
    service = JobService(in_memory_session)

    job, _ = await service.submit_job(
        workflow_version_id=wf.id,
        account_id=acc.id,
        idempotency_key="side_effect_01",
        max_attempts=3,
    )
    claimed = await service.claim_job(worker_id="worker_A")
    assert claimed is not None
    await service.start_job(job_id=job.id, worker_id="worker_A", lease_token=claimed.lease_token)

    # Side-effect step failed
    failed_job, category, can_retry = await service.fail_job(
        job_id=job.id,
        worker_id="worker_A",
        lease_token=claimed.lease_token,
        error_code="STEP_TIMEOUT",
        error_message="Timeout clicking button with side effect",
        requires_verification=True,
    )
    assert failed_job.status == JobStatus.PAUSED_FOR_REVIEW.value
    assert category == FailureCategory.SIDE_EFFECT_AMBIGUOUS
    assert can_retry is False

    # Verify lock was released so account is not permanently blocked
    job_repo = JobRepository(in_memory_session)
    assert await job_repo.get_account_execution_lock(acc.id) is None


async def test_retry_backoff_transitions(in_memory_session: AsyncSession):
    """Retryable failure transitions job to RETRY_WAIT and computes backoff."""
    _, acc, wf = await seed_test_fixtures(in_memory_session)
    service = JobService(in_memory_session)

    job, _ = await service.submit_job(
        workflow_version_id=wf.id,
        account_id=acc.id,
        idempotency_key="retry_01",
        max_attempts=3,
    )
    claimed = await service.claim_job(worker_id="worker_A")
    assert claimed is not None
    await service.start_job(job_id=job.id, worker_id="worker_A", lease_token=claimed.lease_token)

    # First attempt fails with retryable locator error
    retried_job, category, can_retry = await service.fail_job(
        job_id=job.id,
        worker_id="worker_A",
        lease_token=claimed.lease_token,
        error_code="ELEMENT_NOT_FOUND",
        error_message="Element missing",
        requires_verification=False,
    )
    assert retried_job.status == JobStatus.RETRY_WAIT.value
    assert category == FailureCategory.LOCATOR_NOT_FOUND
    assert can_retry is True
    assert retried_job.attempt == 2
    assert retried_job.scheduled_at > utc_now()


# =============================================================================
# 4. Multi-Connection File-Backed SQLite Concurrency Tests
# =============================================================================


async def test_file_db_per_account_mutual_exclusion(file_db_factory):
    """Guarantees two DB connections cannot concurrently claim jobs on the same account."""
    engine, session_maker, init_schema = file_db_factory
    await init_schema()

    async with session_maker() as seed_session:
        await seed_test_fixtures(seed_session)
        service = JobService(seed_session)
        # Seed TWO jobs for the same account
        wf_res = await seed_session.execute(select(WorkflowVersionModel.id))
        wf_id = wf_res.scalar_one()
        job1, _ = await service.submit_job(
            workflow_version_id=wf_id,
            account_id="acc_rewardlab_01",
            idempotency_key="concurrent_same_acc_01",
        )
        job2, _ = await service.submit_job(
            workflow_version_id=wf_id,
            account_id="acc_rewardlab_01",
            idempotency_key="concurrent_same_acc_02",
        )
        await seed_session.commit()

    # Create two completely independent sessions (simulating 2 workers)
    async with session_maker() as conn1_session, session_maker() as conn2_session:
        service1 = JobService(conn1_session)
        service2 = JobService(conn2_session)

        # Worker 1 claims
        claimed_w1 = await service1.claim_job(worker_id="worker_1")
        await conn1_session.commit()

        # Worker 2 tries to claim while Worker 1 still holds the lock
        claimed_w2 = await service2.claim_job(worker_id="worker_2")
        await conn2_session.commit()

        # Exactly one job must be claimed; the second MUST be None because account is locked
        assert claimed_w1 is not None
        assert claimed_w2 is None

        # Worker 1 completes the first job -> releases lock
        await service1.start_job(claimed_w1.id, "worker_1", claimed_w1.lease_token)
        await service1.complete_job(claimed_w1.id, "worker_1", claimed_w1.lease_token)
        await conn1_session.commit()

        # Now Worker 2 can claim the second job!
        claimed_w2_after = await service2.claim_job(worker_id="worker_2")
        await conn2_session.commit()
        assert claimed_w2_after is not None
        assert claimed_w2_after.id == job2.id

    await engine.dispose()


async def test_file_db_multi_account_concurrent_claim(file_db_factory):
    """Guarantees two DB connections CAN claim jobs for different accounts concurrently."""
    engine, session_maker, init_schema = file_db_factory
    await init_schema()

    async with session_maker() as seed_session:
        _, acc1, wf = await seed_test_fixtures(seed_session)
        acc_repo = AccountRepository(seed_session)
        acc2 = await acc_repo.create_account(
            account_id="acc_rewardlab_02",
            provider_id="rewardlab",
            owner_id="operator_01",
            label="Test Account 02",
        )
        service = JobService(seed_session)
        await service.submit_job(
            workflow_version_id=wf.id,
            account_id=acc1.id,
            idempotency_key="diff_acc_01",
        )
        await service.submit_job(
            workflow_version_id=wf.id,
            account_id=acc2.id,
            idempotency_key="diff_acc_02",
        )
        await seed_session.commit()

    async with session_maker() as conn1_session, session_maker() as conn2_session:
        service1 = JobService(conn1_session)
        service2 = JobService(conn2_session)

        # Concurrently claim on independent connections
        claim1_task = service1.claim_job(worker_id="worker_1")
        claim2_task = service2.claim_job(worker_id="worker_2")
        res1, res2 = await asyncio.gather(claim1_task, claim2_task)

        # Both workers succeed because accounts are different
        assert res1 is not None
        assert res2 is not None
        assert res1.account_id != res2.account_id
        assert {res1.account_id, res2.account_id} == {"acc_rewardlab_01", "acc_rewardlab_02"}

    await engine.dispose()


async def test_file_db_transaction_rollback_safety(file_db_factory):
    """Verifies that rolling back a transaction leaves NO orphaned account locks."""
    engine, session_maker, init_schema = file_db_factory
    await init_schema()

    async with session_maker() as session:
        _, acc, wf = await seed_test_fixtures(session)
        service = JobService(session)
        job, _ = await service.submit_job(
            workflow_version_id=wf.id,
            account_id=acc.id,
            idempotency_key="rollback_test_01",
        )
        await session.commit()

    async with session_maker() as session:
        job_repo = JobRepository(session)
        # Manually acquire lock inside transaction
        locked = await job_repo.acquire_account_execution_lock(
            account_id=acc.id,
            job_id=job.id,
            owner_id="test_worker",
        )
        assert locked is True
        # Roll back without committing!
        await session.rollback()

    # Re-open session and verify no lock persisted
    async with session_maker() as session:
        job_repo = JobRepository(session)
        lock = await job_repo.get_account_execution_lock(acc.id)
        assert lock is None, "Rollback must leave no orphaned lock"

    await engine.dispose()


async def test_file_db_process_restart_persistence(file_db_factory):
    """Verifies that job state and lock persist across engine dispose and reopen."""
    engine, session_maker, init_schema = file_db_factory
    await init_schema()

    job_id = ""
    async with session_maker() as session:
        _, acc, wf = await seed_test_fixtures(session)
        service = JobService(session)
        job, _ = await service.submit_job(
            workflow_version_id=wf.id,
            account_id=acc.id,
            idempotency_key="restart_01",
        )
        claimed = await service.claim_job(worker_id="worker_A")
        assert claimed is not None
        job_id = claimed.id
        await session.commit()

    # Dispose engine to simulate process restart / connection termination
    await engine.dispose()

    # Reconnect to the same database file
    db_file = file_db_factory[0].url.database
    new_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", echo=False)
    new_maker = async_sessionmaker(new_engine, expire_on_commit=False, class_=AsyncSession)

    async with new_maker() as session:
        job_repo = JobRepository(session)
        reloaded_job = await job_repo.get_by_id(job_id)
        assert reloaded_job is not None
        assert reloaded_job.status == JobStatus.LEASED.value
        assert reloaded_job.lease_owner == "worker_A"

        # Lock is still active
        lock = await job_repo.get_account_execution_lock("acc_rewardlab_01")
        assert lock is not None
        assert lock.job_id == job_id

    await new_engine.dispose()


# =============================================================================
# 5. Security & Audit Correctness Tests
# =============================================================================


async def test_no_secrets_persisted_in_job_records(in_memory_session: AsyncSession):
    """Verifies that job records never contain passwords, tokens, or auth cookies."""
    _, acc, wf = await seed_test_fixtures(in_memory_session)
    service = JobService(in_memory_session)

    job, _ = await service.submit_job(
        workflow_version_id=wf.id,
        account_id=acc.id,
        idempotency_key="clean_key_01",
    )
    await in_memory_session.commit()

    # Inspect all attributes of JobModel
    job_dict = {
        col.name: getattr(job, col.name)
        for col in JobModel.__table__.columns
    }
    forbidden_substrings = ["password", "token", "cookie", "secret", "bearer"]
    for col_name, val in job_dict.items():
        if col_name in ("lease_token",):  # lease_token is an integer monotonic counter
            continue
        val_str = str(val).lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in val_str, (
                f"Found secret '{forbidden}' in job column '{col_name}': {val_str}"
            )


async def test_audit_event_correctness(in_memory_session: AsyncSession):
    """Verifies that all job actions emit structured, sanitized audit events."""
    _, acc, wf = await seed_test_fixtures(in_memory_session)
    service = JobService(in_memory_session)

    job, _ = await service.submit_job(
        workflow_version_id=wf.id,
        account_id=acc.id,
        idempotency_key="audit_01",
        actor="operator_01",
    )
    claimed = await service.claim_job(worker_id="worker_A", actor="worker_A")
    assert claimed is not None
    await service.start_job(job.id, "worker_A", claimed.lease_token, actor="worker_A")
    await service.complete_job(
        job.id,
        "worker_A",
        claimed.lease_token,
        result_ref="ref.json",
        actor="worker_A",
    )

    audit_repo = AuditEventRepository(in_memory_session)
    events = await audit_repo.list_events(resource=f"job:{job.id}")
    actions = [e.action for e in events]
    assert "job.submit" in actions
    assert "job.claim" in actions
    assert "job.start" in actions
    assert "job.complete" in actions

    for ev in events:
        assert ev.created_at is not None
        assert ev.outcome in ("queued", "claimed", "running", "succeeded")
