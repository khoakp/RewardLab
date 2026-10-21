"""Android Execution Adapter Spike (ADBAP Personal P3).

Defines the minimal typed execution contract for an Android standalone worker:
- Accepts a validated workflow definition
- Maps actions (navigate, wait, click, fill, verify_state)
- Tracks causal mutations and verifies postconditions
- Guarantees zero secret leakage in logs and structured results
- Operates independently without requiring a desktop Playwright process
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.observability.logging import redact_data, redact_free_text


class AndroidExecutionStatus(StrEnum):
    INITIALIZED = "INITIALIZED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PAUSED_FOR_REVIEW = "PAUSED_FOR_REVIEW"
    CANCELLED = "CANCELLED"


class AndroidOutcomeType(StrEnum):
    ACTION_EXECUTED = "ACTION_EXECUTED"
    STATE_VERIFIED = "STATE_VERIFIED"
    OUTCOME_UNCONFIRMED = "OUTCOME_UNCONFIRMED"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"


@dataclass
class AndroidStepResult:
    step_id: str
    action: str
    status: str
    is_mutating: bool = False
    duration_ms: float = 0.0
    error_message: str | None = None
    output_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AndroidExecutionResult:
    """Standardized result schema adhering to ADBAP Personal P3 Contract."""

    workflow_id: str
    execution_id: str
    account_id: str
    status: AndroidExecutionStatus
    outcome_type: AndroidOutcomeType
    verified_mutations: list[str] = field(default_factory=list)
    unverified_mutations: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    requires_verification: bool = False
    duration_seconds: float = 0.0
    steps: list[AndroidStepResult] = field(default_factory=list)
    started_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    completed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Converts result to sanitized dictionary with full secret redaction."""
        raw = {
            "workflow_id": self.workflow_id,
            "execution_id": self.execution_id,
            "account_id": self.account_id,
            "status": self.status.value,
            "outcome_type": self.outcome_type.value,
            "verified_mutations": self.verified_mutations,
            "unverified_mutations": self.unverified_mutations,
            "error_code": self.error_code,
            "error_message": (
                redact_free_text(self.error_message) if self.error_message else None
            ),
            "requires_verification": self.requires_verification,
            "duration_seconds": round(self.duration_seconds, 3),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "steps": [
                {
                    "step_id": s.step_id,
                    "action": s.action,
                    "status": s.status,
                    "is_mutating": s.is_mutating,
                    "duration_ms": round(s.duration_ms, 2),
                    "error_message": (
                        redact_free_text(s.error_message) if s.error_message else None
                    ),
                    "output_data": redact_data(s.output_data),
                }
                for s in self.steps
            ],
        }
        return redact_data(raw)


class AndroidExecutionAdapter:
    """Minimal execution adapter mapping ADBAP DSL workflows to Android execution."""

    def __init__(self, provider: str = "rewardlab"):
        self.provider = provider

    def execute_workflow_dict(
        self,
        workflow_def: dict[str, Any],
        account_id: str,
        session_token: str | None = None,
        driver_bridge: Any = None,
    ) -> AndroidExecutionResult:
        """Executes a workflow definition using the provided driver bridge (e.g. WebView).

        If driver_bridge is None, operates in safe validation mode.
        """
        start_time = time.monotonic()
        execution_id = f"exec_android_{uuid.uuid4().hex[:12]}"
        workflow_id = str(workflow_def.get("id", "unknown_wf"))

        mutating_steps: list[str] = []
        verified_mutations: list[str] = []
        step_results: list[AndroidStepResult] = []

        # 1. Statically inspect verification targets and mutating steps
        steps = workflow_def.get("steps", [])
        for s in steps:
            action = s.get("action", "")
            step_id = s.get("id", "")
            default_side_effect = action in ("click", "fill")
            is_mut = action in ("click", "fill", "press_key") and s.get(
                "side_effect", default_side_effect
            )
            if is_mut:
                mutating_steps.append(step_id)

        # 2. Iterate and execute steps
        failed = False
        failure_code: str | None = None
        failure_msg: str | None = None

        for s in steps:
            step_id = s.get("id", "")
            action = s.get("action", "")
            is_mut = step_id in mutating_steps
            t0 = time.monotonic()

            try:
                if action == "request_human_action":
                    # P1 Human Action Halt invariant
                    step_results.append(
                        AndroidStepResult(
                            step_id=step_id,
                            action=action,
                            status="HALTED",
                            is_mutating=False,
                            duration_ms=(time.monotonic() - t0) * 1000,
                        )
                    )
                    duration = time.monotonic() - start_time
                    unverified = [m for m in mutating_steps if m not in verified_mutations]
                    return AndroidExecutionResult(
                        workflow_id=workflow_id,
                        execution_id=execution_id,
                        account_id=account_id,
                        status=AndroidExecutionStatus.PAUSED_FOR_REVIEW,
                        outcome_type=AndroidOutcomeType.HUMAN_ACTION_REQUIRED,
                        verified_mutations=verified_mutations,
                        unverified_mutations=unverified,
                        error_code="HUMAN_ACTION_REQUIRED",
                        error_message="Workflow halted for human action checkpoint",
                        requires_verification=len(unverified) > 0,
                        duration_seconds=duration,
                        steps=step_results,
                        completed_at=datetime.now(UTC).isoformat(),
                    )

                if driver_bridge:
                    bridge_res = driver_bridge.execute_step(s, session_token=session_token)
                    step_status = bridge_res.get("status", "SUCCESS")
                    step_err = bridge_res.get("error")
                else:
                    # Simulated bridge execution for unit validation
                    step_status = "SUCCESS"
                    step_err = None

                if step_status != "SUCCESS":
                    failed = True
                    failure_code = "STEP_EXECUTION_FAILED"
                    failure_msg = step_err or f"Step '{step_id}' failed in driver"
                    step_results.append(
                        AndroidStepResult(
                            step_id=step_id,
                            action=action,
                            status="FAILED",
                            is_mutating=is_mut,
                            duration_ms=(time.monotonic() - t0) * 1000,
                            error_message=failure_msg,
                        )
                    )
                    break

                # If verify_state succeeded, resolve its explicit targets
                if action == "verify_state":
                    targets = s.get("verifies_step_ids")
                    if targets:
                        for tid in targets:
                            if tid in mutating_steps and tid not in verified_mutations:
                                verified_mutations.append(tid)
                    elif len(mutating_steps) == 1 and mutating_steps[0] not in verified_mutations:
                        # Legacy single mutation inference
                        verified_mutations.append(mutating_steps[0])

                step_results.append(
                    AndroidStepResult(
                        step_id=step_id,
                        action=action,
                        status="SUCCESS",
                        is_mutating=is_mut,
                        duration_ms=(time.monotonic() - t0) * 1000,
                    )
                )

            except Exception as ex:
                failed = True
                failure_code = "UNHANDLED_EXCEPTION"
                failure_msg = str(ex)
                step_results.append(
                    AndroidStepResult(
                        step_id=step_id,
                        action=action,
                        status="FAILED",
                        is_mutating=is_mut,
                        duration_ms=(time.monotonic() - t0) * 1000,
                        error_message=failure_msg,
                    )
                )
                break

        duration = time.monotonic() - start_time
        unverified = [m for m in mutating_steps if m not in verified_mutations]

        # 3. Classify outcome
        if failed:
            has_executed_mutation = any(
                s.status == "SUCCESS" for s in step_results if s.is_mutating
            )
            if len(unverified) > 0 and has_executed_mutation:
                # Mutating side-effect occurred but subsequent execution failed
                status = AndroidExecutionStatus.PAUSED_FOR_REVIEW
                outcome = AndroidOutcomeType.OUTCOME_UNCONFIRMED
                req_verif = True
                failure_code = failure_code or "SIDE_EFFECT_AMBIGUOUS"
            else:
                status = AndroidExecutionStatus.FAILED
                outcome = AndroidOutcomeType.PROVIDER_REJECTED
                req_verif = False
        else:
            if len(mutating_steps) > 0:
                if len(unverified) == 0:
                    status = AndroidExecutionStatus.SUCCEEDED
                    outcome = AndroidOutcomeType.STATE_VERIFIED
                    req_verif = False
                else:
                    status = AndroidExecutionStatus.PAUSED_FOR_REVIEW
                    outcome = AndroidOutcomeType.OUTCOME_UNCONFIRMED
                    req_verif = True
                    failure_code = "SIDE_EFFECT_AMBIGUOUS"
                    failure_msg = f"Mutations left unverified: {unverified}"
            else:
                status = AndroidExecutionStatus.SUCCEEDED
                outcome = AndroidOutcomeType.ACTION_EXECUTED
                req_verif = False

        return AndroidExecutionResult(
            workflow_id=workflow_id,
            execution_id=execution_id,
            account_id=account_id,
            status=status,
            outcome_type=outcome,
            verified_mutations=verified_mutations,
            unverified_mutations=unverified,
            error_code=failure_code,
            error_message=failure_msg,
            requires_verification=req_verif,
            duration_seconds=duration,
            steps=step_results,
            completed_at=datetime.now(UTC).isoformat(),
        )
