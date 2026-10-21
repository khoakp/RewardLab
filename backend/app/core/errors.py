from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

POLICY_DENIED = "POLICY_DENIED"
SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
INTERNAL_ERROR = "INTERNAL_ERROR"

# Runtime execution error codes (M2.2, M2.3)
STEP_TIMEOUT = "STEP_TIMEOUT"
WORKFLOW_TIMEOUT = "WORKFLOW_TIMEOUT"
ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
ELEMENT_NOT_VISIBLE = "ELEMENT_NOT_VISIBLE"
ASSERTION_FAILED = "ASSERTION_FAILED"
STATE_VERIFICATION_FAILED = "STATE_VERIFICATION_FAILED"
UNSUPPORTED_ACTION_TYPE = "UNSUPPORTED_ACTION_TYPE"
ACTION_EXECUTION_FAILED = "ACTION_EXECUTION_FAILED"
BROWSER_DISCONNECTED = "BROWSER_DISCONNECTED"
BROWSER_CRASHED = "BROWSER_CRASHED"
CONTEXT_TIMEOUT = "CONTEXT_TIMEOUT"
EXECUTION_CANCELLED = "EXECUTION_CANCELLED"

# Security & Permission error codes (M2.4)
CAPABILITY_NOT_GRANTED = "CAPABILITY_NOT_GRANTED"
UNSAFE_SCHEME = "UNSAFE_SCHEME"
SSRF_ATTEMPT_BLOCKED = "SSRF_ATTEMPT_BLOCKED"
POPUP_BLOCKED = "POPUP_BLOCKED"
REDIRECT_DENIED = "REDIRECT_DENIED"
ARBITRARY_CODE_EXECUTION_BLOCKED = "ARBITRARY_CODE_EXECUTION_BLOCKED"


class PolicyDeniedException(Exception):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


async def policy_denied_exception_handler(
    request: Request, exc: PolicyDeniedException
) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "code": POLICY_DENIED,
            "message": exc.message,
            "details": exc.details,
        },
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "code": SCHEMA_VALIDATION_FAILED,
            "message": "Request payload failed schema validation",
            "details": {"errors": exc.errors()},
        },
    )


async def workflow_validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", SCHEMA_VALIDATION_FAILED)
    if code == "WORKFLOW_VALIDATION_FAILED":
        code = SCHEMA_VALIDATION_FAILED
    details = getattr(exc, "details", {})
    if "errors" not in details:
        details = {"errors": [str(exc)], **details}
    return JSONResponse(
        status_code=422,
        content={
            "code": code,
            "message": str(getattr(exc, "message", exc)),
            "details": details,
        },
    )


async def workflow_syntax_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", "WORKFLOW_SYNTAX_ERROR")
    details = getattr(exc, "details", {})
    return JSONResponse(
        status_code=422,
        content={
            "code": code,
            "message": str(getattr(exc, "message", exc)),
            "details": details,
        },
    )


async def workflow_size_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", "WORKFLOW_SIZE_EXCEEDED")
    details = getattr(exc, "details", {})
    return JSONResponse(
        status_code=413,
        content={
            "code": code,
            "message": str(getattr(exc, "message", exc)),
            "details": details,
        },
    )
