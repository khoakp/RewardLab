"""Operator CLI for managing jobs, reviewing paused workflows, and inspecting DSLs.

Milestone Personal P2:
- adbap jobs list [--status STATUS] [--account ACCOUNT] [--limit N] [--json]
- adbap jobs show <job-id> [--json]
- adbap jobs review <job-id> --decision {cancel,reject} --reason "..." [--json]
- adbap inspect-workflow <path> [--json]

Zero blind retries, strict state machine transitions, leak-proof redaction.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from app.core.policy import default_evaluator
from app.observability.logging import (
    get_logger,
    redact_data,
    redact_free_text,
    redact_url_credentials,
)
from app.orchestration.job_service import JobService
from app.orchestration.state_machine import (
    IllegalStateTransitionError,
    TerminalStateModificationError,
)
from app.persistence.database import get_db_session
from app.persistence.models import JobStatus
from app.workflows.errors import (
    WorkflowSizeExceededError,
    WorkflowSyntaxError,
    WorkflowValidationError,
)
from app.workflows.parser import parse_workflow_content
from app.workflows.schema import (
    VerifyStateStep,
    WorkflowDefinition,
    is_step_mutating,
)

logger = get_logger("adbap.operator_cli")


def build_jobs_parser() -> argparse.ArgumentParser:
    """Builds the argument parser for `adbap jobs` commands."""
    parser = argparse.ArgumentParser(
        prog="adbap jobs",
        description="Safe Operator Console: Review paused jobs without duplicate side effects.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. jobs list
    list_p = subparsers.add_parser("list", help="List jobs with optional status filter.")
    list_p.add_argument(
        "--status",
        dest="status",
        default=None,
        help="Filter by status (e.g. PAUSED_FOR_REVIEW, QUEUED, SUCCEEDED, FAILED).",
    )
    list_p.add_argument(
        "--account",
        dest="account_id",
        default=None,
        help="Filter by account ID.",
    )
    list_p.add_argument(
        "--limit",
        dest="limit",
        type=int,
        default=50,
        help="Maximum jobs to return (default: 50).",
    )
    list_p.add_argument(
        "--offset",
        dest="offset",
        type=int,
        default=0,
        help="Pagination offset (default: 0).",
    )
    list_p.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output results as sanitized JSON.",
    )

    # 2. jobs show
    show_p = subparsers.add_parser("show", help="Show detailed execution information for a job.")
    show_p.add_argument("job_id", help="Target Job ID.")
    show_p.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output details as sanitized JSON.",
    )

    # 3. jobs review
    review_p = subparsers.add_parser(
        "review",
        help="Review a paused job and make a terminal decision (cancel/reject).",
    )
    review_p.add_argument("job_id", help="Target Job ID in PAUSED_FOR_REVIEW.")
    review_p.add_argument(
        "--decision",
        dest="decision",
        required=True,
        choices=["cancel", "reject"],
        help="Review decision: 'cancel' or 'reject'. Direct SUCCEEDED transition is forbidden.",
    )
    review_p.add_argument(
        "--reason",
        dest="reason",
        required=True,
        help="Operator explanation justifying the decision.",
    )
    review_p.add_argument(
        "--actor",
        dest="actor",
        default="operator",
        help="Reviewing actor identifier (default: 'operator').",
    )
    review_p.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output result as sanitized JSON.",
    )

    return parser


def build_inspect_parser() -> argparse.ArgumentParser:
    """Builds the argument parser for `adbap inspect-workflow`."""
    parser = argparse.ArgumentParser(
        prog="adbap inspect-workflow",
        description="Inspect workflow execution plan and causal verification (zero browser).",
    )
    parser.add_argument("workflow_file", help="Path to workflow YAML or JSON file.")
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output inspection plan as JSON.",
    )
    parser.add_argument(
        "--base-url",
        dest="base_url",
        default="http://127.0.0.1:8001",
        help="Base URL for target web application (default: 'http://127.0.0.1:8001').",
    )
    return parser


async def execute_jobs_async(args: argparse.Namespace) -> tuple[int, str]:
    """Executes the requested jobs subcommand asynchronously."""
    async with get_db_session() as session:
        job_service = JobService(session)

        if args.subcommand == "list":
            jobs = await job_service.list_jobs(
                status=args.status,
                account_id=args.account_id,
                limit=args.limit,
                offset=args.offset,
            )

            # Build sanitized output records
            records: list[dict[str, Any]] = []
            for j in jobs:
                records.append(
                    {
                        "job_id": j.id,
                        "account_id": j.account_id,
                        "workflow_version_id": j.workflow_version_id,
                        "status": j.status,
                        "error_code": j.error_code,
                        "attempt": j.attempt,
                        "max_attempts": j.max_attempts,
                        "is_paused_for_review": (j.status == JobStatus.PAUSED_FOR_REVIEW.value),
                        "created_at": j.created_at.isoformat() if j.created_at else None,
                        "updated_at": j.updated_at.isoformat() if j.updated_at else None,
                    }
                )

            records = redact_data(records)

            if args.output_json:
                return 0, json.dumps(records, indent=2)

            if not records:
                msg = "No jobs found"
                if args.status:
                    msg += f" with status '{args.status}'"
                if args.account_id:
                    msg += f" for account '{args.account_id}'"
                return 0, msg + "."

            # Format human table
            header = (
                f"{'JOB ID':<36} {'ACCOUNT':<10} {'STATUS':<18} "
                f"{'ATTEMPT':<8} {'PAUSED':<7} {'UPDATED AT'}"
            )
            lines = [
                header,
                "-" * 95,
            ]
            for r in records:
                paused_str = "YES" if r["is_paused_for_review"] else "NO"
                lines.append(
                    f"{r['job_id']:<36} {r['account_id']:<10} {r['status']:<18} "
                    f"{r['attempt']}/{r['max_attempts']:<6} {paused_str:<7} "
                    f"{r['updated_at'] or '-'}"
                )
            return 0, "\n".join(lines)

        elif args.subcommand == "show":
            detail = await job_service.get_job_detail(args.job_id)
            if not detail:
                err_msg = f"Job '{args.job_id}' not found."
                if args.output_json:
                    return 1, json.dumps({"error": "NOT_FOUND", "message": err_msg}, indent=2)
                return 1, f"Error: {err_msg}"

            if args.output_json:
                return 0, json.dumps(detail, indent=2)

            wf_ver_str = (
                f"{detail['workflow_version_id']} "
                f"({detail['workflow_key'] or '-'} v{detail['workflow_version'] or '-'})"
            )

            # Format human summary
            lines = [
                "=" * 80,
                f"JOB DETAILS: {detail['job_id']}",
                "=" * 80,
                f"Account ID:         {detail['account_id']}",
                f"Provider:           {detail['provider_id'] or '-'}",
                f"Workflow Version:   {wf_ver_str}",
                f"Status:             {detail['status']}",
                f"Paused for Review:  {'YES' if detail['is_paused_for_review'] else 'NO'}",
            ]
            if detail.get("pause_reason"):
                lines.append(f"Pause Reason:       {detail['pause_reason']}")
            if detail.get("error_code"):
                lines.append(f"Error Code:         {detail['error_code']}")
            if detail.get("error_message"):
                lines.append(f"Error Message:      {detail['error_message']}")

            lines.extend(
                [
                    f"Attempts:           {detail['attempt']} / {detail['max_attempts']}",
                    f"Created At:         {detail['created_at'] or '-'}",
                    f"Started At:         {detail['started_at'] or '-'}",
                    f"Updated At:         {detail['updated_at'] or '-'}",
                    f"Finished At:        {detail['finished_at'] or '-'}",
                    f"Result Ref:         {detail['result_ref'] or '-'}",
                ]
            )

            cv = detail.get("causal_verification") or {}
            lines.extend(
                [
                    "-" * 80,
                    "CAUSAL VERIFICATION EVIDENCE:",
                    f"  Outcome Type:         {cv.get('outcome_type', 'NOT_RECORDED')}",
                    f"  Verification Required:{cv.get('requires_verification', 'NOT_RECORDED')}",
                    f"  Verified Mutations:   {cv.get('verified_mutations', 'NOT_RECORDED')}",
                    f"  Unverified Mutations: {cv.get('unverified_mutations', 'NOT_RECORDED')}",
                    f"  Causal Mappings:      {cv.get('causal_mappings', 'NOT_RECORDED')}",
                    "-" * 80,
                    f"EXECUTED STEPS ({len(detail['steps'])}):",
                ]
            )
            if not detail["steps"]:
                lines.append("  (No step execution records recorded)")
            else:
                for idx, st in enumerate(detail["steps"], 1):
                    err_info = f" [{st['error_code']}]" if st.get("error_code") else ""
                    lines.append(f"  {idx}. {st['step_key']:<25} {st['status']:<10}{err_info}")
                    if st.get("error_message"):
                        lines.append(f"     Reason: {st['error_message']}")

            lines.extend(
                [
                    "-" * 80,
                    f"AUDIT TRAIL ({len(detail['audit_events'])}):",
                ]
            )
            if not detail["audit_events"]:
                lines.append("  (No audit events recorded)")
            else:
                for a in detail["audit_events"]:
                    lines.append(
                        f"  [{a['created_at']}] {a['actor']} -> {a['action']} ({a['outcome']})"
                    )

            lines.append("=" * 80)
            return 0, "\n".join(lines)

        elif args.subcommand == "review":
            try:
                updated_job = await job_service.review_job(
                    job_id=args.job_id,
                    decision=args.decision,
                    reason=args.reason,
                    actor=args.actor,
                )
                await session.commit()

                res_dict = redact_data(
                    {
                        "job_id": updated_job.id,
                        "account_id": updated_job.account_id,
                        "status": updated_job.status,
                        "decision": args.decision.lower(),
                        "reason": args.reason,
                        "actor": args.actor,
                        "updated_at": updated_job.updated_at.isoformat()
                        if updated_job.updated_at
                        else None,
                    }
                )

                if args.output_json:
                    return 0, json.dumps(res_dict, indent=2)

                return 0, (
                    f"Job '{updated_job.id}' successfully reviewed.\n"
                    f"Decision:     {args.decision.upper()}\n"
                    f"New Status:   {updated_job.status}\n"
                    f"Reason:       {res_dict['reason']}\n"
                    f"Actor:        {args.actor}"
                )

            except (IllegalStateTransitionError, TerminalStateModificationError, ValueError) as err:
                clean_err = redact_free_text(str(err))
                if args.output_json:
                    return 1, json.dumps(
                        {"error": "REVIEW_REJECTED", "message": clean_err}, indent=2
                    )
                return 1, f"Review Error: {clean_err}"

        return 1, "Unknown subcommand."


def run_jobs_cli(argv: list[str] | None = None) -> int:
    """Synchronous entry point for `adbap jobs` CLI commands."""
    raw_args = sys.argv[2:] if argv is None else argv
    parser = build_jobs_parser()
    try:
        args = parser.parse_args(raw_args)
    except SystemExit as se:
        return int(se.code) if se.code is not None else 1

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        exit_code, output_text = loop.run_until_complete(execute_jobs_async(args))
    except Exception as e:
        exit_code = 1
        clean_err = redact_url_credentials(str(e))
        if getattr(args, "output_json", False):
            output_text = json.dumps({"error": "EXECUTION_ERROR", "message": clean_err}, indent=2)
        else:
            output_text = f"Execution error: {clean_err}"
    finally:
        loop.close()

    if exit_code == 0 or getattr(args, "output_json", False):
        sys.stdout.write(output_text + "\n")
    else:
        sys.stderr.write(output_text + "\n")
    return exit_code


def inspect_workflow_plan(
    workflow_path: str | Path,
    base_url: str = "http://127.0.0.1:8001",
) -> tuple[int, dict[str, Any]]:
    """Inspects a workflow file without launching a browser.

    Evaluates:
    - File readability and syntax
    - Schema validation and verification mapping invariants
    - Provider policy and permissions
    - Causal mutation-to-verification mappings
    - Unverified side-effect mutations

    Returns (exit_code, inspection_dict).
    """
    path_obj = Path(workflow_path)
    if not path_obj.exists():
        return 1, {"error": "FILE_NOT_FOUND", "message": f"Workflow file not found: {path_obj}"}

    try:
        content = path_obj.read_text(encoding="utf-8")
        parsed_dict = parse_workflow_content(content)
        workflow_def = WorkflowDefinition.model_validate(parsed_dict)
    except (
        WorkflowSizeExceededError,
        WorkflowSyntaxError,
        WorkflowValidationError,
        ValueError,
    ) as err:
        return 5, {
            "error": "SCHEMA_VALIDATION_ERROR",
            "message": redact_url_credentials(str(err)),
        }
    except Exception as err:
        return 1, {
            "error": "WORKFLOW_LOAD_FAILED",
            "message": redact_url_credentials(str(err)),
        }

    # Evaluate Provider Policy statically (zero browser execution)
    evaluator = default_evaluator
    provider = workflow_def.provider
    provider_policy = evaluator.policies.get(provider)

    policy_checks: dict[str, Any] = {
        "provider": provider,
        "automation_allowed": False,
        "capabilities_granted": False,
        "allowlist_domains": [],
        "policy_reason": None,
    }

    if not provider_policy or not provider_policy.automation_allowed:
        policy_checks["policy_reason"] = (
            f"Automation is prohibited for provider '{provider}'. "
            "Microsoft Rewards is companion-only and unknown providers are denied by default."
        )
    else:
        policy_checks["automation_allowed"] = True
        policy_checks["allowlist_domains"] = sorted(provider_policy.domain_allowlist)
        if workflow_def.requested_capabilities and hasattr(evaluator, "evaluate_capabilities"):
            cap_dec = evaluator.evaluate_capabilities(
                provider, workflow_def.requested_capabilities, actor="inspector"
            )
            policy_checks["capabilities_granted"] = cap_dec.allowed
            policy_checks["policy_reason"] = cap_dec.reason
        else:
            policy_checks["capabilities_granted"] = True

    # Build Step Execution Plan, Policy Checks, and Causal Verification Analysis
    steps_plan: list[dict[str, Any]] = []
    mutating_steps: list[str] = []
    verified_steps: set[str] = set()
    action_violations: list[str] = []
    destination_violations: list[str] = []
    unresolved_destinations: list[str] = []

    for idx, step in enumerate(workflow_def.steps, 1):
        is_mut = is_step_mutating(step)
        if is_mut:
            mutating_steps.append(step.id)

        verifies = getattr(step, "verifies_step_ids", None)
        if isinstance(step, VerifyStateStep):
            if verifies:
                verified_steps.update(verifies)
            elif len(mutating_steps) == 1:
                # Legacy single unambiguous mapping
                verified_steps.add(mutating_steps[0])

        # 1. Action-level permission check
        action_allowed = True
        action_reason = "Action permitted by policy"
        if provider_policy:
            if step.action not in provider_policy.allowed_actions:
                action_allowed = False
                action_reason = (
                    f"Action '{step.action}' is not in allowed actions for provider '{provider}'"
                )
                action_violations.append(f"Step '{step.id}': {action_reason}")
        else:
            action_allowed = False
            action_reason = f"Provider '{provider}' not authorized"
            action_violations.append(f"Step '{step.id}': {action_reason}")

        # 2. Destination URL / path check
        dest_status = "N/A"
        dest_reason = None
        target_url = getattr(step, "url", None)
        target_path = getattr(step, "path", None)

        if target_url:
            raw_url = str(target_url).strip()
            # Detect unresolved dynamic template expressions e.g. {param} or <param>
            if re.search(r"\{[^{}]+\}|<[^<>]+>", raw_url):
                dest_status = "UNRESOLVED"
                dest_reason = (
                    "URL contains dynamic template parameters that require runtime resolution"
                )
                unresolved_destinations.append(f"Step '{step.id}': {dest_reason}")
            else:
                valid, url_reason, sanitized_url = evaluator.validate_static_url(
                    raw_url,
                    provider_policy.domain_allowlist if provider_policy else [],
                    allowed_ports=provider_policy.allowed_ports if provider_policy else [],
                )
                if not valid:
                    dest_status = "DENIED"
                    dest_reason = url_reason
                    destination_violations.append(f"Step '{step.id}': {url_reason}")
                else:
                    dest_status = "ALLOWED"
                    dest_reason = "Destination URL matches provider allowlist"

        elif target_path:
            path_str = str(target_path).strip()
            if path_str.startswith("//") or "://" in path_str:
                dest_status = "DENIED"
                dest_reason = "Path must be a relative path, not a scheme-relative or foreign URL"
                destination_violations.append(f"Step '{step.id}': {dest_reason}")
            elif ".." in path_str:
                dest_status = "DENIED"
                dest_reason = "Path traversal sequences '..' are prohibited"
                destination_violations.append(f"Step '{step.id}': {dest_reason}")
            elif re.search(r"\{[^{}]+\}|<[^<>]+>", path_str):
                dest_status = "UNRESOLVED"
                dest_reason = (
                    "Path contains dynamic template parameters that require runtime resolution"
                )
                unresolved_destinations.append(f"Step '{step.id}': {dest_reason}")
            elif path_str.startswith("/"):
                dest_status = "ALLOWED"
                dest_reason = "Valid relative application path"
            else:
                dest_status = "UNRESOLVED"
                dest_reason = "Path lacks leading slash; requires runtime resolution"
                unresolved_destinations.append(f"Step '{step.id}': {dest_reason}")

        step_info = {
            "index": idx,
            "id": step.id,
            "action": step.action,
            "target": str(getattr(step, "target", None)) if getattr(step, "target", None) else None,
            "is_mutating": is_mut,
            "verifies_step_ids": verifies,
            "action_allowed": action_allowed,
            "action_reason": action_reason,
            "destination_status": dest_status,
            "destination_reason": dest_reason,
        }
        steps_plan.append(step_info)

    unverified_mutations = [mid for mid in mutating_steps if mid not in verified_steps]

    # Compute overall Preflight Verdict
    if (
        not provider_policy
        or not provider_policy.automation_allowed
        or not policy_checks["capabilities_granted"]
    ):
        preflight_verdict = "DENIED"
        verdict_reason = policy_checks["policy_reason"]
    elif action_violations or destination_violations:
        preflight_verdict = "DENIED"
        reasons = action_violations + destination_violations
        verdict_reason = "; ".join(reasons)
    elif unresolved_destinations:
        preflight_verdict = "UNRESOLVED"
        verdict_reason = "; ".join(unresolved_destinations)
    else:
        preflight_verdict = "ALLOWED"
        verdict_reason = (
            "All static preflight checks passed. Note: runtime redirects, DNS resolution, "
            "and dynamic navigation are enforced at execution."
        )

    policy_checks["preflight_verdict"] = preflight_verdict
    policy_checks["verdict_reason"] = verdict_reason
    policy_checks["allowed_actions"] = (
        sorted(provider_policy.allowed_actions) if provider_policy else []
    )
    policy_checks["allowed_ports"] = provider_policy.allowed_ports if provider_policy else []
    policy_checks["action_violations"] = action_violations
    policy_checks["destination_violations"] = destination_violations
    policy_checks["unresolved_destinations"] = unresolved_destinations

    inspection = {
        "workflow_id": workflow_def.id,
        "schema_version": workflow_def.schema_version,
        "provider": workflow_def.provider,
        "description": workflow_def.description,
        "preflight_verdict": preflight_verdict,
        "execution_limits": {
            "timeout_seconds": workflow_def.execution.timeout_seconds,
            "step_timeout_seconds": workflow_def.execution.step_timeout_seconds,
            "max_retries": workflow_def.execution.max_retries,
        },
        "policy_evaluation": policy_checks,
        "total_steps": len(workflow_def.steps),
        "mutating_step_count": len(mutating_steps),
        "unverified_mutations": unverified_mutations,
        "has_unverified_mutations": len(unverified_mutations) > 0,
        "steps": steps_plan,
    }

    return 0, redact_data(inspection)


def run_inspect_cli(argv: list[str] | None = None) -> int:
    """Synchronous entry point for `adbap inspect-workflow`."""
    raw_args = sys.argv[2:] if argv is None else argv
    parser = build_inspect_parser()
    try:
        args = parser.parse_args(raw_args)
    except SystemExit as se:
        return int(se.code) if se.code is not None else 5

    exit_code, report = inspect_workflow_plan(args.workflow_file, base_url=args.base_url)

    if args.output_json:
        output_text = json.dumps(report, indent=2)
    else:
        if exit_code != 0:
            output_text = f"Inspection Error: {report.get('message', 'Failed to inspect workflow')}"
        else:
            pol = report["policy_evaluation"]
            lines = [
                "=" * 80,
                f"WORKFLOW INSPECTION: {report['workflow_id']} (Provider: {report['provider']})",
                "=" * 80,
                f"Schema Version:     {report['schema_version']}",
                f"Description:        {report.get('description') or '-'}",
                f"Preflight Verdict:  {pol['preflight_verdict']}",
                f"Verdict Note:       {pol['verdict_reason']}",
                f"Total Steps:        {report['total_steps']}",
                f"Mutating Steps:     {report['mutating_step_count']}",
                (
                    f"Unverified Mutations: {report['unverified_mutations']}"
                    if report["unverified_mutations"]
                    else "Unverified Mutations: None (All verified)"
                ),
                "-" * 80,
                "PROVIDER POLICY CHECK:",
                (
                    "  Automation Allowed:   YES"
                    if pol["automation_allowed"]
                    else "  Automation Allowed:   NO (PROHIBITED)"
                ),
                (
                    "  Capabilities Granted: YES"
                    if pol["capabilities_granted"]
                    else "  Capabilities Granted: NO"
                ),
                (
                    "  Allowed Domains:      "
                    f"{', '.join(pol['allowlist_domains'])}"
                    if pol["allowlist_domains"]
                    else "  Allowed Domains:      None"
                ),
            ]
            if pol.get("action_violations"):
                lines.append(f"  Action Violations:    {'; '.join(pol['action_violations'])}")
            if pol.get("destination_violations"):
                lines.append(f"  URL Violations:       {'; '.join(pol['destination_violations'])}")
            if pol.get("unresolved_destinations"):
                lines.append(
                    f"  Unresolved Targets:   {'; '.join(pol['unresolved_destinations'])}"
                )

            lines.extend(
                [
                    "-" * 80,
                    f"{'#':<3} {'STEP ID':<22} {'ACTION':<16} {'POLICY':<8} "
                    f"{'MUTATING':<9} {'VERIFIES':<16}",
                    "-" * 80,
                ]
            )
            for st in report["steps"]:
                mut_str = "YES" if st["is_mutating"] else "no"
                ver_str = ", ".join(st["verifies_step_ids"]) if st["verifies_step_ids"] else "-"
                pol_str = (
                    "ALLOW"
                    if st["action_allowed"] and st["destination_status"] in ("ALLOWED", "N/A")
                    else "UNRES"
                    if st["destination_status"] == "UNRESOLVED"
                    else "DENY"
                )
                lines.append(
                    f"{st['index']:<3} {st['id']:<22} {st['action']:<16} "
                    f"{pol_str:<8} {mut_str:<9} {ver_str:<16}"
                )
            lines.append("=" * 80)
            output_text = "\n".join(lines)

    if exit_code == 0 or args.output_json:
        sys.stdout.write(output_text + "\n")
    else:
        sys.stderr.write(output_text + "\n")

    return exit_code
