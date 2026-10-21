"""Execution engine and Playwright adapters (Milestone M2.3)."""

from app.execution.browser_manager import BrowserManager
from app.execution.diagnostics import DiagnosticManager

__all__ = ["BrowserManager", "DiagnosticManager"]
