from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_v1_router
from app.core.config import settings
from app.core.errors import (
    PolicyDeniedException,
    policy_denied_exception_handler,
    validation_exception_handler,
    workflow_size_exception_handler,
    workflow_syntax_exception_handler,
    workflow_validation_exception_handler,
)
from app.workflows.errors import (
    WorkflowSizeExceededError,
    WorkflowSyntaxError,
    WorkflowValidationError,
)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Adaptive Distributed Browser Automation Platform (ADBAP) Control Plane",
)

# Restrict CORS to configured origins (default: loopback development ports)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Exception handlers with stable error codes
app.add_exception_handler(PolicyDeniedException, policy_denied_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(WorkflowValidationError, workflow_validation_exception_handler)
app.add_exception_handler(WorkflowSyntaxError, workflow_syntax_exception_handler)
app.add_exception_handler(WorkflowSizeExceededError, workflow_size_exception_handler)

# Mount API routers
app.include_router(api_v1_router)
