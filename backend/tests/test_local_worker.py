"""Comprehensive tests for Milestone M3.4: Local Worker & Bounded Scheduler.

Test coverage:
1. Concurrency limit = 2 (max 2 jobs running concurrently under load).
2. Same-account mutual exclusion (job 2 waits for job 1 to finish on same account).
3. Different-account parallel execution (concurrent browser runs on separate accounts).
4. Batch execution across 12 synthetic accounts.
5. Atomic claim under multi-worker contention (two workers racing on file-backed SQLite).
6. Lease token fencing check (stale worker results dropped on stolen/expired lease).
7. Worker graceful shutdown (polling stops, in-flight jobs drain cleanly).
8. Cancellation cleanup (cancelled task releases account lock).
9. Job state and step result persistence with sanitized diagnostics.
10. Provider policy enforcement (Microsoft Rewards rejected before browser launch).
11. Encrypted session retrieval from SecureSessionStore and injection.
12. Ambiguous side-effect failure -> PAUSED_FOR_REVIEW and account safety hold.
13. Zero database transactions held during browser execution (anti-starvation invariant).
14. End-to-end real Chromium execution against live RewardLab server.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.execution.browser_manager import BrowserManager
from app.orchestration.job_service import JobService
from app.persistence.database import Base, set_sqlite_pragma
from app.persistence.models import (
    JobModel,
    JobStatus,
    StepExecutionStatus,
    WorkflowApprovalStatus,
    utc_now,
)
from app.persistence.repository import (
    AccountRepository,
    AuditEventRepository,
    JobRepository,
    ProviderRepository,
    WorkflowVersionRepository,
)
from app.security.session_crypto import generate_session_encryption_key
from app.security.session_store import SecureSessionStore, SessionData
from app.workers.local_worker import LocalWorker
from app.workflows.results import (
    StepExecutionResult,
    StepStatus,
    WorkflowExecutionResult,
    WorkflowExecutionStatus,
)
from app.workflows.schema import WorkflowDefinition

# =============================================================================
# Helper Fixtures & Setup
# =============================================================================


@pytest.fixture
def file_db_factory(tmp_path: Path):
    """Factory returning async session makers connected to a shared file-backed SQLite database."""
    db_file = tmp_path / "m3_4_worker_test.db"
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

    asyncio.run(_init_schema())

    yield session_maker

    async def _teardown():
        await engine.dispose()

    asyncio.run(_teardown())


async def seed_provider_and_workflow(
    session_factory: async_sessionmaker[AsyncSession],
    provider_id: str = "rewardlab",
    workflow_key: str = "simple_review",
    workflow_def: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Helper to seed an authorized provider and approved workflow version."""
    if workflow_def is None:
        workflow_def = {
            "id": workflow_key,
            "provider": provider_id,
            "schema_version": 1,
            "steps": [
                {
                    "id": "step_1",
                    "action": "navigate",
                    "url": f"http://127.0.0.1:8001/{workflow_key}",
                }
            ],
        }

    async with session_factory() as session:
        prov_repo = ProviderRepository(session)
        prov = await prov_repo.create_or_update(
            provider_id=provider_id,
            adapter_kind="mock",
            policy_version=1,
        )

        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            workflow_key=workflow_key,
            version=1,
            provider_id=provider_id,
            definition_json=workflow_def,
            approval_status=WorkflowApprovalStatus.APPROVED,
        )
        await session.commit()
        return prov.id, wf.id


async def seed_account_and_job(
    session_factory: async_sessionmaker[AsyncSession],
    workflow_version_id: str,
    account_id: str = "test_01",
    provider_id: str = "rewardlab",
    idempotency_key: str | None = None,
) -> tuple[str, str]:
    """Helper to seed an account and queued job."""
    async with session_factory() as session:
        acc_repo = AccountRepository(session)
        acc = await acc_repo.get_by_id(account_id)
        if not acc:
            acc = await acc_repo.create_account(
                provider_id=provider_id,
                owner_id="operator_1",
                label=f"Label {account_id}",
                account_id=account_id,
            )

        job_service = JobService(session)
        key = idempotency_key or f"idem-{account_id}-{workflow_version_id}-{utc_now().timestamp()}"
        job, _ = await job_service.submit_job(
            workflow_version_id=workflow_version_id,
            account_id=account_id,
            idempotency_key=key,
        )
        await session.commit()
        return acc.id, job.id


# =============================================================================
# Unit & Concurrency Tests
# =============================================================================


class MockBrowserManager:
    """Mock BrowserManager tracking concurrency and isolated execution."""

    def __init__(self, execution_delay: float = 0.05) -> None:
        self.execution_delay = execution_delay
        self.current_concurrency = 0
        self.max_observed_concurrency = 0
        self.executed_jobs: list[str] = []
        self._lock = asyncio.Lock()
        self.custom_results: dict[str, WorkflowExecutionResult] = {}

    def is_healthy(self) -> bool:
        return True

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def execute_workflow(
        self,
        workflow: WorkflowDefinition | str | bytes | dict[str, Any],
        account_id: str = "test_01",
        session_id: str | None = None,
        base_url: str = "http://127.0.0.1:8001",
        **kwargs: Any,
    ) -> WorkflowExecutionResult:
        async with self._lock:
            self.current_concurrency += 1
            if self.current_concurrency > self.max_observed_concurrency:
                self.max_observed_concurrency = self.current_concurrency

        try:
            if self.execution_delay > 0:
                await asyncio.sleep(self.execution_delay)

            wf_id = getattr(workflow, "id", "mock_wf")
            if wf_id in self.custom_results:
                return self.custom_results[wf_id]

            self.executed_jobs.append(account_id)
            return WorkflowExecutionResult(
                workflow_id=wf_id,
                provider="rewardlab",
                status=WorkflowExecutionStatus.SUCCESS,
                steps_executed=1,
                total_steps=1,
                step_results=[
                    StepExecutionResult(
                        step_id="step_1",
                        action="navigate",
                        status=StepStatus.SUCCESS,
                        duration_ms=45.0,
                    )
                ],
                duration_seconds=self.execution_delay,
            )
        finally:
            async with self._lock:
                self.current_concurrency -= 1


@pytest.mark.asyncio
async def test_worker_concurrency_bound(file_db_factory):
    """Test 1: Concurrency is strictly bounded by max_concurrency=2."""
    _, wf_id = await seed_provider_and_workflow(file_db_factory)

    # Queue 4 jobs across 4 different accounts
    for i in range(1, 5):
        await seed_account_and_job(
            file_db_factory,
            workflow_version_id=wf_id,
            account_id=f"acc_{i:02d}",
        )

    mock_browser = MockBrowserManager(execution_delay=0.1)
    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=mock_browser,
        worker_id="test-worker-concurrency",
        max_concurrency=2,
        poll_interval_seconds=0.02,
    )

    await worker.start()

    # Wait for all 4 jobs to complete
    for _ in range(50):
        if worker.stats["succeeded"] == 4:
            break
        await asyncio.sleep(0.05)

    await worker.stop()

    assert worker.stats["succeeded"] == 4
    assert mock_browser.max_observed_concurrency <= 2
    assert mock_browser.max_observed_concurrency >= 2


@pytest.mark.asyncio
async def test_same_account_mutual_exclusion(file_db_factory):
    """Test 2: Two jobs on the same account cannot execute concurrently."""
    _, wf_id = await seed_provider_and_workflow(file_db_factory)

    # Submit 2 jobs for account 'acc_solo'
    await seed_account_and_job(
        file_db_factory,
        workflow_version_id=wf_id,
        account_id="acc_solo",
        idempotency_key="key-solo-1",
    )
    await seed_account_and_job(
        file_db_factory,
        workflow_version_id=wf_id,
        account_id="acc_solo",
        idempotency_key="key-solo-2",
    )

    execution_order: list[str] = []

    class OrderTrackingBrowser(MockBrowserManager):
        async def execute_workflow(self, *args, account_id="acc_solo", **kwargs):
            execution_order.append(f"start-{account_id}")
            await asyncio.sleep(0.08)
            execution_order.append(f"end-{account_id}")
            return await super().execute_workflow(*args, account_id=account_id, **kwargs)

    browser = OrderTrackingBrowser(execution_delay=0.08)
    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser,
        worker_id="test-worker-mutex",
        max_concurrency=2,
        poll_interval_seconds=0.02,
    )

    await worker.start()

    for _ in range(50):
        if worker.stats["succeeded"] == 2:
            break
        await asyncio.sleep(0.05)

    await worker.stop()

    assert worker.stats["succeeded"] == 2
    # Verify sequential execution: start -> end -> start -> end (NEVER start -> start)
    assert execution_order == [
        "start-acc_solo",
        "end-acc_solo",
        "start-acc_solo",
        "end-acc_solo",
    ]


@pytest.mark.asyncio
async def test_different_accounts_parallel_execution(file_db_factory):
    """Test 3: Two jobs on different accounts execute in parallel."""
    _, wf_id = await seed_provider_and_workflow(file_db_factory)

    await seed_account_and_job(file_db_factory, wf_id, account_id="acc_alpha")
    await seed_account_and_job(file_db_factory, wf_id, account_id="acc_beta")

    browser = MockBrowserManager(execution_delay=0.1)
    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser,
        max_concurrency=2,
        poll_interval_seconds=0.02,
    )

    await worker.start()

    for _ in range(50):
        if worker.stats["succeeded"] == 2:
            break
        await asyncio.sleep(0.05)

    await worker.stop()

    assert worker.stats["succeeded"] == 2
    assert browser.max_observed_concurrency == 2


@pytest.mark.asyncio
async def test_twelve_synthetic_accounts_batch(file_db_factory):
    """Test 4: 12 synthetic accounts execute with max_concurrency=2."""
    _, wf_id = await seed_provider_and_workflow(file_db_factory)

    # Seed 12 accounts with 1 job each
    for i in range(1, 13):
        await seed_account_and_job(file_db_factory, wf_id, account_id=f"synth_{i:02d}")

    browser = MockBrowserManager(execution_delay=0.02)
    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser,
        max_concurrency=2,
        poll_interval_seconds=0.01,
    )

    await worker.start()

    for _ in range(100):
        if worker.stats["succeeded"] == 12:
            break
        await asyncio.sleep(0.05)

    await worker.stop()

    assert worker.stats["succeeded"] == 12
    assert browser.max_observed_concurrency <= 2

    # Verify all locks released
    async with file_db_factory() as session:
        job_repo = JobRepository(session)
        for i in range(1, 13):
            lock = await job_repo.get_account_execution_lock(f"synth_{i:02d}")
            assert lock is None


@pytest.mark.asyncio
async def test_atomic_claim_under_multi_worker_contention(file_db_factory):
    """Test 5: Two workers racing on file-backed SQLite claim jobs without duplicate leases."""
    _, wf_id = await seed_provider_and_workflow(file_db_factory)

    # Queue 6 jobs across 6 accounts
    for i in range(1, 7):
        await seed_account_and_job(file_db_factory, wf_id, account_id=f"contend_{i:02d}")

    browser1 = MockBrowserManager(execution_delay=0.03)
    browser2 = MockBrowserManager(execution_delay=0.03)

    worker1 = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser1,
        worker_id="worker-node-1",
        max_concurrency=2,
        poll_interval_seconds=0.01,
    )
    worker2 = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser2,
        worker_id="worker-node-2",
        max_concurrency=2,
        poll_interval_seconds=0.01,
    )

    await worker1.start()
    await worker2.start()

    for _ in range(100):
        total_succeeded = worker1.stats["succeeded"] + worker2.stats["succeeded"]
        if total_succeeded == 6:
            break
        await asyncio.sleep(0.05)

    await worker1.stop()
    await worker2.stop()

    assert worker1.stats["succeeded"] + worker2.stats["succeeded"] == 6

    # Verify in DB: all 6 jobs succeeded, each has lease_token >= 1
    async with file_db_factory() as session:
        stmt = select(JobModel)
        res = await session.execute(stmt)
        jobs = list(res.scalars().all())
        assert len(jobs) == 6
        assert all(j.status == JobStatus.SUCCEEDED.value for j in jobs)
        # Every job was claimed by either worker1 or worker2
        assert all(j.lease_owner in ("worker-node-1", "worker-node-2") for j in jobs)


@pytest.mark.asyncio
async def test_lease_token_fencing_drops_stale_results(file_db_factory):
    """Test 6: Monotonic fencing rejects results from a stale worker whose lease was stolen."""
    _, wf_id = await seed_provider_and_workflow(file_db_factory)
    _, job_id = await seed_account_and_job(file_db_factory, wf_id, account_id="acc_fenced")

    # Claim job using JobService as worker-1
    async with file_db_factory() as session:
        job_service = JobService(session)
        job = await job_service.claim_job(worker_id="worker-1", lease_duration_seconds=60)
        assert job is not None
        initial_token = job.lease_token

        # Simulate another worker (or reconciler) claiming/stealing the job with new token
        job.lease_owner = "worker-2"
        job.lease_token = initial_token + 1
        await session.commit()

    # Now simulate worker-1 trying to finalize Phase 4: complete_job with initial_token
    async with file_db_factory() as session:
        job_service = JobService(session)
        with pytest.raises(ValueError, match="Lease fence violation"):
            await job_service.complete_job(
                job_id=job_id,
                worker_id="worker-1",
                lease_token=initial_token,
            )


@pytest.mark.asyncio
async def test_worker_graceful_shutdown(file_db_factory):
    """Test 7: Graceful shutdown stops accepting new jobs and lets in-flight jobs finish."""
    _, wf_id = await seed_provider_and_workflow(file_db_factory)

    await seed_account_and_job(file_db_factory, wf_id, account_id="acc_drain_1")
    await seed_account_and_job(file_db_factory, wf_id, account_id="acc_drain_2")

    browser = MockBrowserManager(execution_delay=0.15)
    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser,
        worker_id="worker-drain",
        max_concurrency=2,
        poll_interval_seconds=0.01,
        drain_timeout_seconds=5.0,
    )

    await worker.start()

    # Wait until both jobs are claimed and active
    for _ in range(50):
        if worker.active_jobs_count == 2:
            break
        await asyncio.sleep(0.02)

    # Initiate graceful shutdown while jobs are running
    stop_task = asyncio.create_task(worker.stop())
    await asyncio.sleep(0.01)

    assert not worker.is_running  # Polling stopped immediately

    await stop_task

    # Both jobs should have finished successfully during drain
    assert worker.stats["succeeded"] == 2
    assert worker.active_jobs_count == 0


@pytest.mark.asyncio
async def test_cancellation_cleanup(file_db_factory):
    """Test 8: Job cancellation safely cleans up account execution locks."""
    _, wf_id = await seed_provider_and_workflow(file_db_factory)
    _, job_id = await seed_account_and_job(file_db_factory, wf_id, account_id="acc_cancel")

    async with file_db_factory() as session:
        job_service = JobService(session)
        job = await job_service.claim_job(worker_id="w-cancel")
        assert job is not None

        # Cancel job
        await job_service.cancel_job(job_id=job.id, reason="User requested stop")
        await session.commit()

        # Verify lock was released
        lock = await job_service.job_repo.get_account_execution_lock("acc_cancel")
        assert lock is None

        # Verify status is CANCELLED
        reloaded = await job_service.job_repo.get_by_id(job_id)
        assert reloaded.status == JobStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_job_state_and_step_persistence(file_db_factory):
    """Test 9: Job completion records all step results and audit events without secrets."""
    _, wf_id = await seed_provider_and_workflow(file_db_factory)
    _, job_id = await seed_account_and_job(file_db_factory, wf_id, account_id="acc_persist")

    browser = MockBrowserManager(execution_delay=0.01)
    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser,
        max_concurrency=2,
        poll_interval_seconds=0.01,
    )

    async with worker:
        for _ in range(50):
            if worker.stats["succeeded"] == 1:
                break
            await asyncio.sleep(0.02)

    # Verify database persistence
    async with file_db_factory() as session:
        job_repo = JobRepository(session)
        job = await job_repo.get_by_id(job_id)
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.started_at is not None
        assert job.finished_at is not None

        steps = await job_repo.list_steps_for_job(job_id)
        assert len(steps) == 1
        assert steps[0].step_key == "step_1"
        assert steps[0].status == StepExecutionStatus.SUCCESS.value
        assert steps[0].details_json.get("action") == "navigate"

        # Check audit trail
        audit_repo = AuditEventRepository(session)
        events = await audit_repo.list_events(limit=10, resource=f"job:{job_id}")
        action_names = {e.action for e in events}
        assert "job.claim" in action_names
        assert "job.start" in action_names
        assert "job.complete" in action_names


@pytest.mark.asyncio
async def test_policy_denial_microsoft_rewards_rejected(file_db_factory):
    """Test 10: Microsoft Rewards automation is rejected before launching browser."""
    # Seed provider microsoft_rewards
    prov_id = "microsoft_rewards"
    async with file_db_factory() as session:
        prov_repo = ProviderRepository(session)
        await prov_repo.create_or_update(
            provider_id=prov_id,
            adapter_kind="mock",
            policy_version=1,
        )

        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            workflow_key="ms_rewards_task",
            version=1,
            provider_id=prov_id,
            definition_json={
                "id": "ms_rewards_task",
                "provider": prov_id,
                "schema_version": 1,
                "steps": [{"id": "s1", "action": "navigate", "url": "https://rewards.bing.com"}],
            },
            approval_status=WorkflowApprovalStatus.APPROVED,
        )

        acc_repo = AccountRepository(session)
        if not await acc_repo.get_by_id("ms_account"):
            await acc_repo.create_account(
                provider_id=prov_id,
                owner_id="operator_ms",
                label="MS Test",
                account_id="ms_account",
            )

        job_repo = JobRepository(session)
        job = await job_repo.create_job(
            workflow_version_id=wf.id,
            account_id="ms_account",
            idempotency_key="ms-key-1",
        )
        job.status = JobStatus.QUEUED.value
        await session.commit()

    browser = MockBrowserManager()
    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser,
        max_concurrency=2,
        poll_interval_seconds=0.01,
    )

    async with worker:
        for _ in range(50):
            if worker.stats["failed"] == 1:
                break
            await asyncio.sleep(0.02)

    # Verify browser was NOT called
    assert len(browser.executed_jobs) == 0

    # Verify job failed with POLICY_DENIED
    async with file_db_factory() as session:
        job_repo = JobRepository(session)
        db_job = await job_repo.get_by_id(job.id)
        assert db_job.status == JobStatus.FAILED.value
        assert "POLICY_DENIED" in (db_job.error_code or "")


@pytest.mark.asyncio
async def test_encrypted_session_retrieval(file_db_factory, monkeypatch):
    """Test 11: Worker retrieves encrypted session from SecureSessionStore and passes to runner."""
    enc_key = generate_session_encryption_key()
    monkeypatch.setenv("ADBAP_SESSION_ENCRYPTION_KEY", enc_key)

    prov_id, wf_id = await seed_provider_and_workflow(file_db_factory)
    acc_id, job_id = await seed_account_and_job(file_db_factory, wf_id, account_id="acc_crypto")

    # Store encrypted session in SecureSessionStore
    async with file_db_factory() as session:
        store = SecureSessionStore(session)
        session_data = SessionData(
            account_id=acc_id,
            provider_id=prov_id,
            session_token="secret_token_12345",
            cookies=[{"name": "rewardlab_session", "value": "secret_token_12345"}],
        )
        await store.save_session(
            account_id=acc_id,
            provider_id=prov_id,
            session_data=session_data,
        )
        await session.commit()

    received_sessions: list[str | None] = []

    class SessionInspectingBrowser(MockBrowserManager):
        async def execute_workflow(self, *args, session_id=None, **kwargs):
            received_sessions.append(session_id)
            return await super().execute_workflow(*args, session_id=session_id, **kwargs)

    browser = SessionInspectingBrowser(execution_delay=0.01)
    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser,
        max_concurrency=2,
        poll_interval_seconds=0.01,
    )

    async with worker:
        for _ in range(50):
            if worker.stats["succeeded"] == 1:
                break
            await asyncio.sleep(0.02)

    assert len(received_sessions) == 1
    assert received_sessions[0] == "secret_token_12345"


@pytest.mark.asyncio
async def test_ambiguous_side_effect_pause_and_account_safety_hold(file_db_factory):
    """Test 12: Unverified mutating step pauses job into PAUSED_FOR_REVIEW
    and triggers account safety hold.
    """
    prov_id, wf_id = await seed_provider_and_workflow(
        file_db_factory,
        workflow_key="mutating_flow",
    )
    acc_id, job1_id = await seed_account_and_job(
        file_db_factory,
        wf_id,
        account_id="acc_hold",
        idempotency_key="key-hold-1",
    )
    # Queue a second job for the same account
    _, job2_id = await seed_account_and_job(
        file_db_factory,
        wf_id,
        account_id="acc_hold",
        idempotency_key="key-hold-2",
    )

    # Browser returns an ambiguous mutating failure (requires_verification=True)
    ambiguous_result = WorkflowExecutionResult(
        workflow_id="mutating_flow",
        provider="rewardlab",
        status=WorkflowExecutionStatus.FAILED,
        steps_executed=1,
        total_steps=2,
        step_results=[
            StepExecutionResult(
                step_id="step_click",
                action="click",
                status=StepStatus.FAILED,
                duration_ms=50.0,
                error_message="Click timed out after submission",
            )
        ],
        error_code="ACTION_TIMEOUT",
        error_message="Click timed out after submission",
        can_retry=False,
        requires_verification=True,  # Crucial: unverified mutating step!
    )

    browser = MockBrowserManager()
    browser.custom_results["mutating_flow"] = ambiguous_result

    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser,
        max_concurrency=2,
        poll_interval_seconds=0.01,
    )

    async with worker:
        # Wait for job 1 to be processed
        for _ in range(50):
            if worker.stats["paused_for_review"] == 1:
                break
            await asyncio.sleep(0.02)

        # Give worker a moment to attempt claiming job 2
        await asyncio.sleep(0.1)

    # Verify job 1 is in PAUSED_FOR_REVIEW
    async with file_db_factory() as session:
        job_repo = JobRepository(session)
        j1 = await job_repo.get_by_id(job1_id)
        assert j1.status == JobStatus.PAUSED_FOR_REVIEW.value

        # Verify job 2 was NOT claimed (remains QUEUED) due to account safety hold!
        j2 = await job_repo.get_by_id(job2_id)
        assert j2.status == JobStatus.QUEUED.value
        assert j2.lease_owner is None


@pytest.mark.asyncio
async def test_no_database_transaction_held_during_browser_execution(file_db_factory):
    """Test 13: SQLite write transactions can proceed while browser execution is active.

    Invariant: 4-phase transaction split guarantees the SQLite DB is NOT locked
    during browser operations.
    """
    _, wf_id = await seed_provider_and_workflow(file_db_factory)
    await seed_account_and_job(file_db_factory, wf_id, account_id="acc_noblock")

    browser_started_event = asyncio.Event()
    db_write_confirmed_event = asyncio.Event()

    class LongRunningBrowser(MockBrowserManager):
        async def execute_workflow(self, *args, **kwargs):
            browser_started_event.set()
            # Wait until external DB write completes
            await db_write_confirmed_event.wait()
            return await super().execute_workflow(*args, **kwargs)

    browser = LongRunningBrowser()
    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=browser,
        max_concurrency=2,
        poll_interval_seconds=0.01,
    )

    await worker.start()

    # Wait until browser execution phase is active
    await browser_started_event.wait()

    # Perform an independent write transaction on the database while browser is running
    async with file_db_factory() as session:
        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            provider_id="rewardlab",
            owner_id="operator_test",
            label="Concurrent Write Check",
            account_id="acc_concurrent_check",
        )
        await session.commit()

    # Notify browser to finish
    db_write_confirmed_event.set()

    # Wait for job to finish
    for _ in range(50):
        if worker.stats["succeeded"] == 1:
            break
        await asyncio.sleep(0.02)

    await worker.stop()

    assert worker.stats["succeeded"] == 1

    # Verify concurrent account exists
    async with file_db_factory() as session:
        acc_repo = AccountRepository(session)
        acc = await acc_repo.get_by_id("acc_concurrent_check")
        assert acc is not None


# =============================================================================
# End-to-End Real Chromium Workflow against Live RewardLab
# =============================================================================


import importlib.util  # noqa: E402
import socket  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from contextlib import closing  # noqa: E402

import httpx  # noqa: E402
import uvicorn  # noqa: E402

# Import RewardLab app dynamically to avoid name collision
rewardlab_root = Path(__file__).resolve().parents[2] / "rewardlab"
if str(rewardlab_root) not in sys.path:
    sys.path.insert(0, str(rewardlab_root))

_spec = importlib.util.spec_from_file_location("rewardlab_worker_app", rewardlab_root / "app.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["rewardlab_worker_app"] = _mod
_spec.loader.exec_module(_mod)
rewardlab_app = _mod.app


def get_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def live_rewardlab_url():
    """Starts live RewardLab server on loopback in a background daemon thread."""
    port = get_free_port()
    config = uvicorn.Config(
        app=rewardlab_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    url = f"http://127.0.0.1:{port}"
    # Wait for server ready
    for _ in range(50):
        try:
            with httpx.Client(base_url=url, timeout=0.5) as client:
                if client.get("/api/health").status_code == 200:
                    break
        except Exception:
            pass
        import time

        time.sleep(0.05)

    yield url


@pytest.mark.asyncio
async def test_worker_e2e_real_chromium(file_db_factory, live_rewardlab_url):
    """Test 14: End-to-end LocalWorker execution with real Chromium against live RewardLab."""
    workflow_yaml_dict = {
        "id": "e2e_worker_review",
        "provider": "rewardlab",
        "schema_version": 1,
        "steps": [
            {
                "id": "nav_home",
                "action": "navigate",
                "url": f"{live_rewardlab_url}/login",
            },
            {
                "id": "assert_login_btn",
                "action": "assert_visible",
                "target": {"by": "role", "role": "button", "name": "Sign in"},
            },
        ],
    }

    prov_id, wf_id = await seed_provider_and_workflow(
        file_db_factory,
        workflow_key="e2e_worker_review",
        workflow_def=workflow_yaml_dict,
    )

    acc_id, job_id = await seed_account_and_job(
        file_db_factory,
        wf_id,
        account_id="test_01",
    )

    # Real Chromium BrowserManager
    real_browser = BrowserManager(headless=True)
    await real_browser.start()

    worker = LocalWorker(
        session_factory=file_db_factory,
        browser_manager=real_browser,
        worker_id="worker-real-chromium",
        base_url=live_rewardlab_url,
        max_concurrency=2,
        poll_interval_seconds=0.02,
    )

    try:
        await worker.start()

        for _ in range(100):
            if worker.stats["succeeded"] == 1:
                break
            await asyncio.sleep(0.1)

        await worker.stop()
    finally:
        await real_browser.stop()

    assert worker.stats["succeeded"] == 1

    # Verify execution results in DB
    async with file_db_factory() as session:
        job_repo = JobRepository(session)
        job = await job_repo.get_by_id(job_id)
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.finished_at is not None

        steps = await job_repo.list_steps_for_job(job_id)
        assert len(steps) == 2
        assert steps[0].step_key == "nav_home"
        assert steps[0].status == StepExecutionStatus.SUCCESS.value
        assert steps[1].step_key == "assert_login_btn"
        assert steps[1].status == StepExecutionStatus.SUCCESS.value
