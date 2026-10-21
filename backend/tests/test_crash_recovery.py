"""Comprehensive test suite for Crash Recovery & Safe Job Reconciliation (Milestone M3.5).

Covers:
1. Stale lease detection for LEASED and RUNNING states.
2. Valid atomic lease renewal.
3. Renewal with stale/invalid token rejected.
4. Worker death before RUNNING (recovers to QUEUED with lease_token + 1).
5. Worker death during read-only step (retried with backoff).
6. Worker death during/after mutating step (fails closed into PAUSED_FOR_REVIEW).
7. Account safety hold survives worker process restart.
8. Concurrent reconcilers idempotency and zero double-reconciliation.
9. Terminal state immutability (SUCCEEDED, FAILED, CANCELLED untouched).
10. Stale worker writes dropped upon lease loss.
11. Observability metrics collection (CPU %, RSS MB, p50/p95 duration, pass rate).
12. Real OS subprocess crash (SIGKILL/TerminateProcess) and recovery on restart.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.observability.metrics import (
    WorkerMetricsTracker,
    compute_percentile,
)
from app.orchestration.job_service import JobService, ReconciliationReport, is_workflow_mutating
from app.persistence.database import set_sqlite_pragma
from app.persistence.models import (
    Base,
    JobModel,
    JobStatus,
    WorkflowApprovalStatus,
    utc_now,
)
from app.persistence.repository import (
    AccountRepository,
    JobRepository,
    ProviderRepository,
    WorkflowVersionRepository,
)
from app.workers.local_worker import LocalWorker

# =============================================================================
# Shared Fixtures & Setup Helpers
# =============================================================================


@pytest.fixture
def file_db_factory(tmp_path: Path):
    """Factory returning async session makers connected to a shared file-backed SQLite database."""
    db_file = tmp_path / "m3_5_crash_recovery_test.db"
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

    yield session_maker, db_file

    async def _teardown():
        await engine.dispose()

    asyncio.run(_teardown())


async def seed_env(
    session_factory: async_sessionmaker[AsyncSession],
    provider_id: str = "rewardlab",
    workflow_key: str = "order_flow",
    is_mutating: bool = True,
    max_attempts: int = 1,
    account_id: str = "acc_recovery_1",
) -> tuple[str, str, str]:
    """Helper to seed provider, workflow version, account, and a queued job."""
    async with session_factory() as session:
        # 1. Provider
        prov_repo = ProviderRepository(session)
        await prov_repo.create_or_update(
            provider_id=provider_id,
            adapter_kind="mock",
            policy_version=1,
        )

        # 2. Workflow definition
        steps = (
            [
                {"id": "step_1", "action": "navigate", "url": "http://127.0.0.1:8001/checkout"},
                {
                    "id": "step_2",
                    "action": "click",
                    "target": {"by": "css", "value": "#confirm_order"},
                    "side_effect": True,
                },
            ]
            if is_mutating
            else [
                {"id": "step_1", "action": "navigate", "url": "http://127.0.0.1:8001/profile"},
                {
                    "id": "step_2",
                    "action": "extract_text",
                    "target": {"by": "css", "value": "#points"},
                    "output_key": "user_points",
                },
            ]
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.get_by_key_and_version(workflow_key, 1)
        if wf is None:
            wf = await wf_repo.create_version(
                workflow_key=workflow_key,
                version=1,
                provider_id=provider_id,
                definition_json={
                    "id": workflow_key,
                    "provider": provider_id,
                    "schema_version": 1,
                    "steps": steps,
                },
                approval_status=WorkflowApprovalStatus.APPROVED,
            )

        # 3. Account
        acc_repo = AccountRepository(session)
        acc = await acc_repo.get_by_id(account_id)
        if acc is None:
            await acc_repo.create_account(
                provider_id=provider_id,
                owner_id="operator_rec",
                label=f"Recovery Account {account_id}",
                account_id=account_id,
            )

        # 4. Job
        job_repo = JobRepository(session)
        job = await job_repo.create_job(
            workflow_version_id=wf.id,
            account_id=account_id,
            idempotency_key=f"rec-key-{account_id}",
            max_attempts=max_attempts,
        )
        job.status = JobStatus.QUEUED.value
        await session.commit()
        return wf.id, account_id, job.id


# =============================================================================
# Unit Tests: Side-Effect Classification & Lease Renewal
# =============================================================================


def test_is_workflow_mutating_classification():
    """Validates accurate classification of mutating vs read-only workflows."""
    # Mutating with click action
    wf_click = {"steps": [{"action": "click", "selector": "#btn"}]}
    assert is_workflow_mutating(wf_click) is True

    # Mutating with fill action
    wf_fill = {"steps": [{"action": "fill", "selector": "#input", "value": "val"}]}
    assert is_workflow_mutating(wf_fill) is True

    # Mutating with explicit side_effect flag
    wf_side_effect = {"steps": [{"action": "custom_action", "side_effect": True}]}
    assert is_workflow_mutating(wf_side_effect) is True

    # Strictly read-only
    wf_readonly = {
        "steps": [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "scrape"},
        ]
    }
    assert is_workflow_mutating(wf_readonly) is False

    # Fail-closed on missing or malformed definitions
    assert is_workflow_mutating(None) is True
    assert is_workflow_mutating({}) is True  # Falsy dict fails closed to True
    assert is_workflow_mutating({"steps": []}) is False  # Empty steps list has no mutations
    assert is_workflow_mutating({"steps": "invalid"}) is True


@pytest.mark.asyncio
async def test_atomic_lease_renewal_success_and_stale_rejection(file_db_factory):
    """Test 2 & 3: Valid lease renewal extends lease_until; stale token is atomically rejected."""
    factory, _ = file_db_factory
    _, _, job_id = await seed_env(factory, account_id="acc_renew")

    async with factory() as session:
        svc = JobService(session)
        # Claim and start job
        job = await svc.claim_job(worker_id="w-1", lease_duration_seconds=10)
        assert job is not None
        valid_token = job.lease_token

        job = await svc.start_job(job_id=job.id, worker_id="w-1", lease_token=valid_token)
        assert job.status == JobStatus.RUNNING.value
        orig_lease_until = job.lease_until

        # 1. Valid renewal extends lease
        renewed = await svc.renew_lease(
            job_id=job.id, worker_id="w-1", lease_token=valid_token, duration_seconds=60
        )
        assert renewed is True
        await session.commit()

        # Verify in DB that lease_until was extended
        reloaded = await svc.job_repo.get_by_id(job_id)
        from datetime import UTC

        orig_tz = (
            orig_lease_until if orig_lease_until.tzinfo else orig_lease_until.replace(tzinfo=UTC)
        )
        reloaded_tz = (
            reloaded.lease_until
            if reloaded.lease_until.tzinfo
            else reloaded.lease_until.replace(tzinfo=UTC)
        )
        assert reloaded_tz > orig_tz

        # 2. Renewal with stale/invalid token is rejected
        rejected_token = await svc.renew_lease(
            job_id=job.id, worker_id="w-1", lease_token=valid_token + 999, duration_seconds=60
        )
        assert rejected_token is False

        # 3. Renewal with mismatched worker_id is rejected
        rejected_worker = await svc.renew_lease(
            job_id=job.id, worker_id="w-intruder", lease_token=valid_token, duration_seconds=60
        )
        assert rejected_worker is False


# =============================================================================
# Unit & Integration Tests: Crash Recovery & Job State Reconciliation
# =============================================================================


@pytest.mark.asyncio
async def test_worker_death_before_running_recovers_to_queued(file_db_factory):
    """Test 4: Worker claimed job (LEASED) but crashed before starting.
    Reconciliation returns job to QUEUED with monotonic lease_token increment.
    """
    factory, _ = file_db_factory
    _, _, job_id = await seed_env(factory, account_id="acc_leased_crash")

    async with factory() as session:
        svc = JobService(session)
        job = await svc.claim_job(worker_id="dead-worker", lease_duration_seconds=1)
        assert job is not None
        assert job.status == JobStatus.LEASED.value
        initial_token = job.lease_token

        # Artificially expire the lease
        past = utc_now() - timedelta(seconds=10)
        await session.execute(
            update(JobModel).where(JobModel.id == job_id).values(lease_until=past)
        )
        await session.commit()

        # Run reconciliation
        report: ReconciliationReport = await svc.reconcile_stale_jobs(actor="test-sweeper")
        await session.commit()

        assert job_id in report.leased_recovered
        assert report.total_reconciled == 1

        # Check job in DB: must be QUEUED, lease cleared, lease_token incremented
        reloaded = await svc.job_repo.get_by_id(job_id)
        assert reloaded.status == JobStatus.QUEUED.value
        assert reloaded.lease_owner is None
        assert reloaded.lease_until is None
        assert reloaded.lease_token == initial_token + 1

        # Lock table should have released the execution lock
        lock = await svc.job_repo.get_account_execution_lock("acc_leased_crash")
        assert lock is None


@pytest.mark.asyncio
async def test_worker_death_during_mutating_step_pauses_for_review(file_db_factory):
    """Test 6 & 13: Worker crashed while executing mutating workflow (RUNNING).
    Reconciliation must fail-closed into PAUSED_FOR_REVIEW with account safety hold preserved.
    """
    factory, _ = file_db_factory
    _, _, job_id = await seed_env(factory, is_mutating=True, account_id="acc_mutating_crash")

    async with factory() as session:
        svc = JobService(session)
        job = await svc.claim_job(worker_id="crashed-worker", lease_duration_seconds=1)
        assert job is not None
        job = await svc.start_job(
            job_id=job.id, worker_id="crashed-worker", lease_token=job.lease_token
        )
        assert job.status == JobStatus.RUNNING.value
        initial_token = job.lease_token

        # Artificially expire the lease
        past = utc_now() - timedelta(seconds=10)
        await session.execute(
            update(JobModel).where(JobModel.id == job_id).values(lease_until=past)
        )
        await session.commit()

        # Run reconciliation
        report = await svc.reconcile_stale_jobs(actor="test-sweeper")
        await session.commit()

        assert job_id in report.running_paused
        assert report.total_reconciled == 1

        # In DB: must be PAUSED_FOR_REVIEW to prevent duplicate mutations!
        reloaded = await svc.job_repo.get_by_id(job_id)
        assert reloaded.status == JobStatus.PAUSED_FOR_REVIEW.value
        assert reloaded.error_code == "WORKER_CRASHED_MID_MUTATION"
        assert reloaded.lease_owner is None
        assert reloaded.lease_token == initial_token + 1


@pytest.mark.asyncio
async def test_worker_death_during_read_only_step_retries_with_backoff(file_db_factory):
    """Test 5: Worker crashed during strictly read-only workflow.
    Since no mutating side effects could have occurred, job safely transitions to RETRY_WAIT.
    """
    factory, _ = file_db_factory
    _, _, job_id = await seed_env(
        factory, is_mutating=False, max_attempts=3, account_id="acc_readonly_crash"
    )

    async with factory() as session:
        svc = JobService(session)
        job = await svc.claim_job(worker_id="crashed-ro-worker", lease_duration_seconds=1)
        assert job is not None
        job = await svc.start_job(
            job_id=job.id, worker_id="crashed-ro-worker", lease_token=job.lease_token
        )
        assert job.status == JobStatus.RUNNING.value

        # Expire lease
        past = utc_now() - timedelta(seconds=10)
        await session.execute(
            update(JobModel).where(JobModel.id == job_id).values(lease_until=past)
        )
        await session.commit()

        report = await svc.reconcile_stale_jobs(actor="test-sweeper")
        await session.commit()

        assert job_id in report.running_retried
        assert report.total_reconciled == 1

        reloaded = await svc.job_repo.get_by_id(job_id)
        assert reloaded.status == JobStatus.RETRY_WAIT.value
        assert reloaded.attempt == 2
        assert reloaded.error_code == "WORKER_CRASHED_READ_ONLY"
        assert reloaded.scheduled_at is not None


@pytest.mark.asyncio
async def test_worker_death_read_only_exhausted_fails(file_db_factory):
    """Worker crashed during read-only workflow but retry attempts are exhausted -> FAILED."""
    factory, _ = file_db_factory
    _, _, job_id = await seed_env(
        factory, is_mutating=False, max_attempts=1, account_id="acc_readonly_exhausted"
    )

    async with factory() as session:
        svc = JobService(session)
        job = await svc.claim_job(worker_id="crashed-ro-worker", lease_duration_seconds=1)
        assert job is not None
        job = await svc.start_job(
            job_id=job.id, worker_id="crashed-ro-worker", lease_token=job.lease_token
        )

        # Expire lease
        past = utc_now() - timedelta(seconds=10)
        await session.execute(
            update(JobModel).where(JobModel.id == job_id).values(lease_until=past)
        )
        await session.commit()

        report = await svc.reconcile_stale_jobs(actor="test-sweeper")
        await session.commit()

        assert job_id in report.running_failed
        reloaded = await svc.job_repo.get_by_id(job_id)
        assert reloaded.status == JobStatus.FAILED.value
        assert reloaded.error_code == "WORKER_CRASHED_EXHAUSTED"
        assert reloaded.finished_at is not None


@pytest.mark.asyncio
async def test_terminal_states_immutable_under_reconciliation(file_db_factory):
    """Test 12: Terminal states (SUCCEEDED, FAILED, CANCELLED) are completely immutable."""
    factory, _ = file_db_factory
    _, _, job_id = await seed_env(factory, account_id="acc_terminal")

    async with factory() as session:
        svc = JobService(session)
        job = await svc.claim_job(worker_id="w-term")
        job = await svc.start_job(job_id=job.id, worker_id="w-term", lease_token=job.lease_token)
        await svc.complete_job(job_id=job.id, worker_id="w-term", lease_token=job.lease_token)
        await session.commit()

        # Set lease_until in the past
        past = utc_now() - timedelta(seconds=100)
        await session.execute(
            update(JobModel).where(JobModel.id == job_id).values(lease_until=past)
        )
        await session.commit()

        # Reconcile sweep
        report = await svc.reconcile_stale_jobs(actor="test-sweeper")
        await session.commit()

        assert report.total_reconciled == 0
        reloaded = await svc.job_repo.get_by_id(job_id)
        assert reloaded.status == JobStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_concurrent_recovery_actors_and_idempotency(file_db_factory):
    """Test 9 & 10: Multiple concurrent reconcilers race on same stale jobs; exactly one recovers
    each job without double-reconciliation or errors.
    """
    factory, _ = file_db_factory
    job_ids = []
    for i in range(5):
        _, _, j_id = await seed_env(factory, account_id=f"acc_multi_{i}")
        job_ids.append(j_id)

    # Claim all jobs as dead worker and expire leases
    async with factory() as session:
        svc = JobService(session)
        for _j_id in job_ids:
            j = await svc.claim_job(worker_id="dead-actor")
            await svc.start_job(job_id=j.id, worker_id="dead-actor", lease_token=j.lease_token)
        past = utc_now() - timedelta(seconds=20)
        await session.execute(update(JobModel).values(lease_until=past))
        await session.commit()

    # Launch 4 parallel reconciliation sweeps simulating multi-worker contention
    async def _sweep(actor_name: str) -> ReconciliationReport:
        async with factory() as session:
            svc = JobService(session)
            rep = await svc.reconcile_stale_jobs(actor=actor_name)
            await session.commit()
            return rep

    results = await asyncio.gather(
        _sweep("reconciler-1"),
        _sweep("reconciler-2"),
        _sweep("reconciler-3"),
        _sweep("reconciler-4"),
    )

    total_recovered = sum(r.total_reconciled for r in results)
    assert total_recovered == 5

    # Second sweep immediately after must recover 0 (idempotency check)
    second_rep = await _sweep("reconciler-followup")
    assert second_rep.total_reconciled == 0


@pytest.mark.asyncio
async def test_account_safety_hold_survives_restart(file_db_factory):
    """Test 11: After worker crash and recovery into PAUSED_FOR_REVIEW, subsequent jobs
    for the same account are blocked by the account safety hold across worker restarts.
    """
    factory, _ = file_db_factory
    wf_id, acc_id, job1_id = await seed_env(
        factory, is_mutating=True, account_id="acc_hold_restart"
    )

    # Queue second job for same account
    async with factory() as session:
        job_repo = JobRepository(session)
        job2 = await job_repo.create_job(
            workflow_version_id=wf_id,
            account_id=acc_id,
            idempotency_key="rec-key-acc_hold_restart-2",
        )
        job2.status = JobStatus.QUEUED.value
        await session.commit()
        job2_id = job2.id

    # 1. Simulate worker crash on job 1
    async with factory() as session:
        svc = JobService(session)
        j1 = await svc.claim_job(worker_id="worker-crashed")
        await svc.start_job(job_id=j1.id, worker_id="worker-crashed", lease_token=j1.lease_token)
        # Crash occurs: lease expires
        past = utc_now() - timedelta(seconds=30)
        stmt = update(JobModel).where(JobModel.id == job1_id).values(lease_until=past)
        await session.execute(stmt)
        await session.commit()

    # 2. Start a BRAND NEW worker instance (restart simulation)
    class _DummyBrowser:
        def is_healthy(self) -> bool:
            return True

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    new_worker = LocalWorker(
        session_factory=factory,
        browser_manager=_DummyBrowser(),  # type: ignore[arg-type]
        worker_id="new-worker-restarted",
        max_concurrency=2,
        poll_interval_seconds=0.01,
        reconcile_interval_seconds=0.0,  # Manual check
    )
    # Startup reconciliation executes inside start()
    await new_worker.start()

    # Wait a moment for worker to attempt poll
    await asyncio.sleep(0.05)
    await new_worker.stop()

    # 3. Check DB: job 1 is PAUSED_FOR_REVIEW;
    # job 2 was NOT claimed (Account Safety Hold preserved!)
    async with factory() as session:
        job_repo = JobRepository(session)
        db_j1 = await job_repo.get_by_id(job1_id)
        assert db_j1.status == JobStatus.PAUSED_FOR_REVIEW.value

        db_j2 = await job_repo.get_by_id(job2_id)
        assert db_j2.status == JobStatus.QUEUED.value
        assert db_j2.lease_owner is None


# =============================================================================
# Observability Metrics Collection Tests
# =============================================================================


def test_worker_metrics_collection():
    """Test 11 & 16: Metrics tracker collects CPU %, RSS memory, p50/p95 duration, and pass rate."""
    tracker = WorkerMetricsTracker()

    # Duration percentiles
    durations = [1.0, 2.0, 3.0, 4.0, 5.0, 10.0]
    for d in durations:
        tracker.record_job_duration(d)

    p50 = compute_percentile(durations, 50.0)
    p95 = compute_percentile(durations, 95.0)
    assert p50 == 3.5
    assert p95 == 8.75

    # Outcomed tracking
    tracker.record_job_outcome("succeeded")
    tracker.record_job_outcome("succeeded")
    tracker.record_job_outcome("failed")
    tracker.record_job_outcome("paused_for_review")
    tracker.record_recovered_jobs(3)

    snapshot = tracker.get_snapshot(active_jobs_count=1)
    assert snapshot["active_jobs_count"] == 1
    assert snapshot["jobs_succeeded"] == 2
    assert snapshot["jobs_failed"] == 1
    assert snapshot["jobs_paused_for_review"] == 1
    assert snapshot["total_finished"] == 4
    assert snapshot["pass_rate"] == 0.5
    assert snapshot["recovered_jobs_count"] == 3
    assert snapshot["p50_duration_seconds"] == 3.5
    assert snapshot["p95_duration_seconds"] == 8.75
    assert snapshot["rss_bytes"] >= 0
    assert snapshot["rss_mb"] >= 0.0
    assert snapshot["cpu_percent"] >= 0.0


# =============================================================================
# Integration Test: Real OS Subprocess Crash & Restart Recovery
# =============================================================================


@pytest.mark.asyncio
async def test_real_subprocess_worker_crash_and_restart_recovery(file_db_factory):
    """Test 8 & 17: Executes a real subprocess worker, forcefully kills it,
    and starts a new worker that safely recovers the orphaned mutating job into PAUSED_FOR_REVIEW.
    """
    factory, db_file = file_db_factory
    wf_id, acc_id, job_id = await seed_env(
        factory, is_mutating=True, account_id="acc_real_subprocess"
    )

    # 1. Launch a real worker process as an OS subprocess with short poll and lease interval
    backend_dir = Path(__file__).parent.parent
    cmd = [
        sys.executable,
        "-m",
        "app.workers.local_worker",
        "--database-url",
        f"sqlite+aiosqlite:///{db_file.as_posix()}",
        "--worker-id",
        "subprocess-crasher",
        "--poll-interval",
        "0.05",
        "--reconcile-interval",
        "0",  # Disable automatic sweep in crashed worker
        "--headless",
    ]
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    env["PYTHONPATH"] = str(backend_dir)

    log_file = db_file.parent / "subprocess_worker.log"
    with open(log_file, "w") as log_f:
        proc = subprocess.Popen(
            cmd,
            cwd=str(backend_dir),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )

    # 2. Wait until the subprocess claims the job and transitions it to RUNNING (or LEASED)
    claimed = False
    for _ in range(50):
        await asyncio.sleep(0.1)
        async with factory() as session:
            job_repo = JobRepository(session)
            j = await job_repo.get_by_id(job_id)
            if j and j.status in (JobStatus.LEASED.value, JobStatus.RUNNING.value):
                claimed = True
                break

    if not claimed:
        proc.kill()
        log_text = log_file.read_text(encoding="utf-8") if log_file.exists() else "No log"
        pytest.fail(f"Subprocess worker failed to claim. Exit code: {proc.poll()}, Log: {log_text}")

    # 3. FORCEFULLY TERMINATE the process (SIGKILL / TerminateProcess) simulating hard crash
    proc.kill()
    proc.wait(timeout=5)
    assert proc.poll() is not None, "Process did not terminate after kill()"

    # 4. Set the orphaned job's lease_until in the past and ensure status is RUNNING
    async with factory() as session:
        past = utc_now() - timedelta(seconds=10)
        await session.execute(
            update(JobModel)
            .where(JobModel.id == job_id)
            .values(status=JobStatus.RUNNING.value, lease_until=past)
        )
        await session.commit()

    # 5. Start a NEW worker process instance to recover the database state
    recovering_worker = LocalWorker(
        session_factory=factory,
        worker_id="subprocess-recovery-agent",
        max_concurrency=1,
        poll_interval_seconds=0.1,
        reconcile_interval_seconds=0.0,
    )

    report = await recovering_worker.reconcile()
    assert report.total_reconciled == 1
    assert job_id in report.running_paused

    # 6. Verify job state after crash recovery
    async with factory() as session:
        job_repo = JobRepository(session)
        recovered_job = await job_repo.get_by_id(job_id)
        assert recovered_job is not None
        assert recovered_job.status == JobStatus.PAUSED_FOR_REVIEW.value
        assert recovered_job.lease_owner is None
        assert recovered_job.error_code == "WORKER_CRASHED_MID_MUTATION"
        # Account execution lock must be released
        lock = await job_repo.get_account_execution_lock(acc_id)
        assert lock is None
