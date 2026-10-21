"""Comprehensive tests for ADBAP CLI Workflow Runner (Milestone M2.5).

Tests:
1. CLI help and argument validation.
2. Valid YAML/JSON workflow execution.
3. Invalid workflow path and malformed YAML.
4. Unsupported schema version.
5. Denied provider (Microsoft Rewards, unknown provider).
6. Unauthorized capability rejection.
7. Validate-only / dry-run (zero browser allocation, zero side effects).
8. JSON output format contract across success and failure modes.
9. Stable exit codes (SUCCESS=0, FAILED=1, POLICY_DENIED=2,
   TIMEOUT=3, CANCELLED=4, USAGE_OR_SCHEMA_ERROR=5).
10. Timeout, cancellation, and resource cleanup.
11. Secret redaction in stdout/stderr.
12. Real Chromium execution on RewardLab test server.
13. Subprocess execution at the OS process level.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import uvicorn

from app.cli import (
    ExitCode,
    build_parser,
    execute_cli_async,
    resolve_workflow_path,
    run_workflow_cli,
)
from app.workflows.results import (
    WorkflowExecutionResult,
    WorkflowExecutionStatus,
)

# ---------------------------------------------------------------------------
# Load RewardLab App dynamically for live integration tests
# ---------------------------------------------------------------------------
rewardlab_root = Path(__file__).resolve().parents[2] / "rewardlab"
if str(rewardlab_root) not in sys.path:
    sys.path.insert(0, str(rewardlab_root))

_spec = importlib.util.spec_from_file_location("rewardlab_cli_test_app", rewardlab_root / "app.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["rewardlab_cli_test_app"] = _mod
_spec.loader.exec_module(_mod)

rewardlab_app = _mod.app
rewardlab_store = _mod.store
fault_controller = _mod.fault_controller


def get_free_port() -> int:
    """Find an available TCP port on loopback."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def live_rewardlab_server() -> Generator[str, None, None]:
    """Spawn an actual Uvicorn server running RewardLab on loopback in a background thread."""
    port = get_free_port()
    config = uvicorn.Config(
        app=rewardlab_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"

    started = False
    for _ in range(50):
        try:
            with httpx.Client(base_url=base_url, timeout=1.0) as client:
                r = client.get("/api/health")
                if r.status_code == 200:
                    started = True
                    break
        except Exception:
            time.sleep(0.05)

    if not started:
        server.should_exit = True
        raise RuntimeError("RewardLab live server failed to launch within timeout")

    yield base_url

    server.should_exit = True
    thread.join(timeout=3.0)


@pytest.fixture
def reset_rewardlab():
    """Reset RewardLab state to baseline."""
    rewardlab_store.reset()
    fault_controller.reset()
    fault_controller.set_test_mode(True)
    yield
    rewardlab_store.reset()
    fault_controller.reset()
    fault_controller.set_test_mode(None)


# Path to repository sample workflow
SAMPLE_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "workflows"
    / "rewardlab_review_first_task.yaml"
)


# =============================================================================
# 1. CLI Help and Argument Validation Tests
# =============================================================================


def test_cli_help(capsys):
    """CLI --help returns 0 and displays help text with security notes."""
    exit_code = run_workflow_cli(["--help"])
    assert exit_code == 0

    captured = capsys.readouterr()
    stdout = captured.out
    assert "run-workflow" in stdout
    assert "--file" in stdout
    assert "--account" in stdout
    assert "--validate-only" in stdout
    assert "--json" in stdout
    assert "--timeout" in stdout
    assert "Security Notes:" in stdout
    assert "REWARDLAB_PASSWORD" in stdout


def test_cli_short_help(capsys):
    """CLI -h flag returns 0 and displays help text."""
    exit_code = run_workflow_cli(["-h"])
    assert exit_code == 0


def test_cli_argument_missing_workflow(capsys):
    """CLI without arguments returns exit code 5 (USAGE_OR_SCHEMA_ERROR)."""
    exit_code = run_workflow_cli([])
    assert exit_code == ExitCode.USAGE_OR_SCHEMA_ERROR

    captured = capsys.readouterr()
    assert "No workflow file specified" in (captured.err or captured.out)


def test_cli_argument_unknown_flag(capsys):
    """CLI with unknown flag returns exit code 5."""
    exit_code = run_workflow_cli(["--nonexistent-flag-xyz"])
    assert exit_code == ExitCode.USAGE_OR_SCHEMA_ERROR

    captured = capsys.readouterr()
    assert "Usage error:" in captured.err or "unrecognized arguments" in captured.err


def test_cli_path_resolution():
    """Verify resolve_workflow_path handles flags and positional combinations."""
    parser = build_parser()

    # 1. Flag --file
    args = parser.parse_args(["-f", "test.yaml"])
    assert resolve_workflow_path(args) == Path("test.yaml")

    # 2. Positional workflow path
    args = parser.parse_args(["test.yaml"])
    assert resolve_workflow_path(args) == Path("test.yaml")

    # 3. Subcommand style: run-workflow test.yaml
    args = parser.parse_args(["run-workflow", "test.yaml"])
    assert resolve_workflow_path(args) == Path("test.yaml")

    # 4. Subcommand style with --file
    args = parser.parse_args(["run-workflow", "-f", "test.yaml"])
    assert resolve_workflow_path(args) == Path("test.yaml")

    # 5. Empty
    args = parser.parse_args([])
    assert resolve_workflow_path(args) is None


# =============================================================================
# 2. File and Schema Validation Tests
# =============================================================================


def test_cli_nonexistent_workflow_path(capsys, tmp_path):
    """Specifying a non-existent file returns exit code 5."""
    missing = tmp_path / "does_not_exist.yaml"
    exit_code = run_workflow_cli(["-f", str(missing)])
    assert exit_code == ExitCode.USAGE_OR_SCHEMA_ERROR

    captured = capsys.readouterr()
    assert "Workflow file not found" in captured.err


def test_cli_nonexistent_workflow_path_json(capsys, tmp_path):
    """Specifying a non-existent file with --json returns valid JSON error with exit code 5."""
    missing = tmp_path / "does_not_exist.yaml"
    exit_code = run_workflow_cli(["-f", str(missing), "--json"])
    assert exit_code == ExitCode.USAGE_OR_SCHEMA_ERROR

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] == "FILE_NOT_FOUND"
    assert "not found" in data["message"].lower()


def test_cli_malformed_yaml(capsys, tmp_path):
    """Malformed YAML returns exit code 5."""
    bad_yaml = tmp_path / "broken.yaml"
    bad_yaml.write_text("schema_version: 1\n  invalid_indentation: true:\n", encoding="utf-8")

    exit_code = run_workflow_cli(["-f", str(bad_yaml)])
    assert exit_code == ExitCode.USAGE_OR_SCHEMA_ERROR

    captured = capsys.readouterr()
    assert "Syntax Error:" in captured.err


def test_cli_malformed_yaml_json(capsys, tmp_path):
    """Malformed YAML with --json returns structured JSON syntax error."""
    bad_yaml = tmp_path / "broken.yaml"
    bad_yaml.write_text("schema_version: 1\n  invalid_indentation: true:\n", encoding="utf-8")

    exit_code = run_workflow_cli(["-f", str(bad_yaml), "--json"])
    assert exit_code == ExitCode.USAGE_OR_SCHEMA_ERROR

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] == "SYNTAX_ERROR"


def test_cli_unsupported_schema_version(capsys, tmp_path):
    """Workflow with unsupported schema version returns exit code 5."""
    wf = tmp_path / "future_schema.yaml"
    wf.write_text(
        """schema_version: 99
id: future_test
provider: rewardlab
steps:
  - id: s1
    action: navigate
    path: /tasks
""",
        encoding="utf-8",
    )

    exit_code = run_workflow_cli(["-f", str(wf)])
    assert exit_code == ExitCode.USAGE_OR_SCHEMA_ERROR

    captured = capsys.readouterr()
    assert "Validation Error:" in captured.err


def test_cli_provider_mismatch(capsys, tmp_path):
    """CLI --provider mismatching workflow definition returns exit code 5."""
    wf = tmp_path / "wf.yaml"
    wf.write_text(
        """schema_version: 1
id: test_mismatch
provider: rewardlab
requested_capabilities: [read_tasks, mark_reviewed]
steps:
  - id: s1
    action: navigate
    path: /tasks
""",
        encoding="utf-8",
    )

    exit_code = run_workflow_cli(["-f", str(wf), "--provider", "other_provider"])
    assert exit_code == ExitCode.USAGE_OR_SCHEMA_ERROR

    captured = capsys.readouterr()
    assert "does not match" in (captured.err or captured.out)


# =============================================================================
# 3. Policy and Capability Enforcement Tests (Pre-dispatch, Zero Browser)
# =============================================================================


def test_cli_denied_provider_microsoft_rewards(capsys, tmp_path):
    """Microsoft Rewards workflow is rejected with exit code 2 (POLICY_DENIED)."""
    wf = tmp_path / "rewards_wf.yaml"
    wf.write_text(
        """schema_version: 1
id: rewards_auto
provider: microsoft_rewards
steps:
  - id: s1
    action: navigate
    path: /rewards
""",
        encoding="utf-8",
    )

    exit_code = run_workflow_cli(["-f", str(wf)])
    assert exit_code == ExitCode.POLICY_DENIED

    captured = capsys.readouterr()
    assert "Policy Denied:" in captured.err


def test_cli_denied_provider_unknown(capsys, tmp_path):
    """Unknown provider is rejected with exit code 2 (POLICY_DENIED)."""
    wf = tmp_path / "unknown_wf.yaml"
    wf.write_text(
        """schema_version: 1
id: unknown_auto
provider: malicious_site_xyz
steps:
  - id: s1
    action: navigate
    path: /home
""",
        encoding="utf-8",
    )

    exit_code = run_workflow_cli(["-f", str(wf)])
    assert exit_code == ExitCode.POLICY_DENIED


def test_cli_denied_provider_json_output(capsys, tmp_path):
    """Denied provider with --json outputs valid JSON with code 2."""
    wf = tmp_path / "rewards_wf.yaml"
    wf.write_text(
        """schema_version: 1
id: rewards_auto
provider: microsoft_rewards
steps:
  - id: s1
    action: navigate
    path: /rewards
""",
        encoding="utf-8",
    )

    exit_code = run_workflow_cli(["-f", str(wf), "--json"])
    assert exit_code == ExitCode.POLICY_DENIED

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] == "POLICY_DENIED"


def test_cli_unauthorized_capability_rejected(capsys, tmp_path):
    """Workflow requesting unauthorized capability returns exit code 2."""
    wf = tmp_path / "unauth_cap.yaml"
    wf.write_text(
        """schema_version: 1
id: test_cap
provider: rewardlab
requested_capabilities:
  - read_tasks
  - unapproved_capability_xyz
steps:
  - id: s1
    action: navigate
    path: /tasks
""",
        encoding="utf-8",
    )

    exit_code = run_workflow_cli(["-f", str(wf)])
    assert exit_code == ExitCode.POLICY_DENIED

    captured = capsys.readouterr()
    assert "Policy Denied:" in captured.err
    assert "unapproved_capability_xyz" in captured.err


# =============================================================================
# 4. Validate-Only / Dry-Run Tests
# =============================================================================


def test_cli_validate_only_valid_workflow(capsys):
    """Validate-only on valid sample workflow returns 0 without launching browser."""
    assert SAMPLE_WORKFLOW.exists(), f"Sample workflow not found: {SAMPLE_WORKFLOW}"

    exit_code = run_workflow_cli(["-f", str(SAMPLE_WORKFLOW), "--validate-only"])
    assert exit_code == ExitCode.SUCCESS

    captured = capsys.readouterr()
    assert "=== ADBAP Workflow Validation ===" in captured.out
    assert "VALID" in captured.out
    assert "rewardlab_review_first_task" in captured.out


def test_cli_dry_run_alias(capsys):
    """--dry-run works as an alias for --validate-only."""
    exit_code = run_workflow_cli(["-f", str(SAMPLE_WORKFLOW), "--dry-run"])
    assert exit_code == ExitCode.SUCCESS

    captured = capsys.readouterr()
    assert "VALID" in captured.out


def test_cli_validate_only_json(capsys):
    """Validate-only with --json returns structured JSON verification object."""
    exit_code = run_workflow_cli(["-f", str(SAMPLE_WORKFLOW), "--validate-only", "--json"])
    assert exit_code == ExitCode.SUCCESS

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["valid"] is True
    assert data["workflow_id"] == "rewardlab_review_first_task"
    assert data["provider"] == "rewardlab"
    assert data["schema_version"] == 1
    assert data["total_steps"] == 4
    assert "read_tasks" in data["requested_capabilities"]


def test_cli_validate_only_rejects_denied_provider(capsys, tmp_path):
    """Validate-only on denied provider fails with code 2."""
    wf = tmp_path / "rewards_wf.yaml"
    wf.write_text(
        """schema_version: 1
id: rewards_auto
provider: microsoft_rewards
steps:
  - id: s1
    action: navigate
    path: /rewards
""",
        encoding="utf-8",
    )

    exit_code = run_workflow_cli(["-f", str(wf), "--validate-only"])
    assert exit_code == ExitCode.POLICY_DENIED


# =============================================================================
# 5. Execution, Timeout, Cancellation, and Redaction Tests (Mocked Browser)
# =============================================================================


@pytest.mark.asyncio
async def test_cli_timeout_handling(tmp_path):
    """Context timeout returns exit code 3 (TIMEOUT)."""
    parser = build_parser()
    args = parser.parse_args(["-f", str(SAMPLE_WORKFLOW), "--timeout", "0.001"])

    mock_result = WorkflowExecutionResult(
        workflow_id="rewardlab_review_first_task",
        provider="rewardlab",
        status=WorkflowExecutionStatus.TIMEOUT,
        steps_executed=0,
        total_steps=4,
        step_results=[],
        error_code="CONTEXT_TIMEOUT",
        error_message="Browser context lifecycle timed out after 0.001s",
    )

    with patch("app.cli.BrowserManager") as MockBM:
        bm_instance = MockBM.return_value
        bm_instance.start = AsyncMock()
        bm_instance.stop = AsyncMock()
        bm_instance.execute_workflow = AsyncMock(return_value=mock_result)

        exit_code, output = await execute_cli_async(args)
        assert exit_code == ExitCode.TIMEOUT
        assert "TIMEOUT" in output
        bm_instance.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_cli_cancellation_handling():
    """Cancelled execution returns exit code 4 (CANCELLED)."""
    parser = build_parser()
    args = parser.parse_args(["-f", str(SAMPLE_WORKFLOW)])

    with patch("app.cli.BrowserManager") as MockBM:
        bm_instance = MockBM.return_value
        bm_instance.start = AsyncMock()
        bm_instance.stop = AsyncMock()
        bm_instance.execute_workflow = AsyncMock(side_effect=asyncio.CancelledError)

        exit_code, output = await execute_cli_async(args)
        assert exit_code == ExitCode.CANCELLED
        bm_instance.stop.assert_awaited_once()


def test_cli_secret_redaction_in_output(capsys, tmp_path):
    """Sensitive URLs with credentials must be redacted in human and JSON outputs."""
    wf = tmp_path / "secret_wf.yaml"
    wf.write_text(
        """schema_version: 1
id: secret_test
provider: rewardlab
requested_capabilities: [read_tasks, mark_reviewed]
steps:
  - id: s1
    action: navigate
    path: /tasks/task_01
""",
        encoding="utf-8",
    )

    # Base URL with embedded sensitive credentials
    dirty_url = "http://admin_user:super_secret_token_12345@127.0.0.1:8001"
    exit_code = run_workflow_cli(["-f", str(wf), "-b", dirty_url, "--validate-only"])
    assert exit_code == ExitCode.SUCCESS

    captured = capsys.readouterr()
    assert "super_secret_token_12345" not in captured.out
    assert "super_secret_token_12345" not in captured.err


# =============================================================================
# 6. Real Chromium Live Integration Tests on RewardLab
# =============================================================================


def test_cli_live_e2e_rewardlab_execution(live_rewardlab_server, reset_rewardlab, capsys):
    """Full live execution of sample workflow against real RewardLab server via Chromium."""
    account_id = "test_01"
    initial_user = rewardlab_store.users[account_id]
    initial_points = initial_user.points
    assert initial_points == 120

    env_backup = os.environ.get("REWARDLAB_PASSWORD")
    try:
        # Set account password for auto-login
        os.environ["REWARDLAB_PASSWORD"] = "test_password_01"

        exit_code = run_workflow_cli(
            [
                "-f",
                str(SAMPLE_WORKFLOW),
                "-b",
                live_rewardlab_server,
                "-a",
                account_id,
                "--headless",
            ]
        )
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        stdout = captured.out
        assert "=== ADBAP Workflow Execution Summary ===" in stdout
        assert "Overall Status       : SUCCESS" in stdout
        assert "Steps Executed       : 4/4" in stdout

        # Verify side effect on RewardLab
        updated_user = rewardlab_store.users[account_id]
        assert updated_user.points == 130
        task_01 = rewardlab_store.get_task_for_user(account_id, "task_01")
        assert task_01 is not None
        assert task_01.reviewed is True

        # Confirm test_02 account isolation
        assert rewardlab_store.users["test_02"].points == 50

    finally:
        if env_backup is not None:
            os.environ["REWARDLAB_PASSWORD"] = env_backup
        else:
            os.environ.pop("REWARDLAB_PASSWORD", None)


def test_cli_live_e2e_rewardlab_json_output(live_rewardlab_server, reset_rewardlab, capsys):
    """Full live execution with --json outputs structured, valid JSON matching execution schema."""
    account_id = "test_02"
    initial_points = rewardlab_store.users[account_id].points
    assert initial_points == 50

    env_backup = os.environ.get("REWARDLAB_PASSWORD")
    try:
        os.environ["REWARDLAB_PASSWORD"] = "test_password_02"

        exit_code = run_workflow_cli(
            [
                "-f",
                str(SAMPLE_WORKFLOW),
                "-b",
                live_rewardlab_server,
                "-a",
                account_id,
                "--json",
                "--headless",
            ]
        )
        assert exit_code == ExitCode.SUCCESS

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["workflow_id"] == "rewardlab_review_first_task"
        assert data["provider"] == "rewardlab"
        assert data["status"] == "SUCCESS"
        assert data["steps_executed"] == 4
        assert data["total_steps"] == 4
        assert len(data["step_results"]) == 4
        assert "execution_id" in data
        assert data["duration_seconds"] > 0
        assert data["requires_verification"] is False

        # Verify side effect on RewardLab
        assert rewardlab_store.users[account_id].points == 60

    finally:
        if env_backup is not None:
            os.environ["REWARDLAB_PASSWORD"] = env_backup
        else:
            os.environ.pop("REWARDLAB_PASSWORD", None)


# =============================================================================
# 7. OS Subprocess Integration Tests
# =============================================================================


def test_cli_subprocess_help():
    """Verify CLI executable runs as an independent OS subprocess via python -m app.cli."""
    backend_dir = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "--help"],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "run-workflow" in proc.stdout
    assert "--validate-only" in proc.stdout


def test_cli_subprocess_validate_only():
    """Verify subprocess validate-only returns exit code 0."""
    backend_dir = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "-f", str(SAMPLE_WORKFLOW), "--validate-only", "--json"],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["valid"] is True


def test_cli_subprocess_denied_provider(tmp_path):
    """Verify subprocess policy rejection exits with code 2 at the OS process level."""
    backend_dir = Path(__file__).resolve().parents[1]
    wf = tmp_path / "rewards.yaml"
    wf.write_text(
        """schema_version: 1
id: rewards_subproc
provider: microsoft_rewards
steps:
  - id: s1
    action: navigate
    path: /rewards
""",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "-f", str(wf)],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "Policy Denied:" in proc.stderr


# =============================================================================
# 8. Pre-M3 Session Security & Cross-Account Isolation Tests
# =============================================================================


def test_cli_cross_account_session_mixing_blocked(live_rewardlab_server, reset_rewardlab, capsys):
    """Providing a session belonging to account A for running account B is rejected."""
    # Obtain legitimate session token for test_01
    with httpx.Client(base_url=live_rewardlab_server) as client:
        resp = client.post(
            "/login",
            data={"username": "test_01", "password": "test_password_01"},
            follow_redirects=False,
        )
        test_01_session = resp.cookies.get("rewardlab_session")
        assert test_01_session is not None

    # Attempt to execute with test_01 session while claiming to be test_02
    exit_code = run_workflow_cli(
        [
            "-f",
            str(SAMPLE_WORKFLOW),
            "-b",
            live_rewardlab_server,
            "-a",
            "test_02",  # Claiming account test_02
            "-s",
            test_01_session,  # But using test_01 session token
            "--json",
        ]
    )
    assert exit_code == ExitCode.POLICY_DENIED

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"] == "SESSION_ACCOUNT_MISMATCH"
    assert "Cross-account session mixing is prohibited" in data["message"]


def test_cli_session_id_cli_warning(live_rewardlab_server, reset_rewardlab, caplog):
    """Passing --session-id triggers a security warning recommending environment variables."""
    import logging

    with caplog.at_level(logging.WARNING, logger="adbap.cli"):
        exit_code = run_workflow_cli(
            [
                "-f",
                str(SAMPLE_WORKFLOW),
                "-b",
                live_rewardlab_server,
                "-a",
                "test_01",
                "-s",
                "dummy_token_xyz",
            ]
        )
    # The session is invalid, so exit code is POLICY_DENIED
    assert exit_code == ExitCode.POLICY_DENIED
    assert any(
        "Passing session token via --session-id exposes credentials" in r.message
        for r in caplog.records
    )
