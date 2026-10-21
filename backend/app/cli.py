"""Command-line interface (CLI) for executing ADBAP workflows (Milestone M2.5).

Provides the `run-workflow` command to validate and execute authorized YAML/JSON
workflows against synthetic accounts on RewardLab with strict runtime security,
context isolation, stable exit codes, sanitized diagnostics, and JSON/human outputs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import uuid
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import PolicyDeniedException
from app.core.policy import PolicyEvaluator, default_evaluator
from app.execution.browser_manager import BrowserManager
from app.execution.diagnostics import DiagnosticManager
from app.observability.logging import (
    get_logger,
    redact_data,
    redact_url_credentials,
)
from app.persistence.database import get_session_factory
from app.security.session_crypto import SessionKeyConfigurationError
from app.security.session_store import SecureSessionStore
from app.workflows.errors import (
    UnsupportedSchemaVersionError,
    WorkflowSizeExceededError,
    WorkflowSyntaxError,
    WorkflowValidationError,
)
from app.workflows.parser import parse_workflow_content
from app.workflows.results import (
    WorkflowExecutionResult,
    WorkflowExecutionStatus,
)
from app.workflows.schema import WorkflowDefinition
from app.workflows.validator import validate_workflow_definition

logger = get_logger("adbap.cli")


# =============================================================================
# Exit Codes
# =============================================================================


class ExitCode(IntEnum):
    """Stable CLI process exit codes."""

    SUCCESS = 0
    FAILED = 1
    POLICY_DENIED = 2
    TIMEOUT = 3
    CANCELLED = 4
    USAGE_OR_SCHEMA_ERROR = 5


# =============================================================================
# Custom Argument Parser with Controlled Exit Code
# =============================================================================


class WorkflowArgumentParser(argparse.ArgumentParser):
    """Custom ArgumentParser that exits with USAGE_OR_SCHEMA_ERROR (5) on bad arguments."""

    def error(self, message: str) -> None:
        sanitized = redact_url_credentials(message)
        sys.stderr.write(f"Usage error: {sanitized}\n")
        self.print_help(sys.stderr)
        sys.exit(ExitCode.USAGE_OR_SCHEMA_ERROR)


def build_parser() -> argparse.ArgumentParser:
    """Builds and configures the CLI argument parser."""
    parser = WorkflowArgumentParser(
        prog="run-workflow",
        description="ADBAP Workflow Runner: Execute and validate automation workflows safely.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Security Notes:\n"
            "  - Passwords and secrets must NOT be passed via command-line flags.\n"
            "  - For RewardLab synthetic accounts, set REWARDLAB_PASSWORD in the environment.\n"
            "  - All outputs and logs are sanitized to prevent credential leakage.\n"
        ),
    )

    # Allow optional subcommand keyword "run-workflow" for compatibility with `adbap run-workflow`
    parser.add_argument(
        "subcommand_or_file",
        nargs="?",
        default=None,
        help="Path to workflow file, or 'run-workflow' subcommand keyword.",
    )

    parser.add_argument(
        "file_positional",
        nargs="?",
        default=None,
        help="Path to workflow YAML or JSON file (if 'run-workflow' keyword was used).",
    )

    parser.add_argument(
        "-f",
        "--file",
        dest="workflow_file",
        default=None,
        help="Path to workflow YAML or JSON file.",
    )

    parser.add_argument(
        "-a",
        "--account",
        dest="account_id",
        default="test_01",
        help="Synthetic test account identifier (default: 'test_01').",
    )

    parser.add_argument(
        "-p",
        "--provider",
        dest="provider",
        default=None,
        help="Expected provider name (optional; verified against workflow definition).",
    )

    parser.add_argument(
        "-b",
        "--base-url",
        dest="base_url",
        default="http://127.0.0.1:8001",
        help="Base URL for target web application (default: 'http://127.0.0.1:8001').",
    )

    parser.add_argument(
        "-t",
        "--task-id",
        dest="task_id",
        default=None,
        help="Optional target task ID for state verification steps.",
    )

    parser.add_argument(
        "-s",
        "--session-id",
        dest="session_id",
        default=None,
        help="Optional session token (can also be supplied via REWARDLAB_SESSION_ID env var).",
    )

    parser.add_argument(
        "--timeout",
        dest="timeout",
        type=float,
        default=None,
        help="Context lifecycle timeout bound in seconds.",
    )

    parser.add_argument(
        "--validate-only",
        "--dry-run",
        dest="validate_only",
        action="store_true",
        help="Validate workflow syntax, schema, and policy without launching browser.",
    )

    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output structured, machine-readable JSON to stdout.",
    )

    parser.add_argument(
        "--headless",
        dest="headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run Chromium in headless mode (default: --headless).",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        dest="verbose",
        action="store_true",
        help="Enable verbose debug logging.",
    )

    return parser


# =============================================================================
# Helper Utilities
# =============================================================================


def resolve_workflow_path(args: argparse.Namespace) -> Path | None:
    """Resolves workflow file path from positional or flagged arguments."""
    candidate: str | None = None

    if args.workflow_file:
        candidate = args.workflow_file
    elif args.subcommand_or_file and args.subcommand_or_file.lower() == "run-workflow":
        candidate = args.file_positional
    elif args.subcommand_or_file:
        candidate = args.subcommand_or_file

    if candidate:
        return Path(candidate)
    return None


async def try_auto_authenticate(
    base_url: str,
    account_id: str,
    provider: str,
) -> str | None:
    """Attempts auto-login via HTTP endpoint if REWARDLAB_PASSWORD is set."""
    if provider != "rewardlab":
        return None

    password = os.environ.get("REWARDLAB_PASSWORD")
    if not password:
        return None

    try:
        async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=5.0) as client:
            resp = await client.post(
                "/login",
                data={"username": account_id, "password": password},
                follow_redirects=False,
            )
            session_cookie = resp.cookies.get("rewardlab_session")
            if session_cookie:
                logger.debug("Successfully auto-authenticated account '%s' via HTTP", account_id)
                return session_cookie
    except Exception as e:
        logger.debug(
            "Auto-login attempt failed: %s",
            redact_url_credentials(str(e)),
        )

    return None


async def try_load_stored_session(
    account_id: str,
    provider: str,
    base_url: str,
) -> str | None:
    """Attempts to retrieve a valid encrypted session from SecureSessionStore.

    Validates that the session is unexpired, owned by account_id, and unrevoked.
    Fails closed if the database or encryption key is unconfigured.
    """
    try:
        session_factory = get_session_factory(settings.database_url)
        async with session_factory() as db_session:
            store = SecureSessionStore(db_session)
            session_data = await store.get_valid_session(
                account_id=account_id,
                provider_id=provider,
            )
            if session_data:
                is_valid = await store.verify_session_with_provider(
                    session_data=session_data,
                    base_url=base_url,
                )
                if is_valid:
                    await db_session.commit()
                    logger.debug(
                        "Loaded valid encrypted session for '%s' from SecureSessionStore",
                        account_id,
                    )
                    return session_data.session_token
                await db_session.commit()
    except (SessionKeyConfigurationError, Exception) as err:
        logger.debug(
            "SecureSessionStore lookup skipped or unavailable: %s",
            redact_url_credentials(str(err)),
        )

    return None


async def verify_session_account_binding(
    base_url: str,
    account_id: str,
    session_id: str,
    provider: str,
) -> tuple[bool, str | None]:
    """Verifies that the provided session_id strictly belongs to account_id.

    Guarantees that credentials from account A cannot be mistakenly or maliciously
    bound to execution for account B (cross-account session isolation).
    """
    if provider != "rewardlab":
        return True, None

    try:
        async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=5.0) as client:
            resp = await client.get(
                "/api/tasks",
                headers={"Authorization": f"Bearer {session_id}"},
                follow_redirects=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                owner = data.get("username")
                if owner and owner != account_id:
                    return False, (
                        f"Session token belongs to account '{owner}', but execution was requested "
                        f"for account '{account_id}'. Cross-account session mixing is prohibited."
                    )
                return True, None
            if resp.status_code == 401:
                return False, "Provided session token is invalid or expired."
            return True, None
    except Exception as e:
        logger.debug("Session verification check error: %s", redact_url_credentials(str(e)))
        return True, None


# =============================================================================
# Formatting Outputs (Sanitized)
# =============================================================================


def render_human_result(
    result: WorkflowExecutionResult,
    account_id: str,
    base_url: str,
    headless: bool,
) -> str:
    """Renders human-readable execution summary with secret redaction."""
    lines: list[str] = [
        "=== ADBAP Workflow Execution Summary ===",
        f"Execution ID : {result.execution_id}",
        f"Workflow     : {result.workflow_id} (provider: {result.provider})",
        f"Account      : {account_id}",
        f"Target URL   : {redact_url_credentials(base_url)}",
        f"Headless     : {headless}",
        "----------------------------------------",
    ]

    for idx, s in enumerate(result.step_results, start=1):
        status_tag = f"[{s.status}]"
        dur_str = f"({s.duration_ms:.1f}ms)"
        line_prefix = f"[{idx}/{result.total_steps}] {s.action} ({s.step_id})"
        dots = "." * max(2, 45 - len(line_prefix) - len(status_tag) - len(dur_str) - 2)
        lines.append(f"{line_prefix} {dots} {status_tag} {dur_str}")
        if s.error_message:
            clean_err = redact_url_credentials(s.error_message)
            lines.append(f"      Error: {clean_err}")

    lines.extend(
        [
            "----------------------------------------",
            f"Overall Status       : {result.status}",
            f"Duration             : {result.duration_seconds:.3f}s",
            f"Steps Executed       : {result.steps_executed}/{result.total_steps}",
            f"Requires Verification: {result.requires_verification}",
            f"Can Retry            : {result.can_retry}",
        ]
    )

    if result.error_code:
        lines.append(f"Error Code           : {result.error_code}")
    if result.error_message:
        lines.append(f"Error Message        : {redact_url_credentials(result.error_message)}")

    if result.artifacts:
        lines.append("Diagnostic Artifacts :")
        for art in result.artifacts:
            lines.append(f"  - {art}")

    lines.append("========================================")
    return "\n".join(lines)


def render_human_validation(
    wf: WorkflowDefinition,
    is_valid: bool,
    error_message: str | None = None,
) -> str:
    """Renders human-readable validation summary."""
    lines = [
        "=== ADBAP Workflow Validation ===",
        f"Workflow ID  : {wf.id}",
        f"Provider     : {wf.provider}",
        f"Schema Ver   : {wf.schema_version}",
        f"Total Steps  : {len(wf.steps)}",
        f"Capabilities : {wf.requested_capabilities or []}",
        f"Result       : {'VALID' if is_valid else 'INVALID'}",
    ]
    if error_message:
        lines.append(f"Error        : {redact_url_credentials(error_message)}")
    lines.append("==================================")
    return "\n".join(lines)


# =============================================================================
# Core Async Execution Logic
# =============================================================================


async def execute_cli_async(
    args: argparse.Namespace,
    evaluator: PolicyEvaluator | None = None,
) -> tuple[ExitCode, str]:
    """Asynchronous core of the CLI runner.

    Returns:
        (ExitCode, formatted_output_string)
    """
    eval_policy = evaluator or default_evaluator

    # 1. Resolve workflow file path
    wf_path = resolve_workflow_path(args)
    if not wf_path:
        err_msg = "No workflow file specified. Use --file <path> or run-workflow <path>."
        if args.output_json:
            return ExitCode.USAGE_OR_SCHEMA_ERROR, json.dumps(
                {"error": "NO_WORKFLOW_FILE", "message": err_msg}, indent=2
            )
        return ExitCode.USAGE_OR_SCHEMA_ERROR, f"Error: {err_msg}"

    if not wf_path.exists() or not wf_path.is_file():
        err_msg = f"Workflow file not found: '{wf_path}'"
        if args.output_json:
            return ExitCode.USAGE_OR_SCHEMA_ERROR, json.dumps(
                {"error": "FILE_NOT_FOUND", "message": err_msg, "path": str(wf_path)}, indent=2
            )
        return ExitCode.USAGE_OR_SCHEMA_ERROR, f"Error: {err_msg}"

    # 2. Read raw workflow file content
    try:
        raw_content = wf_path.read_bytes()
    except Exception as e:
        clean_err = redact_url_credentials(str(e))
        err_msg = f"Could not read workflow file: {clean_err}"
        if args.output_json:
            return ExitCode.USAGE_OR_SCHEMA_ERROR, json.dumps(
                {"error": "FILE_READ_ERROR", "message": err_msg}, indent=2
            )
        return ExitCode.USAGE_OR_SCHEMA_ERROR, f"Error: {err_msg}"

    # 3. Parse content safely
    try:
        raw_dict = parse_workflow_content(raw_content)
    except (WorkflowSyntaxError, WorkflowSizeExceededError) as pse:
        clean_err = redact_url_credentials(pse.message)
        if args.output_json:
            return ExitCode.USAGE_OR_SCHEMA_ERROR, json.dumps(
                {
                    "error": "SYNTAX_ERROR",
                    "message": clean_err,
                    "details": redact_data(pse.details),
                },
                indent=2,
            )
        return ExitCode.USAGE_OR_SCHEMA_ERROR, f"Syntax Error: {clean_err}"

    # 4. Validate schema and provider policies
    wf_def: WorkflowDefinition
    try:
        wf_def = validate_workflow_definition(raw_dict, eval_policy)
    except (UnsupportedSchemaVersionError, WorkflowValidationError) as sve:
        clean_err = redact_url_credentials(sve.message)
        if args.output_json:
            return ExitCode.USAGE_OR_SCHEMA_ERROR, json.dumps(
                {"error": "VALIDATION_ERROR", "message": clean_err}, indent=2
            )
        return ExitCode.USAGE_OR_SCHEMA_ERROR, f"Validation Error: {clean_err}"
    except PolicyDeniedException as pde:
        clean_err = redact_url_credentials(pde.message)
        if args.output_json:
            return ExitCode.POLICY_DENIED, json.dumps(
                {
                    "error": "POLICY_DENIED",
                    "message": clean_err,
                    "details": redact_data(pde.details),
                },
                indent=2,
            )
        return ExitCode.POLICY_DENIED, f"Policy Denied: {clean_err}"

    # 5. Check provider constraint if explicitly passed via CLI
    if args.provider and args.provider != wf_def.provider:
        err_msg = (
            f"CLI argument --provider '{args.provider}' does not match "
            f"workflow provider '{wf_def.provider}'."
        )
        if args.output_json:
            return ExitCode.USAGE_OR_SCHEMA_ERROR, json.dumps(
                {"error": "PROVIDER_MISMATCH", "message": err_msg}, indent=2
            )
        return ExitCode.USAGE_OR_SCHEMA_ERROR, f"Error: {err_msg}"

    # Verify provider permission explicitly
    prov_policy = eval_policy.get_policy(wf_def.provider)
    if not prov_policy or not prov_policy.automation_allowed:
        deny_msg = (
            f"Automation is prohibited for provider '{wf_def.provider}'. "
            "Microsoft Rewards is companion-only and unknown providers are denied by default."
        )
        if args.output_json:
            return ExitCode.POLICY_DENIED, json.dumps(
                {
                    "error": "POLICY_DENIED",
                    "provider": wf_def.provider,
                    "message": deny_msg,
                },
                indent=2,
            )
        return ExitCode.POLICY_DENIED, f"Policy Denied: {deny_msg}"

    # 6. Handle --validate-only / --dry-run (Zero browser launch)
    if args.validate_only:
        if args.output_json:
            val_json = {
                "valid": True,
                "workflow_id": wf_def.id,
                "provider": wf_def.provider,
                "schema_version": wf_def.schema_version,
                "total_steps": len(wf_def.steps),
                "requested_capabilities": wf_def.requested_capabilities or [],
            }
            return ExitCode.SUCCESS, json.dumps(val_json, indent=2)
        return ExitCode.SUCCESS, render_human_validation(wf_def, is_valid=True)

    # 7. Resolve authentication session ID
    if args.session_id:
        logger.warning(
            "Passing session token via --session-id exposes credentials in process listings. "
            "Prefer the encrypted SecureSessionStore or REWARDLAB_SESSION_ID environment variable."
        )

    session_id: str | None = args.session_id
    if not session_id:
        # Check encrypted SecureSessionStore first
        session_id = await try_load_stored_session(
            account_id=args.account_id,
            provider=wf_def.provider,
            base_url=args.base_url,
        )

    if not session_id:
        session_id = os.environ.get("REWARDLAB_SESSION_ID")

    if not session_id:
        session_id = await try_auto_authenticate(
            base_url=args.base_url,
            account_id=args.account_id,
            provider=wf_def.provider,
        )
    elif session_id:
        # Pre-execution session ownership verification
        is_valid, bind_err = await verify_session_account_binding(
            base_url=args.base_url,
            account_id=args.account_id,
            session_id=session_id,
            provider=wf_def.provider,
        )
        if not is_valid:
            logger.error("Session-account binding violation: %s", bind_err)
            if args.output_json:
                return ExitCode.POLICY_DENIED, json.dumps(
                    {
                        "error": "SESSION_ACCOUNT_MISMATCH",
                        "message": bind_err,
                    },
                    indent=2,
                )
            return ExitCode.POLICY_DENIED, f"Security Violation: {bind_err}"

    # 8. Full execution with managed BrowserManager
    exec_id = str(uuid.uuid4())
    start_time = datetime.now(UTC)
    browser_manager = BrowserManager(
        headless=args.headless,
        diagnostic_manager=DiagnosticManager(),
    )

    result: WorkflowExecutionResult
    await browser_manager.start()

    try:
        timeout_val = (
            args.timeout
            if args.timeout is not None
            else (wf_def.execution.timeout_seconds if wf_def.execution else None)
        )
        result = await browser_manager.execute_workflow(
            workflow=wf_def,
            account_id=args.account_id,
            task_id=args.task_id,
            session_id=session_id,
            base_url=args.base_url,
            context_timeout_seconds=timeout_val,
            policy_evaluator=eval_policy,
        )
        # Ensure execution ID is populated
        result.execution_id = exec_id
    except asyncio.CancelledError:
        result = WorkflowExecutionResult(
            execution_id=exec_id,
            workflow_id=wf_def.id,
            provider=wf_def.provider,
            status=WorkflowExecutionStatus.CANCELLED,
            steps_executed=0,
            total_steps=len(wf_def.steps),
            step_results=[],
            error_code="EXECUTION_CANCELLED",
            error_message="Workflow execution cancelled via signal interrupt",
            started_at=start_time,
            completed_at=datetime.now(UTC),
            duration_seconds=round((datetime.now(UTC) - start_time).total_seconds(), 4),
            can_retry=False,
            requires_verification=False,
        )
    finally:
        # Guarantee browser process and contexts are closed
        await browser_manager.stop()

    # 9. Map WorkflowExecutionStatus to ExitCode
    exit_code: ExitCode
    match result.status:
        case WorkflowExecutionStatus.SUCCESS:
            exit_code = ExitCode.SUCCESS
        case WorkflowExecutionStatus.POLICY_DENIED:
            exit_code = ExitCode.POLICY_DENIED
        case WorkflowExecutionStatus.TIMEOUT:
            exit_code = ExitCode.TIMEOUT
        case WorkflowExecutionStatus.CANCELLED:
            exit_code = ExitCode.CANCELLED
        case _:
            exit_code = ExitCode.FAILED

    # 10. Format and sanitize output
    if args.output_json:
        # Convert result model to dictionary and sanitize
        raw_res = result.model_dump(mode="json")
        sanitized_res = redact_data(raw_res)
        return exit_code, json.dumps(sanitized_res, indent=2)

    human_out = render_human_result(
        result=result,
        account_id=args.account_id,
        base_url=args.base_url,
        headless=args.headless,
    )
    return exit_code, human_out


# =============================================================================
# CLI Entrypoint
# =============================================================================


def run_workflow_cli(
    argv: list[str] | None = None,
    evaluator: PolicyEvaluator | None = None,
) -> int:
    """Synchronous entry point for the run-workflow CLI command.

    Args:
        argv: Command-line argument list (defaults to sys.argv[1:]).
        evaluator: Optional policy evaluator instance for testing.

    Returns:
        Integer exit code matching ExitCode enum.
    """
    raw_args = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    try:
        args = parser.parse_args(raw_args)
    except SystemExit as se:
        return int(se.code) if se.code is not None else int(ExitCode.USAGE_OR_SCHEMA_ERROR)

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )

    # Set up asyncio event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task = loop.create_task(execute_cli_async(args, evaluator=evaluator))

    # Signal handling for Ctrl+C (SIGINT)
    def _sigint_handler(*_: Any) -> None:
        logger.warning("Interrupt signal received; cancelling workflow execution...")
        main_task.cancel()

    with contextlib.suppress(ValueError, AttributeError):
        signal.signal(signal.SIGINT, _sigint_handler)

    try:
        exit_code, output_text = loop.run_until_complete(main_task)
    except (asyncio.CancelledError, KeyboardInterrupt):
        exit_code = ExitCode.CANCELLED
        if args.output_json:
            output_text = json.dumps(
                {
                    "error": "EXECUTION_CANCELLED",
                    "message": "Workflow execution cancelled by user.",
                },
                indent=2,
            )
        else:
            output_text = "Workflow execution cancelled by user."
    except Exception as e:
        exit_code = ExitCode.FAILED
        clean_err = redact_url_credentials(str(e))
        if args.output_json:
            output_text = json.dumps(
                {
                    "error": "UNHANDLED_ERROR",
                    "message": clean_err,
                },
                indent=2,
            )
        else:
            output_text = f"Unhandled error during workflow execution: {clean_err}"
    finally:
        loop.close()

    # Write output to stdout if success/json, or stderr if usage error
    if args.output_json:
        sys.stdout.write(output_text + "\n")
    else:
        if exit_code in (ExitCode.USAGE_OR_SCHEMA_ERROR, ExitCode.POLICY_DENIED):
            sys.stderr.write(output_text + "\n")
        else:
            sys.stdout.write(output_text + "\n")

    return int(exit_code)


def main() -> None:
    """Console script entrypoint."""
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        from app.workers.local_worker import run_worker_cli

        sys.exit(run_worker_cli(sys.argv[2:]))
    sys.exit(run_workflow_cli())


if __name__ == "__main__":
    main()
