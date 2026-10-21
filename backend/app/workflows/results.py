import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StepStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"


class WorkflowExecutionStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    POLICY_DENIED = "POLICY_DENIED"
    CANCELLED = "CANCELLED"


class StepExecutionResult(BaseModel):
    """Execution outcome for a single step in a workflow."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    action: str
    status: StepStatus
    duration_ms: float = Field(ge=0.0)
    output_data: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None


class WorkflowExecutionResult(BaseModel):
    """Aggregate execution outcome for an entire workflow run."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str
    provider: str
    status: WorkflowExecutionStatus
    steps_executed: int = Field(default=0, ge=0)
    total_steps: int = Field(default=0, ge=0)
    step_results: list[StepExecutionResult] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_seconds: float = Field(default=0.0, ge=0.0)
    can_retry: bool = False
    requires_verification: bool = False
    artifacts: list[str] = Field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return self.status == WorkflowExecutionStatus.SUCCESS
