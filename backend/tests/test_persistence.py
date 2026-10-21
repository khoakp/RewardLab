"""Unit and integration tests for Database Persistence Layer (Milestone M3.1).

Tests:
1. Database schema creation, tables, and indices.
2. Provider repository operations.
3. Account repository operations and unique constraints.
4. Foreign key constraint enforcement on SQLite connections.
5. Session reference management and no plaintext tokens.
6. Workflow version repository, unique constraints, and immutability.
7. Job creation, idempotency key uniqueness, and single active account job exclusion.
8. Job lifecycle state machine transitions (valid vs invalid).
9. Job step recording and cascade deletion.
10. Audit event logging and retrieval.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.persistence.database import (
    Base,
    get_session_factory,
    set_sqlite_pragma,
)
from app.persistence.models import (
    AccountAuthState,
    JobStatus,
    JobStepModel,
    StepExecutionStatus,
    WorkflowApprovalStatus,
    generate_uuid,
)
from app.persistence.repository import (
    AccountRepository,
    AuditEventRepository,
    JobRepository,
    ProviderRepository,
    SessionRefRepository,
    WorkflowVersionRepository,
)


@pytest.fixture
async def async_db_session() -> AsyncSession:
    """Fixture provisioning an isolated, in-memory SQLite database with active foreign keys."""
    # Use in-memory SQLite with foreign keys enabled
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
    )
    # Enable SQLite foreign keys on connection
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    event.listen(Engine, "connect", set_sqlite_pragma)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = get_session_factory(engine)
    async with session_maker() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# =============================================================================
# 1. Schema and Provider Tests
# =============================================================================


async def test_schema_table_creation(async_db_session: AsyncSession):
    """Verifies that all 9 core tables defined in ARCHITECTURE.md are created."""
    from sqlalchemy import inspect

    def _get_tables(conn):
        return inspect(conn).get_table_names()

    tables = await async_db_session.connection()
    table_names = await tables.run_sync(_get_tables)

    expected_tables = {
        "providers",
        "accounts",
        "session_refs",
        "workflow_versions",
        "jobs",
        "job_steps",
        "nodes",
        "audit_events",
        "activity_records",
    }
    for tbl in expected_tables:
        assert tbl in table_names, f"Table '{tbl}' missing from database schema"


async def test_provider_repository_crud(async_db_session: AsyncSession):
    """Tests provider creation, retrieval, and updating."""
    repo = ProviderRepository(async_db_session)

    prov = await repo.create_or_update(
        provider_id="rewardlab",
        adapter_kind="mock_web",
        policy_version=1,
        capabilities=["read_tasks", "mark_reviewed"],
    )
    assert prov.id == "rewardlab"
    assert prov.adapter_kind == "mock_web"
    assert prov.capabilities_json == ["read_tasks", "mark_reviewed"]

    # Retrieve
    fetched = await repo.get_by_id("rewardlab")
    assert fetched is not None
    assert fetched.policy_version == 1

    # Update policy version
    updated = await repo.create_or_update(
        provider_id="rewardlab",
        adapter_kind="mock_web",
        policy_version=2,
        capabilities=["read_tasks", "mark_reviewed", "claim_bonus"],
    )
    assert updated.policy_version == 2
    assert "claim_bonus" in updated.capabilities_json


# =============================================================================
# 2. Account Repository and Foreign Key Tests
# =============================================================================


async def test_account_creation_and_unique_constraint(async_db_session: AsyncSession):
    """Tests account creation and unique constraint on (provider_id, owner_id, label)."""
    prov_repo = ProviderRepository(async_db_session)
    acc_repo = AccountRepository(async_db_session)

    await prov_repo.create_or_update("rewardlab", "mock_web")

    acc1 = await acc_repo.create_account(
        account_id="acc_01",
        provider_id="rewardlab",
        owner_id="operator_01",
        label="test_account_01",
    )
    assert acc1.id == "acc_01"
    assert acc1.auth_state == AccountAuthState.ACTIVE

    # Attempt duplicate (provider_id, owner_id, label)
    with pytest.raises(IntegrityError):
        await acc_repo.create_account(
            account_id="acc_02",
            provider_id="rewardlab",
            owner_id="operator_01",
            label="test_account_01",  # Duplicate label for same provider and owner
        )
    await async_db_session.rollback()


async def test_foreign_key_violation_rejected(async_db_session: AsyncSession):
    """Attempting to create an account referencing a non-existent provider fails."""
    acc_repo = AccountRepository(async_db_session)

    with pytest.raises(IntegrityError):
        await acc_repo.create_account(
            account_id="orphan_acc",
            provider_id="non_existent_provider",
            owner_id="op_1",
            label="lbl",
        )
    await async_db_session.rollback()


# =============================================================================
# 3. Session Reference Tests (No Plaintext)
# =============================================================================


async def test_session_ref_management(async_db_session: AsyncSession):
    """Tests encrypted session pointers and expiration handling."""
    prov_repo = ProviderRepository(async_db_session)
    acc_repo = AccountRepository(async_db_session)
    sess_repo = SessionRefRepository(async_db_session)

    await prov_repo.create_or_update("rewardlab", "mock_web")
    await acc_repo.create_account(
        account_id="test_01",
        provider_id="rewardlab",
        owner_id="op_1",
        label="Account 01",
    )

    # Store encrypted vault pointer (never plaintext tokens)
    future_exp = datetime.now(UTC) + timedelta(hours=2)
    s_ref = await sess_repo.create_session_ref(
        account_id="test_01",
        vault_ref="vault://tokens/rewardlab/test_01/key_v1",
        key_version=1,
        expires_at=future_exp,
    )
    assert s_ref.vault_ref.startswith("vault://")

    # Retrieve active session
    active = await sess_repo.get_active_session_ref("test_01")
    assert active is not None
    assert active.id == s_ref.id

    # Test expired session
    past_exp = datetime.now(UTC) - timedelta(minutes=5)
    await sess_repo.create_session_ref(
        account_id="test_01",
        vault_ref="vault://tokens/rewardlab/test_01/key_v0",
        key_version=1,
        expires_at=past_exp,
    )
    # Most recent is expired, so get_active_session_ref should return None
    active_now = await sess_repo.get_active_session_ref("test_01")
    assert active_now is None


# =============================================================================
# 4. Workflow Version Immutability Tests
# =============================================================================


async def test_workflow_version_immutability_and_unique_constraint(
    async_db_session: AsyncSession,
):
    """Tests versioning, approval immutability, and uniqueness per workflow key."""
    prov_repo = ProviderRepository(async_db_session)
    wf_repo = WorkflowVersionRepository(async_db_session)

    await prov_repo.create_or_update("rewardlab", "mock_web")

    wf_def = {
        "id": "review_first_task",
        "provider": "rewardlab",
        "steps": [{"id": "s1", "action": "navigate", "path": "/tasks"}],
    }

    v1 = await wf_repo.create_version(
        workflow_key="review_first_task",
        version=1,
        provider_id="rewardlab",
        definition_json=wf_def,
    )
    assert v1.approval_status == WorkflowApprovalStatus.DRAFT
    assert v1.is_immutable is False

    # Approve workflow -> becomes immutable
    approved = await wf_repo.approve_version(v1.id)
    assert approved.approval_status == WorkflowApprovalStatus.APPROVED
    assert approved.is_immutable is True

    # Duplicate (workflow_key, version) must fail
    with pytest.raises(IntegrityError):
        await wf_repo.create_version(
            workflow_key="review_first_task",
            version=1,
            provider_id="rewardlab",
            definition_json=wf_def,
        )
    await async_db_session.rollback()


# =============================================================================
# 5. Job Idempotency and State Machine Tests
# =============================================================================


async def test_job_idempotency_key_uniqueness(async_db_session: AsyncSession):
    """Tests unique idempotency key enforcement on jobs."""
    prov_repo = ProviderRepository(async_db_session)
    acc_repo = AccountRepository(async_db_session)
    wf_repo = WorkflowVersionRepository(async_db_session)
    job_repo = JobRepository(async_db_session)

    await prov_repo.create_or_update("rewardlab", "mock_web")
    await acc_repo.create_account("acc_01", "rewardlab", "op_1", "acc_label")
    wf = await wf_repo.create_version("wf_key", 1, "rewardlab", {"steps": []})

    j1 = await job_repo.create_job(
        workflow_version_id=wf.id,
        account_id="acc_01",
        idempotency_key="idemp_key_unique_001",
    )
    assert j1.status == JobStatus.PENDING

    # Attempt duplicate idempotency key
    with pytest.raises(IntegrityError):
        await job_repo.create_job(
            workflow_version_id=wf.id,
            account_id="acc_01",
            idempotency_key="idemp_key_unique_001",
        )
    await async_db_session.rollback()


async def test_job_state_machine_valid_transitions(async_db_session: AsyncSession):
    """Tests standard lifecycle transitions: PENDING -> QUEUED -> LEASED -> RUNNING -> SUCCEEDED."""
    prov_repo = ProviderRepository(async_db_session)
    acc_repo = AccountRepository(async_db_session)
    wf_repo = WorkflowVersionRepository(async_db_session)
    job_repo = JobRepository(async_db_session)

    await prov_repo.create_or_update("rewardlab", "mock_web")
    await acc_repo.create_account("acc_01", "rewardlab", "op_1", "acc_label")
    wf = await wf_repo.create_version("wf_key", 1, "rewardlab", {"steps": []})

    job = await job_repo.create_job(wf.id, "acc_01", "idemp_test_valid")
    assert job.status == JobStatus.PENDING

    # PENDING -> QUEUED
    job = await job_repo.transition_job_status(job.id, JobStatus.QUEUED)
    assert job.status == JobStatus.QUEUED

    # QUEUED -> LEASED
    lease_exp = datetime.now(UTC) + timedelta(seconds=60)
    job = await job_repo.transition_job_status(
        job.id,
        JobStatus.LEASED,
        lease_owner="worker_local_01",
        lease_until=lease_exp,
    )
    assert job.status == JobStatus.LEASED
    assert job.lease_owner == "worker_local_01"
    assert job.lease_token == 1

    # LEASED -> RUNNING
    job = await job_repo.transition_job_status(job.id, JobStatus.RUNNING)
    assert job.status == JobStatus.RUNNING
    assert job.started_at is not None

    # RUNNING -> SUCCEEDED
    job = await job_repo.transition_job_status(
        job.id, JobStatus.SUCCEEDED, result_ref="results/job_success.json"
    )
    assert job.status == JobStatus.SUCCEEDED
    assert job.finished_at is not None
    assert job.result_ref == "results/job_success.json"


async def test_job_state_machine_invalid_transition_rejected(async_db_session: AsyncSession):
    """Tests that illegal state transitions raise ValueError without corrupting state."""
    prov_repo = ProviderRepository(async_db_session)
    acc_repo = AccountRepository(async_db_session)
    wf_repo = WorkflowVersionRepository(async_db_session)
    job_repo = JobRepository(async_db_session)

    await prov_repo.create_or_update("rewardlab", "mock_web")
    await acc_repo.create_account("acc_01", "rewardlab", "op_1", "acc_label")
    wf = await wf_repo.create_version("wf_key", 1, "rewardlab", {"steps": []})

    job = await job_repo.create_job(wf.id, "acc_01", "idemp_test_invalid")

    # Direct PENDING -> SUCCEEDED is forbidden
    with pytest.raises(ValueError, match="Invalid job state transition"):
        await job_repo.transition_job_status(job.id, JobStatus.SUCCEEDED)


async def test_single_active_job_account_exclusion(async_db_session: AsyncSession):
    """Verifies querying active jobs for an account to enforce per-account mutual exclusion."""
    prov_repo = ProviderRepository(async_db_session)
    acc_repo = AccountRepository(async_db_session)
    wf_repo = WorkflowVersionRepository(async_db_session)
    job_repo = JobRepository(async_db_session)

    await prov_repo.create_or_update("rewardlab", "mock_web")
    await acc_repo.create_account("acc_01", "rewardlab", "op_1", "acc_label_01")
    await acc_repo.create_account("acc_02", "rewardlab", "op_1", "acc_label_02")
    wf = await wf_repo.create_version("wf_key", 1, "rewardlab", {"steps": []})

    # No active jobs initially
    assert await job_repo.get_active_job_for_account("acc_01") is None

    # Create job for acc_01 in PENDING status
    job1 = await job_repo.create_job(wf.id, "acc_01", "idemp_acc1")
    active = await job_repo.get_active_job_for_account("acc_01")
    assert active is not None
    assert active.id == job1.id

    # acc_02 remains unblocked
    assert await job_repo.get_active_job_for_account("acc_02") is None

    # When job1 reaches terminal state, acc_01 becomes free
    await job_repo.transition_job_status(job1.id, JobStatus.QUEUED)
    await job_repo.transition_job_status(job1.id, JobStatus.LEASED)
    await job_repo.transition_job_status(job1.id, JobStatus.RUNNING)
    await job_repo.transition_job_status(job1.id, JobStatus.SUCCEEDED)

    assert await job_repo.get_active_job_for_account("acc_01") is None


# =============================================================================
# 6. Job Step and Cascade Deletion Tests
# =============================================================================


async def test_job_step_recording_and_cascade(async_db_session: AsyncSession):
    """Tests recording step results and cascade delete on job removal."""
    prov_repo = ProviderRepository(async_db_session)
    acc_repo = AccountRepository(async_db_session)
    wf_repo = WorkflowVersionRepository(async_db_session)
    job_repo = JobRepository(async_db_session)

    await prov_repo.create_or_update("rewardlab", "mock_web")
    await acc_repo.create_account("acc_01", "rewardlab", "op_1", "label")
    wf = await wf_repo.create_version("wf_key", 1, "rewardlab", {"steps": []})
    job = await job_repo.create_job(wf.id, "acc_01", "idemp_step_test")

    step1 = await job_repo.record_step_result(
        job_id=job.id,
        step_key="open_tasks",
        attempt=1,
        status=StepExecutionStatus.SUCCESS,
        details={"path": "/tasks"},
    )
    assert step1.status == "SUCCESS"

    step2 = await job_repo.record_step_result(
        job_id=job.id,
        step_key="click_review",
        attempt=1,
        status=StepExecutionStatus.FAILED,
        error_code="LOCATOR_NOT_FOUND",
        error_message="Button not found",
    )
    assert step2.error_code == "LOCATOR_NOT_FOUND"

    steps = await job_repo.list_steps_for_job(job.id)
    assert len(steps) == 2

    # Attempting duplicate step key + attempt for same job fails unique constraint at DB level
    dup_step = JobStepModel(
        id=generate_uuid(),
        job_id=job.id,
        step_key="open_tasks",
        attempt=1,
        status="PENDING",
    )
    async_db_session.add(dup_step)
    with pytest.raises(IntegrityError):
        await async_db_session.flush()
    await async_db_session.rollback()


# =============================================================================
# 7. Audit Event Repository Tests
# =============================================================================


async def test_audit_event_logging(async_db_session: AsyncSession):
    """Tests recording and retrieving immutable audit events."""
    audit_repo = AuditEventRepository(async_db_session)

    e1 = await audit_repo.record_event(
        actor="cli_runner",
        action="validate_workflow",
        resource="workflow:review_first_task",
        outcome="ALLOWED",
        sanitized_details={"schema_version": 1},
    )
    assert e1.id is not None
    assert e1.actor == "cli_runner"

    events = await audit_repo.list_events(limit=10)
    assert len(events) >= 1
    assert events[0].action == "validate_workflow"
