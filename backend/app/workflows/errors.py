from typing import Any


class WorkflowError(Exception):
    """Base exception for all workflow DSL errors."""

    def __init__(
        self,
        message: str,
        code: str = "WORKFLOW_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}


class WorkflowSyntaxError(WorkflowError):
    """Raised when YAML or JSON syntax is invalid, unsafe, or contains duplicate keys."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message=message, code="WORKFLOW_SYNTAX_ERROR", details=details)


class WorkflowSizeExceededError(WorkflowError):
    """Raised when workflow content exceeds maximum allowed size limits."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message=message, code="WORKFLOW_SIZE_EXCEEDED", details=details)


class WorkflowValidationError(WorkflowError):
    """Raised when workflow fails schema, locator, step ID, or boundary validation."""

    def __init__(
        self,
        message: str,
        code: str = "WORKFLOW_VALIDATION_FAILED",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, details=details)


class UnsupportedSchemaVersionError(WorkflowValidationError):
    """Raised when a workflow specifies an unsupported schema_version."""

    def __init__(self, schema_version: Any, supported_versions: set[int]) -> None:
        super().__init__(
            message=(
                f"Unsupported workflow schema_version '{schema_version}'. "
                f"Supported versions: {sorted(supported_versions)}"
            ),
            code="UNSUPPORTED_SCHEMA_VERSION",
            details={
                "provided_version": schema_version,
                "supported_versions": sorted(supported_versions),
            },
        )


# =============================================================================
# Runtime Execution Errors (M2.2)
# =============================================================================


class WorkflowExecutionError(WorkflowError):
    """Base exception for runtime workflow execution failures."""

    def __init__(
        self,
        message: str,
        code: str = "ACTION_EXECUTION_FAILED",
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message=message, code=code, details=details)
        self.step_id = step_id


class ActionTimeoutError(WorkflowExecutionError):
    """Raised when an individual action exceeds its configured timeout bound."""

    def __init__(
        self,
        message: str,
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="STEP_TIMEOUT",
            step_id=step_id,
            details=details,
        )


class WorkflowTimeoutError(WorkflowExecutionError):
    """Raised when the entire workflow execution exceeds the overall timeout."""

    def __init__(
        self,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="WORKFLOW_TIMEOUT",
            step_id=None,
            details=details,
        )


class ElementNotFoundError(WorkflowExecutionError):
    """Raised when a target element cannot be found in the DOM."""

    def __init__(
        self,
        message: str,
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="ELEMENT_NOT_FOUND",
            step_id=step_id,
            details=details,
        )


class ElementNotVisibleError(WorkflowExecutionError):
    """Raised when a target element exists but is not visible within timeout."""

    def __init__(
        self,
        message: str,
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="ELEMENT_NOT_VISIBLE",
            step_id=step_id,
            details=details,
        )


class StepAssertionError(WorkflowExecutionError):
    """Raised when an assert_visible or assert step fails."""

    def __init__(
        self,
        message: str,
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="ASSERTION_FAILED",
            step_id=step_id,
            details=details,
        )


class StepVerificationError(WorkflowExecutionError):
    """Raised when verify_state fails to match expected server or DOM state."""

    def __init__(
        self,
        message: str,
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="STATE_VERIFICATION_FAILED",
            step_id=step_id,
            details=details,
        )


class UnsupportedActionError(WorkflowExecutionError):
    """Raised when the workflow requests an action not yet supported by the interpreter."""

    def __init__(
        self,
        action: str,
        step_id: str | None = None,
    ) -> None:
        super().__init__(
            message=(
                f"Action '{action}' is defined in schema but not yet supported by the "
                "Playwright action interpreter in Milestone M2.2"
            ),
            code="UNSUPPORTED_ACTION_TYPE",
            step_id=step_id,
            details={"action": action},
        )


class RuntimePolicyViolationError(WorkflowExecutionError):
    """Raised when a runtime action or redirect violates security policy."""

    def __init__(
        self,
        message: str,
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="POLICY_DENIED",
            step_id=step_id,
            details=details,
        )


# =============================================================================
# Lifecycle & Reliability Errors (M2.3)
# =============================================================================


class BrowserDisconnectedError(WorkflowExecutionError):
    """Raised when the underlying Chromium browser disconnects or closes unexpectedly."""

    def __init__(
        self,
        message: str = "Browser process disconnected unexpectedly",
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="BROWSER_DISCONNECTED",
            step_id=step_id,
            details=details,
        )


class BrowserCrashError(WorkflowExecutionError):
    """Raised when the Chromium browser process crashes during execution."""

    def __init__(
        self,
        message: str = "Browser process crashed during execution",
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="BROWSER_CRASHED",
            step_id=step_id,
            details=details,
        )


class ContextTimeoutError(WorkflowExecutionError):
    """Raised when browser context execution exceeds its lifecycle timeout."""

    def __init__(
        self,
        message: str = "Browser context lifecycle timed out",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="CONTEXT_TIMEOUT",
            step_id=None,
            details=details,
        )


class ExecutionCancelledError(WorkflowExecutionError):
    """Raised when workflow execution is cancelled via asyncio cancellation."""

    def __init__(
        self,
        message: str = "Workflow execution was cancelled",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            code="EXECUTION_CANCELLED",
            step_id=None,
            details=details,
        )
