"""Diagnostic artifacts and safe trace-on-failure manager.

Ensures that diagnostic artifacts are generated strictly on execution failure,
sanitizes all credentials and session tokens, prevents raw traces from leaking
passwords, and enforces retention policies to avoid disk bloat.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from app.observability.logging import (
    SENSITIVE_KEYS,
    get_logger,
    redact_data,
    redact_url_credentials,
)
from app.workflows.results import WorkflowExecutionResult, WorkflowExecutionStatus
from app.workflows.schema import FillStep, WorkflowDefinition

logger = get_logger("adbap.execution.diagnostics")

DEFAULT_DIAGNOSTICS_DIR = Path("var/diagnostics")
DEFAULT_MAX_ARTIFACTS = 50


class DiagnosticManager:
    """Manages creation, redaction, and retention of failure diagnostic artifacts."""

    def __init__(
        self,
        artifacts_dir: Path | str = DEFAULT_DIAGNOSTICS_DIR,
        max_artifacts: int = DEFAULT_MAX_ARTIFACTS,
        capture_screenshot: bool = True,
        enable_playwright_trace: bool = False,
    ) -> None:
        self.artifacts_dir = Path(artifacts_dir)
        self.max_artifacts = max_artifacts
        self.capture_screenshot = capture_screenshot
        self.enable_playwright_trace = enable_playwright_trace
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def workflow_contains_sensitive_data(self, workflow: WorkflowDefinition) -> bool:
        """Determines whether a workflow contains sensitive inputs (e.g. passwords)."""
        for step in workflow.steps:
            if isinstance(step, FillStep):
                # Check field name, target, or value for sensitive identifiers
                target_str = str(step.target).lower()
                val_str = str(step.value).lower()
                if any(k in target_str or k in val_str for k in SENSITIVE_KEYS):
                    return True
        return False

    async def capture_failure_diagnostic(
        self,
        workflow: WorkflowDefinition,
        result: WorkflowExecutionResult,
        page: Page | None = None,
        account_id: str = "test_01",
        task_id: str | None = None,
    ) -> list[str]:
        """Captures structured diagnostic artifacts for a failed workflow execution.

        Returns a list of created artifact file paths.
        Only called when result is NOT successful.
        """
        if result.status == WorkflowExecutionStatus.SUCCESS:
            return []

        self._ensure_dir()
        execution_id = str(uuid.uuid4())[:8]
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        prefix = f"diag_{timestamp}_{workflow.id}_{execution_id}"
        created_artifacts: list[str] = []

        # 1. Identify failing step details
        failed_step_info: dict[str, Any] | None = None
        completed_steps_summary: list[dict[str, Any]] = []

        for s in result.step_results:
            step_summary = {
                "step_id": s.step_id,
                "action": s.action,
                "status": str(s.status),
                "duration_ms": s.duration_ms,
            }
            if s.error_code:
                step_summary["error_code"] = s.error_code
                step_summary["error_message"] = redact_url_credentials(s.error_message or "")

            if s.status in ("FAILED", "TIMEOUT"):
                failed_step_info = step_summary
            else:
                completed_steps_summary.append(step_summary)

        # 2. Extract page metadata safely if page is still open
        current_url: str | None = None
        page_title: str | None = None
        screenshot_path: Path | None = None
        screenshot_suppressed_reason: str | None = None

        is_sensitive = self.workflow_contains_sensitive_data(workflow)

        if page is not None and not page.is_closed():
            try:
                raw_url = page.url
                current_url = redact_url_credentials(raw_url)
                page_title = await page.title()
            except Exception as e:
                logger.debug("Could not inspect page url/title for diagnostic: %s", e)

            # Capture screenshot if enabled and NOT sensitive
            if self.capture_screenshot:
                if is_sensitive:
                    screenshot_suppressed_reason = (
                        "Screenshot suppressed: workflow inputs contain sensitive credentials "
                        "(password/token). Fail-closed policy active."
                    )
                    logger.warning(screenshot_suppressed_reason)
                else:
                    try:
                        s_file = self.artifacts_dir / f"{prefix}_screenshot.png"
                        await page.screenshot(path=str(s_file), full_page=False)
                        screenshot_path = s_file
                        created_artifacts.append(str(s_file))
                        logger.info("Captured failure screenshot: %s", s_file.name)
                    except Exception as e:
                        logger.warning(
                            "Failed capturing failure screenshot: %s",
                            redact_url_credentials(str(e)),
                        )
            else:
                screenshot_suppressed_reason = "Screenshot capture disabled by configuration."

        # 3. Playwright trace policy: Fail-closed check
        trace_suppressed_reason: str | None = None
        if self.enable_playwright_trace:
            if is_sensitive:
                trace_suppressed_reason = (
                    "Native Playwright trace suppressed: workflow inputs contain "
                    "sensitive credentials (password/token). Fail-closed policy active."
                )
                logger.warning(trace_suppressed_reason)
            else:
                trace_suppressed_reason = "Playwright trace capture not active for this context."
        else:
            trace_suppressed_reason = "Native trace disabled by default (least-privilege policy)."

        # 4. Build structured, strictly redacted diagnostic record
        diagnostic_record: dict[str, Any] = {
            "execution_id": execution_id,
            "workflow_id": workflow.id,
            "workflow_schema_version": workflow.schema_version,
            "provider": workflow.provider,
            "account_id": account_id,
            "task_id": task_id,
            "status": str(result.status),
            "error_code": result.error_code,
            "error_message": redact_url_credentials(result.error_message or ""),
            "duration_seconds": result.duration_seconds,
            "requires_verification": result.requires_verification,
            "can_retry": result.can_retry,
            "started_at": result.started_at.isoformat(),
            "completed_at": result.completed_at.isoformat(),
            "current_url": current_url,
            "page_title": page_title,
            "failed_step": failed_step_info,
            "completed_steps": completed_steps_summary,
            "trace_policy": {
                "native_trace_enabled": self.enable_playwright_trace,
                "screenshot_captured": screenshot_path is not None,
                "screenshot_suppressed_reason": screenshot_suppressed_reason,
                "trace_suppressed_reason": trace_suppressed_reason,
            },
            "outputs": redact_data(result.outputs),
        }

        # Write sanitized diagnostic JSON
        diag_file = self.artifacts_dir / f"{prefix}_diagnostic.json"
        try:
            with open(diag_file, "w", encoding="utf-8") as f:
                json.dump(diagnostic_record, f, indent=2, ensure_ascii=False)
            created_artifacts.append(str(diag_file))
            logger.info("Saved failure diagnostic record: %s", diag_file.name)
        except Exception as e:
            logger.error("Failed writing diagnostic record: %s", redact_url_credentials(str(e)))

        # 5. Enforce artifact retention policy
        self.prune_artifacts()

        return created_artifacts

    def prune_artifacts(self) -> int:
        """Prunes oldest diagnostic files if artifact count exceeds max_artifacts.

        Returns number of deleted files.
        """
        if not self.artifacts_dir.exists():
            return 0

        files = [
            f
            for f in self.artifacts_dir.iterdir()
            if f.is_file() and (f.suffix in (".json", ".png", ".zip"))
        ]

        if len(files) <= self.max_artifacts:
            return 0

        # Sort files by modification time (oldest first)
        files.sort(key=lambda p: p.stat().st_mtime)
        overflow_count = len(files) - self.max_artifacts
        to_remove = files[:overflow_count]

        removed = 0
        for f in to_remove:
            try:
                f.unlink(missing_ok=True)
                removed += 1
            except Exception as e:
                logger.warning("Could not prune artifact %s: %s", f.name, e)

        if removed > 0:
            logger.info(
                "Pruned %d old diagnostic artifacts (retention limit: %d)",
                removed,
                self.max_artifacts,
            )

        return removed

    def cleanup_all_artifacts(self) -> int:
        """Cleans up all artifacts in the directory (useful for test resets)."""
        if not self.artifacts_dir.exists():
            return 0

        count = 0
        for item in self.artifacts_dir.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                    count += 1
                elif item.is_dir():
                    shutil.rmtree(item)
                    count += 1
            except Exception as e:
                logger.warning("Failed deleting %s: %s", item, e)
        return count
