"""Tests for AndroidExecutionAdapter contract (Milestone Personal P3 & P3.1)."""

import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
if str(repo_root / "backend") not in sys.path:
    sys.path.insert(0, str(repo_root / "backend"))

from spikes.android_worker_poc.android_adapter import (  # noqa: E402
    AndroidExecutionAdapter,
    AndroidExecutionStatus,
    AndroidOutcomeType,
)


def test_android_adapter_verified_workflow() -> None:
    adapter = AndroidExecutionAdapter()
    wf = {
        "id": "android_wf_verified",
        "provider": "rewardlab",
        "steps": [
            {"id": "s1", "action": "navigate", "path": "/tasks/task_01"},
            {"id": "s2", "action": "click", "side_effect": True, "target": "mark-reviewed-btn"},
            {"id": "s3", "action": "verify_state", "verifies_step_ids": ["s2"]},
        ],
    }

    res = adapter.execute_workflow_dict(wf, account_id="test_01")
    assert res.status == AndroidExecutionStatus.SUCCEEDED
    assert res.outcome_type == AndroidOutcomeType.STATE_VERIFIED
    assert res.verified_mutations == ["s2"]
    assert len(res.unverified_mutations) == 0
    assert res.requires_verification is False


def test_android_adapter_unverified_mutation_pauses_for_review() -> None:
    adapter = AndroidExecutionAdapter()
    wf = {
        "id": "android_wf_unconfirmed",
        "provider": "rewardlab",
        "steps": [
            {"id": "s1", "action": "navigate", "path": "/tasks/task_01"},
            {"id": "s2", "action": "click", "side_effect": True, "target": "mark-reviewed-btn"},
        ],
    }

    res = adapter.execute_workflow_dict(wf, account_id="test_01")
    assert res.status == AndroidExecutionStatus.PAUSED_FOR_REVIEW
    assert res.outcome_type == AndroidOutcomeType.OUTCOME_UNCONFIRMED
    assert res.unverified_mutations == ["s2"]
    assert res.requires_verification is True
    assert res.error_code == "SIDE_EFFECT_AMBIGUOUS"


def test_android_adapter_read_only_ui_fixtures() -> None:
    """Phase B Test 1: Read-only fixtures automation without side effects."""
    adapter = AndroidExecutionAdapter()
    wf = {
        "id": "android_wf_readonly",
        "provider": "rewardlab",
        "steps": [
            {"id": "s1", "action": "navigate", "path": "/ui-fixtures"},
            {"id": "s2", "action": "wait_visible", "target": "delayed-btn"},
            {"id": "s3", "action": "extract_text", "target": "delayed-btn"},
        ],
    }

    res = adapter.execute_workflow_dict(wf, account_id="test_01")
    assert res.status == AndroidExecutionStatus.SUCCEEDED
    assert res.outcome_type == AndroidOutcomeType.ACTION_EXECUTED
    assert len(res.verified_mutations) == 0
    assert len(res.unverified_mutations) == 0
    assert res.requires_verification is False


def test_android_adapter_human_action_halts_immediately() -> None:
    adapter = AndroidExecutionAdapter()
    wf = {
        "id": "android_wf_human",
        "provider": "rewardlab",
        "steps": [
            {"id": "s1", "action": "navigate", "path": "/tasks/task_01"},
            {"id": "s2", "action": "request_human_action", "reason": "MFA checkpoint"},
            {"id": "s3", "action": "click", "side_effect": True, "target": "btn_after_human"},
        ],
    }

    res = adapter.execute_workflow_dict(wf, account_id="test_01")
    assert res.status == AndroidExecutionStatus.PAUSED_FOR_REVIEW
    assert res.outcome_type == AndroidOutcomeType.HUMAN_ACTION_REQUIRED
    # Step 3 must NOT have been executed
    assert len(res.steps) == 2
    assert res.steps[-1].action == "request_human_action"


def test_android_adapter_mutation_failure_halts_without_retry() -> None:
    """Test that failed mutation enters PAUSED_FOR_REVIEW and does NOT blind retry."""
    adapter = AndroidExecutionAdapter()
    wf = {
        "id": "android_wf_ambiguous",
        "provider": "rewardlab",
        "steps": [
            {"id": "s1", "action": "navigate", "path": "/tasks/task_01"},
            {"id": "s2", "action": "click", "side_effect": True, "target": "mark-reviewed-btn"},
            {"id": "s3", "action": "verify_state", "verifies_step_ids": ["s2"]},
        ],
    }

    class MockFailingPostconditionBridge:
        def execute_step(self, step, session_token=None):
            if step["id"] == "s3":
                return {"status": "FAILED", "error": "Postcondition element mismatch"}
            return {"status": "SUCCESS"}

    res = adapter.execute_workflow_dict(
        wf, account_id="test_01", driver_bridge=MockFailingPostconditionBridge()
    )
    assert res.status == AndroidExecutionStatus.PAUSED_FOR_REVIEW
    assert res.outcome_type == AndroidOutcomeType.OUTCOME_UNCONFIRMED
    assert "s2" in res.unverified_mutations
    assert res.requires_verification is True


def test_android_adapter_redacts_secrets_in_result_dict() -> None:
    adapter = AndroidExecutionAdapter()
    wf = {
        "id": "android_wf_secret",
        "provider": "rewardlab",
        "steps": [
            {"id": "s1", "action": "navigate", "path": "/login"},
        ],
    }

    class MockFailingBridge:
        def execute_step(self, step, session_token=None):
            return {
                "status": "FAILED",
                "error": "Failed with token=android_secret_token_abc and password=secret_pass_xyz",
            }

    res = adapter.execute_workflow_dict(
        wf, account_id="test_01", driver_bridge=MockFailingBridge()
    )
    out = res.to_dict()
    out_str = str(out)
    assert "android_secret_token_abc" not in out_str
    assert "secret_pass_xyz" not in out_str
    assert "token=[REDACTED]" in out_str
    assert "password=[REDACTED]" in out_str
