"""Acceptance and regression tests for Milestone Personal P2.1:
Security & Operator Reliability Hardening.

Covers:
Phase A (P0): Free-Text Secret Redaction
- Redacting password=example_secret_123 in error messages
- Redacting Bearer authorization tokens in headers/messages
- Redacting tokens in query strings
- Redacting cookies and credentials in nested data structures
- Write-time sanitization of operator review reason
- Operator review reason length and emptiness enforcement
- Sanitizing audit events
- Redaction in CLI human-readable table output
- Redaction in CLI --json output
- Read-time redaction of legacy DB records containing raw secrets
- Preservation of benign free-text strings
- Secret suppression in logging filter
- Write-time protection ensuring raw secrets are never committed to DB

Phase B (P1): Full Static ProviderPolicy Inspection
- Authorized RewardLab workflow -> ALLOWED
- Unknown provider -> DENIED
- Microsoft Rewards companion-only -> DENIED
- Disallowed action -> DENIED
- Out-of-allowlist domain -> DENIED
- Prohibited schemes (javascript:, file:, data:) -> DENIED
- Disallowed port -> DENIED
- Valid relative path -> ALLOWED
- Dynamic unresolved URL template -> UNRESOLVED
- Unauthorized capability request -> DENIED
- Zero browser launch and policy immutability

Phase C (P1): Atomic Concurrent Operator Review
- Real file-backed SQLite database
- Two concurrent competing operator reviews (cancel vs reject)
- Exactly one succeeds; competing reviewer receives IllegalStateTransitionError
- Database contains exactly one terminal decision and exactly one audit event
- Account safety hold persists when multiple paused jobs exist on same account
- Cross-account isolation during review

Phase D (P1): Persist Causal Verification Evidence
- Verified job persists outcome_type, verified_mutations, causal_mappings
- Unconfirmed mutation job persists unverified_mutations and failure reason
- Legacy job returns NOT_RECORDED fallback for backward compatibility
- CLI jobs show displays causal evidence in both text and JSON formats
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.policy import PolicyEvaluator, ProviderPolicy
from app.observability.logging import (
    RedactionFilter,
    redact_data,
    redact_free_text,
)
from app.operator_cli import (
    build_jobs_parser,
    execute_jobs_async,
    inspect_workflow_plan,
)
from app.orchestration.job_service import JobService
from app.orchestration.state_machine import IllegalStateTransitionError
from app.persistence.models import (
    AuditEventModel,
    Base,
    JobModel,
    JobStatus,
    WorkflowApprovalStatus,
)
from app.persistence.repository import (
    AccountRepository,
    AuditEventRepository,
    JobRepository,
    ProviderRepository,
    WorkflowVersionRepository,
)

# =============================================================================
# Database Fixtures
# =============================================================================


@pytest.fixture
async def sqlite_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sqlite_session_factory(sqlite_engine):
    return async_sessionmaker(
        bind=sqlite_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@pytest.fixture
async def setup_db_provider(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        prov_repo = ProviderRepository(session)
        await prov_repo.create_or_update(
            provider_id="rewardlab",
            adapter_kind="rewardlab",
            policy_version=1,
        )
        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            account_id="acc_01", provider_id="rewardlab", owner_id="op1", label="Account 1"
        )
        await acc_repo.create_account(
            account_id="acc_02", provider_id="rewardlab", owner_id="op2", label="Account 2"
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            "wf_key", 1, "rewardlab", {"steps": []}, approval_status=WorkflowApprovalStatus.APPROVED
        )
        await session.commit()
        return wf.id


# =============================================================================
# PHASE A: Free-Text Secret Redaction Tests
# =============================================================================


def test_p2_1_redact_free_text_password_in_error_message() -> None:
    raw = "Authentication failed: password=example_secret_123 for user admin"
    sanitized = redact_free_text(raw)
    assert "example_secret_123" not in sanitized
    assert "password=[REDACTED]" in sanitized
    assert "for user admin" in sanitized


def test_p2_1_redact_free_text_bearer_authorization_token() -> None:
    raw = "Request failed with header Authorization: Bearer example_token_123"
    sanitized = redact_free_text(raw)
    assert "example_token_123" not in sanitized
    assert "Authorization: Bearer [REDACTED]" in sanitized


def test_p2_1_redact_free_text_token_in_query_string() -> None:
    raw = "Redirecting to http://127.0.0.1:8001/tasks?token=secret_query_val&page=2"
    sanitized = redact_free_text(raw)
    assert "secret_query_val" not in sanitized
    assert "token=[REDACTED]" in sanitized
    assert "page=2" in sanitized


def test_p2_1_redact_data_cookie_in_nested_dict() -> None:
    payload = {
        "headers": {
            "cookie": "session_id=secret_cookie_val; user=tester",
            "content_type": "application/json",
        },
        "error_message": "Error encountered: password=raw_secret_value",
    }
    redacted = redact_data(payload)
    assert "raw_secret_value" not in str(redacted)
    assert redacted["headers"]["cookie"] == "[REDACTED]"
    assert "password=[REDACTED]" in redacted["error_message"]
    assert redacted["headers"]["content_type"] == "application/json"


@pytest.mark.asyncio
async def test_p2_1_operator_review_reason_write_time_redaction(
    sqlite_session_factory, setup_db_provider
) -> None:
    wf_id = setup_db_provider
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf_id, "acc_01", "idem_reason_01")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        await session.commit()
        job_id = job.id

    raw_reason = "Rejecting because password=super_secret_operator_pass was wrong"
    async with sqlite_session_factory() as session:
        service = JobService(session)
        updated = await service.review_job(job_id=job_id, decision="reject", reason=raw_reason)
        await session.commit()
        assert updated.status == JobStatus.REJECTED.value

    # Direct DB inspection: ensure raw secret is NEVER in JobModel or AuditEventModel
    async with sqlite_session_factory() as session:
        stmt = select(JobModel).where(JobModel.id == job_id)
        res = await session.execute(stmt)
        db_job = res.scalar_one()
        assert "super_secret_operator_pass" not in db_job.error_message
        assert "password=[REDACTED]" in db_job.error_message

        stmt_audit = select(AuditEventModel).where(AuditEventModel.resource == f"job:{job_id}")
        res_audit = await session.execute(stmt_audit)
        audit_events = list(res_audit.scalars().all())
        assert len(audit_events) == 1
        audit_str = str(audit_events[0].sanitized_details)
        assert "super_secret_operator_pass" not in audit_str
        assert "password=[REDACTED]" in audit_str


@pytest.mark.asyncio
async def test_p2_1_operator_review_reason_validation_bounds(
    sqlite_session_factory, setup_db_provider
) -> None:
    wf_id = setup_db_provider
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf_id, "acc_01", "idem_bounds_01")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        await session.commit()
        job_id = job.id

    async with sqlite_session_factory() as session:
        service = JobService(session)
        # Empty reason rejected
        with pytest.raises(ValueError, match="non-empty reason"):
            await service.review_job(job_id=job_id, decision="cancel", reason="   ")

        # Excessively long reason (>1024) rejected
        too_long = "A" * 1025
        with pytest.raises(ValueError, match="exceeds maximum allowed length"):
            await service.review_job(job_id=job_id, decision="cancel", reason=too_long)


@pytest.mark.asyncio
async def test_p2_1_legacy_db_record_with_raw_secret_redacted_at_read_time(
    sqlite_session_factory, setup_db_provider
) -> None:
    """If a legacy record in SQLite contains an unredacted secret, get_job_detail redacts it."""
    wf_id = setup_db_provider
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf_id, "acc_01", "idem_legacy_01")
        # Direct raw unredacted string assignment bypassing service
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        job.error_message = "Crash log: password=legacy_unredacted_password_123 failed"
        await session.commit()
        job_id = job.id

    async with sqlite_session_factory() as session:
        service = JobService(session)
        detail = await service.get_job_detail(job_id)
        assert detail is not None
        assert "legacy_unredacted_password_123" not in detail["error_message"]
        assert "password=[REDACTED]" in detail["error_message"]
        assert "legacy_unredacted_password_123" not in detail["pause_reason"]
        assert "password=[REDACTED]" in detail["pause_reason"]


def test_p2_1_benign_free_text_unaltered() -> None:
    benign_samples = [
        "Workflow completed successfully in 12.5s",
        "No errors detected during navigation",
        "The password was changed successfully by user",
        "User session active for 300 seconds",
        "Task task_01 is currently reviewed",
    ]
    for sample in benign_samples:
        assert redact_free_text(sample) == sample


def test_p2_1_redaction_filter_suppresses_log_secrets() -> None:
    filter_ = RedactionFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=10,
        msg="Failed auth with Authorization: Bearer secret_token_xyz and password=raw_pass_456",
        args=(),
        exc_info=None,
    )
    filter_.filter(record)
    assert "secret_token_xyz" not in record.msg
    assert "raw_pass_456" not in record.msg
    assert "Bearer [REDACTED]" in record.msg
    assert "password=[REDACTED]" in record.msg


@pytest.mark.asyncio
async def test_p2_1_cli_text_and_json_redaction(sqlite_session_factory, setup_db_provider) -> None:
    wf_id = setup_db_provider
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf_id, "acc_01", "idem_cli_redact_01")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        job.error_message = "Failed: token=cli_secret_token_777"
        await session.commit()
        job_id = job.id

    parser = build_jobs_parser()

    @asynccontextmanager
    async def mock_db():
        async with sqlite_session_factory() as s:
            yield s

    # 1. Text table output
    args_text = parser.parse_args(["show", job_id])
    with patch("app.operator_cli.get_db_session", mock_db):
        code_text, out_text = await execute_jobs_async(args_text)
    assert code_text == 0
    assert "cli_secret_token_777" not in out_text
    assert "token=[REDACTED]" in out_text

    # 2. JSON output
    args_json = parser.parse_args(["show", job_id, "--json"])
    with patch("app.operator_cli.get_db_session", mock_db):
        code_json, out_json = await execute_jobs_async(args_json)
    assert code_json == 0
    assert "cli_secret_token_777" not in out_json
    data = json.loads(out_json)
    assert "token=[REDACTED]" in data["error_message"]


# =============================================================================
# PHASE B: Full Static ProviderPolicy Inspection Tests
# =============================================================================


def test_p2_1_inspector_allowed_workflow(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_allowed_wf
provider: rewardlab
requested_capabilities: [read_tasks, mark_reviewed]
steps:
  - id: s1
    action: navigate
    path: /tasks/task_01
  - id: s2
    action: click
    target:
      by: test_id
      value: mark-btn
  - id: s3
    action: verify_state
    field: reviewed
    equals: true
    verifies_step_ids: [s2]
"""
    f = tmp_path / "allowed.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "ALLOWED"
    assert report["policy_evaluation"]["preflight_verdict"] == "ALLOWED"
    assert report["policy_evaluation"]["action_violations"] == []
    assert report["policy_evaluation"]["destination_violations"] == []


def test_p2_1_inspector_denies_unknown_provider(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_unknown_provider
provider: unknown_attacker_site
steps:
  - id: s1
    action: navigate
    url: https://attacker.com/
"""
    f = tmp_path / "unknown_prov.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "DENIED"
    assert report["policy_evaluation"]["automation_allowed"] is False


def test_p2_1_inspector_denies_microsoft_rewards_companion_only(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_ms_rewards
provider: microsoft_rewards
steps:
  - id: s1
    action: navigate
    url: https://rewards.bing.com/
"""
    f = tmp_path / "ms_rewards.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "DENIED"
    assert "companion-only" in report["policy_evaluation"]["policy_reason"]


def test_p2_1_inspector_denies_disallowed_action(tmp_path: Path) -> None:
    # Build policy with restricted actions
    evaluator = PolicyEvaluator(
        policies={
            "rewardlab": ProviderPolicy(
                provider="rewardlab",
                automation_allowed=True,
                domain_allowlist=["127.0.0.1", "localhost"],
                allowed_actions=["navigate"],  # click is disallowed!
                required_capabilities=[],
                allowed_capabilities=[],
            )
        }
    )
    wf_yaml = """
schema_version: 1
id: test_disallowed_action
provider: rewardlab
steps:
  - id: s1
    action: click
    target:
      by: test_id
      value: btn
"""
    f = tmp_path / "bad_action.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    from unittest.mock import patch

    with patch("app.operator_cli.default_evaluator", evaluator):
        code, report = inspect_workflow_plan(f)
        assert code == 0
        assert report["preflight_verdict"] == "DENIED"
        assert len(report["policy_evaluation"]["action_violations"]) == 1
        assert "click" in report["policy_evaluation"]["action_violations"][0]


def test_p2_1_inspector_denies_out_of_allowlist_domain(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_bad_domain
provider: rewardlab
steps:
  - id: s1
    action: navigate
    url: https://evil.attacker.com/tasks
"""
    f = tmp_path / "bad_domain.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "DENIED"
    violations = report["policy_evaluation"]["destination_violations"]
    assert len(violations) == 1
    assert "not in provider domain allowlist" in violations[0]


def test_p2_1_inspector_denies_prohibited_scheme(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_bad_scheme
provider: rewardlab
steps:
  - id: s1
    action: navigate
    url: javascript:alert(document.cookie)
"""
    f = tmp_path / "bad_scheme.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 5
    assert report["error"] == "SCHEMA_VALIDATION_ERROR"
    assert "must use 'http' or 'https' scheme" in report["message"]


def test_p2_1_inspector_denies_disallowed_port(tmp_path: Path) -> None:
    evaluator = PolicyEvaluator(
        policies={
            "rewardlab": ProviderPolicy(
                provider="rewardlab",
                automation_allowed=True,
                domain_allowlist=["127.0.0.1", "localhost"],
                allowed_actions=["navigate"],
                allowed_ports=[8001],  # port 9090 disallowed!
                required_capabilities=[],
                allowed_capabilities=[],
            )
        }
    )
    wf_yaml = """
schema_version: 1
id: test_bad_port
provider: rewardlab
steps:
  - id: s1
    action: navigate
    url: http://127.0.0.1:9090/tasks
"""
    f = tmp_path / "bad_port.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    from unittest.mock import patch

    with patch("app.operator_cli.default_evaluator", evaluator):
        code, report = inspect_workflow_plan(f)
        assert code == 0
        assert report["preflight_verdict"] == "DENIED"
        violations = report["policy_evaluation"]["destination_violations"]
        assert "not in provider allowed ports" in violations[0]


def test_p2_1_inspector_unresolved_dynamic_url(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_unresolved_url
provider: rewardlab
steps:
  - id: s1
    action: navigate
    url: http://127.0.0.1:8001/tasks/{target_task_id}
"""
    f = tmp_path / "unresolved.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "UNRESOLVED"
    unresolved = report["policy_evaluation"]["unresolved_destinations"]
    assert len(unresolved) == 1
    assert "dynamic template parameters" in unresolved[0]


def test_p2_1_inspector_denies_unauthorized_capability(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_bad_cap
provider: rewardlab
requested_capabilities: [read_tasks, prohibited_superadmin_cap]
steps:
  - id: s1
    action: navigate
    path: /tasks
"""
    f = tmp_path / "bad_cap.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "DENIED"
    assert report["policy_evaluation"]["capabilities_granted"] is False
    assert "prohibited_superadmin_cap" in report["policy_evaluation"]["policy_reason"]


# =============================================================================
# PHASE C: Atomic Concurrent Operator Review Tests
# =============================================================================


@pytest.mark.asyncio
async def test_p2_1_atomic_concurrent_review_file_backed_sqlite(tmp_path: Path) -> None:
    """Verifies that when two reviewers concurrently review the same paused job on a real
    file-backed SQLite DB, exactly ONE succeeds and the competing one is cleanly rejected.
    """
    db_file = tmp_path / "concurrent_review.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(db_url, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    # Seed data
    async with session_factory() as session:
        prov_repo = ProviderRepository(session)
        await prov_repo.create_or_update(
            provider_id="rewardlab", adapter_kind="rewardlab", policy_version=1
        )
        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            account_id="acc_race", provider_id="rewardlab", owner_id="op1", label="Race Acc"
        )
        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            "wf_key", 1, "rewardlab", {"steps": []}, approval_status=WorkflowApprovalStatus.APPROVED
        )
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf.id, "acc_race", "idem_race_01")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        await session.commit()
        job_id = job.id

    async def reviewer_task(decision: str, reason: str, actor: str) -> dict[str, Any]:
        async with session_factory() as session:
            service = JobService(session)
            try:
                res = await service.review_job(
                    job_id=job_id, decision=decision, reason=reason, actor=actor
                )
                await session.commit()
                return {"success": True, "decision": decision, "status": res.status}
            except IllegalStateTransitionError as err:
                return {"success": False, "error": str(err)}

    # Run competing reviews simultaneously
    results = await asyncio.gather(
        reviewer_task("cancel", "Operator Reviewer A cancels", "actor_a"),
        reviewer_task("reject", "Operator Reviewer B rejects", "actor_b"),
    )

    success_results = [r for r in results if r["success"]]
    failed_results = [r for r in results if not r["success"]]

    # Invariant: EXACTLY one succeeds, and exactly one is rejected with state transition conflict
    assert len(success_results) == 1
    assert len(failed_results) == 1
    assert "not 'PAUSED_FOR_REVIEW'" in failed_results[0]["error"]

    # Verify DB state: exactly one terminal state, exactly one audit event
    async with session_factory() as session:
        stmt_job = select(JobModel).where(JobModel.id == job_id)
        res_job = await session.execute(stmt_job)
        final_job = res_job.scalar_one()
        assert final_job.status in (JobStatus.CANCELLED.value, JobStatus.REJECTED.value)

        stmt_audit = select(AuditEventModel).where(AuditEventModel.resource == f"job:{job_id}")
        res_audit = await session.execute(stmt_audit)
        audits = list(res_audit.scalars().all())
        assert len(audits) == 1
        assert audits[0].action in ("jobs.review.cancel", "jobs.review.reject")

    await engine.dispose()


@pytest.mark.asyncio
async def test_p2_1_account_safety_hold_persists_when_multiple_paused_jobs_exist(
    sqlite_session_factory, setup_db_provider
) -> None:
    wf_id = setup_db_provider
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        # Create 2 paused jobs and 1 queued job on acc_01
        paused1 = await job_repo.create_job(wf_id, "acc_01", "idem_p1")
        paused1.status = JobStatus.PAUSED_FOR_REVIEW.value

        paused2 = await job_repo.create_job(wf_id, "acc_01", "idem_p2")
        paused2.status = JobStatus.PAUSED_FOR_REVIEW.value

        queued = await job_repo.create_job(wf_id, "acc_01", "idem_q1")
        queued.status = JobStatus.QUEUED.value

        await session.commit()
        p1_id = paused1.id
        q_id = queued.id

    # 1. While both are paused, claim_next_job cannot claim queued
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        candidate = await job_repo.claim_next_job("worker_1")
        assert candidate is None

    # 2. Review and cancel job 1. Since job 2 is still paused, claim_next_job STILL returns None!
    async with sqlite_session_factory() as session:
        service = JobService(session)
        await service.review_job(job_id=p1_id, decision="cancel", reason="Cancelled job 1")
        await session.commit()

    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        candidate = await job_repo.claim_next_job("worker_1")
        assert candidate is None  # Safety hold persists!

    # 3. Review job 2. Now that no paused jobs remain, queued job can be claimed!
    async with sqlite_session_factory() as session:
        service = JobService(session)
        await service.review_job(job_id=paused2.id, decision="reject", reason="Rejected job 2")
        await session.commit()

    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        candidate = await job_repo.claim_next_job("worker_1")
        assert candidate is not None
        assert candidate.id == q_id


@pytest.mark.asyncio
async def test_p2_1_cross_account_isolation(sqlite_session_factory, setup_db_provider) -> None:
    wf_id = setup_db_provider
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        # Paused job on acc_01
        p_acc1 = await job_repo.create_job(wf_id, "acc_01", "idem_iso_p1")
        p_acc1.status = JobStatus.PAUSED_FOR_REVIEW.value

        # Queued job on acc_02
        q_acc2 = await job_repo.create_job(wf_id, "acc_02", "idem_iso_q2")
        q_acc2.status = JobStatus.QUEUED.value
        await session.commit()
        q_acc2_id = q_acc2.id

    # acc_01 paused does NOT block acc_02
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        candidate = await job_repo.claim_next_job("worker_1")
        assert candidate is not None
        assert candidate.id == q_acc2_id
        assert candidate.account_id == "acc_02"


# =============================================================================
# PHASE D: Persist Causal Verification Evidence Tests
# =============================================================================


@pytest.mark.asyncio
async def test_p2_1_causal_evidence_retrieval_and_legacy_fallback(
    sqlite_session_factory, setup_db_provider
) -> None:
    wf_id = setup_db_provider

    # 1. Job WITH causal verification audit event
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        job1 = await job_repo.create_job(wf_id, "acc_01", "idem_causal_01")
        job1.status = JobStatus.SUCCEEDED.value

        audit_repo = AuditEventRepository(session)
        await audit_repo.record_event(
            actor="worker_local",
            action="job.causal_verification",
            resource=f"job:{job1.id}",
            outcome="STATE_VERIFIED",
            sanitized_details={
                "execution_id": "exec_abc123",
                "outcome_type": "STATE_VERIFIED",
                "requires_verification": False,
                "verified_mutations": ["step_click_1", "step_click_2"],
                "unverified_mutations": [],
                "causal_mappings": {"step_verify_1": ["step_click_1", "step_click_2"]},
                "error_code": None,
            },
        )
        await session.commit()
        job1_id = job1.id

    # 2. Legacy Job WITHOUT causal verification audit event
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        job2 = await job_repo.create_job(wf_id, "acc_02", "idem_legacy_causal_02")
        job2.status = JobStatus.SUCCEEDED.value
        await session.commit()
        job2_id = job2.id

    async with sqlite_session_factory() as session:
        service = JobService(session)

        # Job 1 verification evidence
        detail1 = await service.get_job_detail(job1_id)
        assert detail1 is not None
        cv1 = detail1["causal_verification"]
        assert cv1["recorded"] is True
        assert cv1["outcome_type"] == "STATE_VERIFIED"
        assert cv1["requires_verification"] is False
        assert cv1["verified_mutations"] == ["step_click_1", "step_click_2"]
        assert cv1["unverified_mutations"] == []
        assert cv1["causal_mappings"] == {"step_verify_1": ["step_click_1", "step_click_2"]}

        # Job 2 legacy fallback: NOT_RECORDED
        detail2 = await service.get_job_detail(job2_id)
        assert detail2 is not None
        cv2 = detail2["causal_verification"]
        assert cv2["recorded"] is False
        assert cv2["outcome_type"] == "NOT_RECORDED"
        assert cv2["requires_verification"] == "NOT_RECORDED"
        assert cv2["verified_mutations"] == "NOT_RECORDED"
        assert cv2["unverified_mutations"] == "NOT_RECORDED"
        assert cv2["causal_mappings"] == "NOT_RECORDED"


@pytest.mark.asyncio
async def test_p2_1_causal_evidence_in_cli_jobs_show(
    sqlite_session_factory, setup_db_provider
) -> None:
    wf_id = setup_db_provider
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf_id, "acc_01", "idem_cli_causal_01")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        job.error_code = "SIDE_EFFECT_AMBIGUOUS"

        audit_repo = AuditEventRepository(session)
        await audit_repo.record_event(
            actor="worker_local",
            action="job.causal_verification",
            resource=f"job:{job.id}",
            outcome="OUTCOME_UNCONFIRMED",
            sanitized_details={
                "execution_id": "exec_unconfirmed_456",
                "outcome_type": "OUTCOME_UNCONFIRMED",
                "requires_verification": True,
                "verified_mutations": [],
                "unverified_mutations": ["step_mutate_unverified"],
                "causal_mappings": {},
                "error_code": "SIDE_EFFECT_AMBIGUOUS",
            },
        )
        await session.commit()
        job_id = job.id

    parser = build_jobs_parser()

    @asynccontextmanager
    async def mock_db():
        async with sqlite_session_factory() as s:
            yield s

    # Test human table output
    args_show = parser.parse_args(["show", job_id])
    with patch("app.operator_cli.get_db_session", mock_db):
        code, out = await execute_jobs_async(args_show)
    assert code == 0
    assert "CAUSAL VERIFICATION EVIDENCE:" in out
    assert "Outcome Type:         OUTCOME_UNCONFIRMED" in out
    assert "step_mutate_unverified" in out

    # Test JSON output
    args_json = parser.parse_args(["show", job_id, "--json"])
    with patch("app.operator_cli.get_db_session", mock_db):
        code_json, out_json = await execute_jobs_async(args_json)
    assert code_json == 0
    data = json.loads(out_json)
    assert data["causal_verification"]["recorded"] is True
    assert data["causal_verification"]["outcome_type"] == "OUTCOME_UNCONFIRMED"
    assert "step_mutate_unverified" in data["causal_verification"]["unverified_mutations"]
