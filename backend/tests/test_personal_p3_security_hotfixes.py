"""Tests for Personal P3 Phase 0 Security & Operator Reliability Hotfixes:
1. Long Free-Text Redaction with Fail-Closed Truncation:
   - Secret at beginning of string
   - Secret at end of string
   - Secret beyond max_length
   - Secret crossing truncation boundary
   - Long string with multiple credential assignments
   - Nested object containing long string
   - No raw secret in log, audit, or CLI output
   - Deterministic [TRUNCATED] marker verification
2. Static vs Dynamic Path Inspection in inspect_workflow_plan:
   - Static relative path (ALLOWED)
   - Dynamic relative path with {param} (UNRESOLVED)
   - Template variable with <param> (UNRESOLVED)
   - Scheme-relative URL //evil.com (DENIED)
   - Path traversal sequence .. (DENIED)
   - URL outside allowlist (DENIED)
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from app.persistence.models import (
    AuditEventModel,
    Base,
    JobModel,
    JobStatus,
    WorkflowApprovalStatus,
)
from app.persistence.repository import (
    AccountRepository,
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
def sqlite_session_factory(sqlite_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(sqlite_engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def setup_db_provider(sqlite_session_factory) -> str:
    async with sqlite_session_factory() as session:
        prov_repo = ProviderRepository(session)
        await prov_repo.create_or_update(
            provider_id="rewardlab",
            adapter_kind="mock",
            policy_version=1,
        )

        wf_repo = WorkflowVersionRepository(session)
        wf = await wf_repo.create_version(
            workflow_key="hotfix_wf",
            version=1,
            provider_id="rewardlab",
            definition_json={
                "id": "hotfix_wf",
                "provider": "rewardlab",
                "schema_version": 1,
                "steps": [{"id": "s1", "action": "navigate", "path": "/tasks"}],
            },
            approval_status=WorkflowApprovalStatus.APPROVED,
        )

        acc_repo = AccountRepository(session)
        await acc_repo.create_account(
            provider_id="rewardlab",
            owner_id="op_hotfix",
            label="Hotfix Test",
            account_id="acc_hotfix_01",
        )
        await session.commit()
        return wf.id


# =============================================================================
# 1. P0 Security Fix: Long Free-Text Redaction & Truncation Tests
# =============================================================================


def test_redact_free_text_secret_at_beginning() -> None:
    raw = "password=secret_beginning_123 followed by benign log details here"
    sanitized = redact_free_text(raw)
    assert "secret_beginning_123" not in sanitized
    assert "password=[REDACTED]" in sanitized
    assert "benign log details" in sanitized
    assert "[TRUNCATED]" not in sanitized


def test_redact_free_text_secret_at_end() -> None:
    raw = "Some benign system context message and token=secret_end_456"
    sanitized = redact_free_text(raw)
    assert "secret_end_456" not in sanitized
    assert "token=[REDACTED]" in sanitized
    assert "Some benign system context" in sanitized
    assert "[TRUNCATED]" not in sanitized


def test_redact_free_text_secret_beyond_max_length() -> None:
    # Secret is positioned after max_length (50)
    prefix = "A" * 60
    secret = "password=hidden_beyond_threshold_999"
    raw = f"{prefix} {secret}"

    sanitized = redact_free_text(raw, max_length=50)
    assert "hidden_beyond_threshold_999" not in sanitized
    assert "[TRUNCATED]" in sanitized
    # Preserved prefix should be retained up to max_length
    assert sanitized.startswith("A" * 50)


def test_redact_free_text_secret_crossing_truncation_boundary() -> None:
    # "password=secret_crossing_abc" starts at index 20, cut at 30
    prefix = "X" * 20
    # "password=sec" is inside target, "ret_crossing_abc" is cut off
    raw = f"{prefix}password=secret_crossing_abc"

    sanitized = redact_free_text(raw, max_length=32)
    assert "secret_crossing_abc" not in sanitized
    assert "password=[REDACTED]" in sanitized
    assert "[TRUNCATED]" in sanitized


def test_redact_free_text_severed_key_at_boundary() -> None:
    # Boundary cuts right after "password="
    prefix = "B" * 20
    raw = f"{prefix}password=my_severed_secret_xyz"
    # Target length exactly up to "password="
    cut_len = len(prefix) + len("password=")

    sanitized = redact_free_text(raw, max_length=cut_len)
    assert "my_severed_secret_xyz" not in sanitized
    assert "password=[REDACTED]" in sanitized
    assert "[TRUNCATED]" in sanitized


def test_redact_free_text_multiple_credentials_in_long_string() -> None:
    raw = (
        "auth: Bearer token_one_123, "
        "password=pass_two_456, "
        "api_key=key_three_789, "
        "benign=normal_data"
    )
    sanitized = redact_free_text(raw)
    assert "token_one_123" not in sanitized
    assert "pass_two_456" not in sanitized
    assert "key_three_789" not in sanitized
    assert "Bearer [REDACTED]" in sanitized
    assert "password=[REDACTED]" in sanitized
    assert "api_key=[REDACTED]" in sanitized
    assert "benign=normal_data" in sanitized


def test_redact_data_nested_object_with_long_string() -> None:
    nested = {
        "user": "operator",
        "metadata": {
            "trace": "C" * 100 + " password=nested_secret_pass",
            "items": ["safe_item", "token=item_secret_token"],
        },
    }
    cleaned = redact_data(nested)
    trace_clean = cleaned["metadata"]["trace"]
    # With default max_length=65536, password inside trace is redacted
    assert "nested_secret_pass" not in trace_clean
    assert "password=[REDACTED]" in trace_clean
    assert "item_secret_token" not in cleaned["metadata"]["items"][1]
    assert "token=[REDACTED]" in cleaned["metadata"]["items"][1]


def test_redaction_filter_suppresses_long_record() -> None:
    filter_ = RedactionFilter()
    msg = "D" * 100 + " token=record_secret_999"
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    filter_.filter(record)
    assert "record_secret_999" not in record.msg
    assert "token=[REDACTED]" in record.msg


@pytest.mark.asyncio
async def test_write_time_redaction_with_truncation_in_audit_and_job(
    sqlite_session_factory, setup_db_provider
) -> None:
    wf_id = setup_db_provider
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf_id, "acc_hotfix_01", "idem_hotfix_audit")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        await session.commit()
        job_id = job.id

    # Test review_job with long reason containing secret at the end
    long_reason = "Operator review: " + ("E" * 200) + " password=hotfix_operator_secret"
    async with sqlite_session_factory() as session:
        service = JobService(session)
        await service.review_job(job_id=job_id, decision="cancel", reason=long_reason)
        await session.commit()

    async with sqlite_session_factory() as session:
        stmt = select(JobModel).where(JobModel.id == job_id)
        res = await session.execute(stmt)
        db_job = res.scalar_one()
        assert "hotfix_operator_secret" not in db_job.error_message
        assert "password=[REDACTED]" in db_job.error_message

        stmt_audit = select(AuditEventModel).where(
            AuditEventModel.resource == f"job:{job_id}"
        )
        res_audit = await session.execute(stmt_audit)
        events = res_audit.scalars().all()
        assert len(events) >= 1
        audit_str = str(events[-1].sanitized_details)
        assert "hotfix_operator_secret" not in audit_str
        assert "password=[REDACTED]" in audit_str


@pytest.mark.asyncio
async def test_cli_show_redacts_long_error_message(
    sqlite_session_factory, setup_db_provider
) -> None:
    wf_id = setup_db_provider
    long_error = "Failure at step: " + ("F" * 150) + " token=cli_long_token_val"
    async with sqlite_session_factory() as session:
        job_repo = JobRepository(session)
        job = await job_repo.create_job(wf_id, "acc_hotfix_01", "idem_cli_long_err")
        job.status = JobStatus.PAUSED_FOR_REVIEW.value
        job.error_message = long_error
        await session.commit()
        job_id = job.id

    parser = build_jobs_parser()

    @asynccontextmanager
    async def mock_db():
        async with sqlite_session_factory() as s:
            yield s

    # Text output
    args_text = parser.parse_args(["show", job_id])
    with patch("app.operator_cli.get_db_session", mock_db):
        code_text, out_text = await execute_jobs_async(args_text)
    assert code_text == 0
    assert "cli_long_token_val" not in out_text
    assert "token=[REDACTED]" in out_text

    # JSON output
    args_json = parser.parse_args(["show", job_id, "--json"])
    with patch("app.operator_cli.get_db_session", mock_db):
        code_json, out_json = await execute_jobs_async(args_json)
    assert code_json == 0
    assert "cli_long_token_val" not in out_json
    data = json.loads(out_json)
    assert "token=[REDACTED]" in data["error_message"]


# =============================================================================
# 2. P1 Fix: Dynamic Relative Path & Destination Inspection Tests
# =============================================================================


def test_inspector_allowed_static_relative_path(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_static_path
provider: rewardlab
steps:
  - id: s1
    action: navigate
    path: /tasks
"""
    f = tmp_path / "static_path.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "ALLOWED"
    assert report["steps"][0]["destination_status"] == "ALLOWED"
    assert "Valid relative application path" in report["steps"][0]["destination_reason"]


def test_inspector_unresolved_dynamic_relative_path_curly(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_dynamic_path_curly
provider: rewardlab
steps:
  - id: s1
    action: navigate
    path: /tasks/{task_id}
"""
    f = tmp_path / "dynamic_curly.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "UNRESOLVED"
    assert report["steps"][0]["destination_status"] == "UNRESOLVED"
    assert (
        "dynamic template parameters"
        in report["steps"][0]["destination_reason"]
    )
    assert len(report["policy_evaluation"]["unresolved_destinations"]) == 1


def test_inspector_unresolved_dynamic_relative_path_angle(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_dynamic_path_angle
provider: rewardlab
steps:
  - id: s1
    action: navigate
    path: /tasks/<task_id>/review
"""
    f = tmp_path / "dynamic_angle.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "UNRESOLVED"
    assert report["steps"][0]["destination_status"] == "UNRESOLVED"
    assert (
        "dynamic template parameters"
        in report["steps"][0]["destination_reason"]
    )


def test_inspector_denies_scheme_relative_url(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_scheme_rel
provider: rewardlab
steps:
  - id: s1
    action: navigate
    path: //evil.attacker.com/tasks
"""
    f = tmp_path / "scheme_rel.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "DENIED"
    assert report["steps"][0]["destination_status"] == "DENIED"
    assert "scheme-relative" in report["steps"][0]["destination_reason"]


def test_inspector_denies_path_traversal(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_path_traversal
provider: rewardlab
steps:
  - id: s1
    action: navigate
    path: /tasks/../internal/secret
"""
    f = tmp_path / "traversal.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "DENIED"
    assert report["steps"][0]["destination_status"] == "DENIED"
    assert "Path traversal" in report["steps"][0]["destination_reason"]


def test_inspector_denies_url_outside_allowlist(tmp_path: Path) -> None:
    wf_yaml = """
schema_version: 1
id: test_out_of_allowlist
provider: rewardlab
steps:
  - id: s1
    action: navigate
    url: https://unauthorized.target.com/tasks
"""
    f = tmp_path / "out_allowlist.yaml"
    f.write_text(wf_yaml, encoding="utf-8")

    code, report = inspect_workflow_plan(f)
    assert code == 0
    assert report["preflight_verdict"] == "DENIED"
    assert report["steps"][0]["destination_status"] == "DENIED"
    violations = report["policy_evaluation"]["destination_violations"]
    assert "not in provider domain allowlist" in violations[0]
