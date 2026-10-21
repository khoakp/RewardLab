"""Job state machine transitions, failure classification, and bounded backoff (Milestone M3.3).

Invariants:
- Terminal states (SUCCEEDED, FAILED, CANCELLED, REJECTED) are immutable.
- Side effects that failed or timed out without verified postcondition are classified
  as SIDE_EFFECT_AMBIGUOUS and are strictly non-retryable (can_retry = False).
- Deterministic bounded exponential backoff prevents thundering herds.
"""

from __future__ import annotations

from app.persistence.models import (
    VALID_JOB_TRANSITIONS,
    FailureCategory,
    JobStatus,
)

TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.REJECTED,
    }
)


class JobStateMachineError(ValueError):
    """Base exception for job state machine violations."""


class IllegalStateTransitionError(JobStateMachineError):
    """Raised when an illegal status jump is attempted."""


class TerminalStateModificationError(IllegalStateTransitionError):
    """Raised when attempting to transition a job out of a terminal state."""


class IdempotencyConflictError(JobStateMachineError):
    """Raised when an idempotency key is reused with conflicting parameters."""


class LockAcquisitionError(JobStateMachineError):
    """Raised when an account execution lock cannot be acquired due to active concurrency."""


class SideEffectVerificationError(JobStateMachineError):
    """Raised when a step with side effects fails or cannot be verified."""


def validate_transition(current: JobStatus, target: JobStatus) -> None:
    """Validates state machine transition rules.

    Raises:
        TerminalStateModificationError: If job is already in a terminal state.
        IllegalStateTransitionError: If the transition is not in VALID_JOB_TRANSITIONS.
    """
    if current in TERMINAL_JOB_STATUSES:
        raise TerminalStateModificationError(
            f"Job is in terminal state '{current.value}' and cannot transition to '{target.value}'."
        )

    allowed = VALID_JOB_TRANSITIONS.get(current, set())
    if target not in allowed:
        allowed_str = sorted([s.value for s in allowed])
        raise IllegalStateTransitionError(
            f"Invalid job state transition from {current} to {target}. "
            f"Allowed transitions: {allowed_str}"
        )


def classify_failure(
    error_code: str | None,
    requires_verification: bool = False,
) -> tuple[FailureCategory, bool]:
    """Classifies execution failure and determines whether the job can be safely retried.

    Guarantees:
    - If requires_verification is True (step had side effects), retry is strictly prohibited
      to prevent duplicate external mutations.
    """
    if requires_verification:
        return FailureCategory.SIDE_EFFECT_AMBIGUOUS, False

    code = (error_code or "").upper()

    if "POLICY" in code:
        return FailureCategory.POLICY_DENIED, False
    if "VALIDAT" in code or "SCHEMA" in code or "SYNTAX" in code:
        return FailureCategory.VALIDATION_ERROR, False
    if "AUTH" in code or "CREDENTIAL" in code or "SESSION" in code:
        return FailureCategory.AUTH_FAILED, False
    if "LOCATOR" in code or "SELECTOR" in code or "ELEMENT" in code:
        return FailureCategory.LOCATOR_NOT_FOUND, True
    if "TIMEOUT" in code:
        return FailureCategory.ACTION_TIMEOUT, True
    if "DISCONNECT" in code or "CRASH" in code or "NETWORK" in code:
        return FailureCategory.INFRASTRUCTURE_FAILURE, True

    return FailureCategory.SYSTEM_ERROR, False


def compute_backoff_seconds(
    attempt: int,
    base_seconds: float = 2.0,
    max_seconds: float = 60.0,
    factor: float = 2.0,
) -> float:
    """Computes bounded exponential backoff delay in seconds."""
    if attempt <= 1:
        return base_seconds
    backoff = base_seconds * (factor ** (attempt - 1))
    return min(backoff, max_seconds)
