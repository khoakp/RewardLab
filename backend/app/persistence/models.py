"""SQLAlchemy 2.0 ORM models for ADBAP database persistence (Milestone M3.1).

Defines core tables adhering strictly to ARCHITECTURE.md Section 5:
- providers
- accounts
- session_refs
- workflow_versions
- jobs
- job_steps
- nodes
- audit_events
- activity_records
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.database import Base


def utc_now() -> datetime:
    """Returns timezone-aware UTC datetime."""
    return datetime.now(UTC)


def generate_uuid() -> str:
    """Generates standard UUIDv4 string."""
    return str(uuid.uuid4())


# =============================================================================
# Domain Enums for State Machine
# =============================================================================


class JobStatus(StrEnum):
    """Job lifecycle states per ARCHITECTURE.md Section 6."""

    PENDING = "PENDING"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RETRY_WAIT = "RETRY_WAIT"
    PAUSED_FOR_REVIEW = "PAUSED_FOR_REVIEW"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class StepExecutionStatus(StrEnum):
    """Job step execution status."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    TIMEOUT = "TIMEOUT"


class AccountAuthState(StrEnum):
    """Account authentication lifecycle status."""

    ACTIVE = "ACTIVE"
    READY = "READY"
    EXPIRED = "EXPIRED"
    LOCKED = "LOCKED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    UNAUTHENTICATED = "UNAUTHENTICATED"


class NodeState(StrEnum):
    """Worker node lifecycle state."""

    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"


class WorkflowApprovalStatus(StrEnum):
    """Workflow version approval lifecycle status."""

    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SessionStatus(StrEnum):
    """Session lifecycle and validity status."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    INVALID = "INVALID"
    REAUTH_REQUIRED = "REAUTH_REQUIRED"


class FailureCategory(StrEnum):
    """Structured failure categories for workflow job execution."""

    POLICY_DENIED = "POLICY_DENIED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTH_FAILED = "AUTH_FAILED"
    LOCATOR_NOT_FOUND = "LOCATOR_NOT_FOUND"
    ACTION_TIMEOUT = "ACTION_TIMEOUT"
    SIDE_EFFECT_AMBIGUOUS = "SIDE_EFFECT_AMBIGUOUS"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    SYSTEM_ERROR = "SYSTEM_ERROR"


# Valid state transitions for the Job state machine
VALID_JOB_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {
        JobStatus.VALIDATING,
        JobStatus.QUEUED,
        JobStatus.CANCELLED,
        JobStatus.REJECTED,
    },
    JobStatus.VALIDATING: {JobStatus.QUEUED, JobStatus.REJECTED, JobStatus.CANCELLED},
    JobStatus.QUEUED: {JobStatus.LEASED, JobStatus.CANCELLED, JobStatus.PAUSED_FOR_REVIEW},
    JobStatus.LEASED: {JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.RUNNING: {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.RETRY_WAIT,
        JobStatus.PAUSED_FOR_REVIEW,
        JobStatus.CANCELLED,
    },
    JobStatus.RETRY_WAIT: {JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.PAUSED_FOR_REVIEW: {JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.REJECTED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
    JobStatus.REJECTED: set(),
}


# =============================================================================
# SQLAlchemy ORM Models
# =============================================================================


class ProviderModel(Base):
    """Provider configuration, policy version, and capability declaration."""

    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    adapter_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    # Relationships
    accounts: Mapped[list[AccountModel]] = relationship(
        "AccountModel", back_populates="provider", cascade="all, delete-orphan"
    )
    workflow_versions: Mapped[list[WorkflowVersionModel]] = relationship(
        "WorkflowVersionModel", back_populates="provider"
    )


class NodeModel(Base):
    """Execution worker node metadata and capacity advertisement."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_uuid)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    node_type: Mapped[str] = mapped_column(String(64), default="local", nullable=False)
    capability_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default=NodeState.ACTIVE, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AccountModel(Base):
    """Test account entity isolated per provider and owner."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint(
            "provider_id", "owner_id", "label", name="uq_accounts_provider_owner_label"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_uuid)
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    assigned_node_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True
    )
    auth_state: Mapped[str] = mapped_column(
        String(32), default=AccountAuthState.ACTIVE, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    # Relationships
    provider: Mapped[ProviderModel] = relationship("ProviderModel", back_populates="accounts")
    session_refs: Mapped[list[SessionRefModel]] = relationship(
        "SessionRefModel", back_populates="account", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[JobModel]] = relationship("JobModel", back_populates="account")


class SessionRefModel(Base):
    """Encrypted session pointer. Plaintext cookies or credentials must NEVER be stored here."""

    __tablename__ = "session_refs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_uuid)
    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vault_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    encrypted_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=SessionStatus.ACTIVE, nullable=False, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    # Relationship
    account: Mapped[AccountModel] = relationship("AccountModel", back_populates="session_refs")


class WorkflowVersionModel(Base):
    """Immutable versioned workflow definition."""

    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint("workflow_key", "version", name="uq_workflow_versions_key_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_uuid)
    workflow_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    definition_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    approval_status: Mapped[str] = mapped_column(
        String(32), default=WorkflowApprovalStatus.DRAFT, nullable=False
    )
    is_immutable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    # Relationships
    provider: Mapped[ProviderModel] = relationship(
        "ProviderModel", back_populates="workflow_versions"
    )
    jobs: Mapped[list[JobModel]] = relationship("JobModel", back_populates="workflow_version")


class JobModel(Base):
    """Workflow execution job instance with idempotency key and lease token."""

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_uuid)
    workflow_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default=JobStatus.PENDING, nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_token: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    # Relationships
    workflow_version: Mapped[WorkflowVersionModel] = relationship(
        "WorkflowVersionModel", back_populates="jobs"
    )
    account: Mapped[AccountModel] = relationship("AccountModel", back_populates="jobs")
    steps: Mapped[list[JobStepModel]] = relationship(
        "JobStepModel", back_populates="job", cascade="all, delete-orphan"
    )


class JobStepModel(Base):
    """Step execution status and diagnostics per job attempt."""

    __tablename__ = "job_steps"
    __table_args__ = (
        UniqueConstraint("job_id", "step_key", "attempt", name="uq_job_steps_job_step_attempt"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_uuid)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=StepExecutionStatus.PENDING, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Relationship
    job: Mapped[JobModel] = relationship("JobModel", back_populates="steps")


class AuditEventModel(Base):
    """Immutable audit trail for authorization, job actions, and policy decisions."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_uuid)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resource: Mapped[str] = mapped_column(String(256), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    sanitized_details: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )


class ActivityRecordModel(Base):
    """External activity tracking and user confirmation record."""

    __tablename__ = "activity_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_uuid)
    provider_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    account_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    activity_key: Mapped[str] = mapped_column(String(128), nullable=False)
    user_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AccountExecutionLockModel(Base):
    """Database-level per-account mutual exclusion lock for active execution (M3.3).

    Guarantees:
    - account_id is PRIMARY KEY: exactly one active job execution lock per account.
    - job_id is UNIQUE: a job can hold at most one account lock.
    - Foreign keys with cascade delete clean up on account or job removal.
    """

    __tablename__ = "account_execution_locks"

    account_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
