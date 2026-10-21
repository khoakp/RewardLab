from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.workflows.errors import WorkflowSyntaxError
from app.workflows.parser import parse_workflow_content
from app.workflows.validator import (
    parse_and_validate_workflow,
    validate_workflow_definition,
)

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowValidationResponse(BaseModel):
    valid: bool
    provider: str
    step_count: int
    message: str


@router.post("/validate", response_model=WorkflowValidationResponse)
async def validate_draft_workflow(
    request: Request,
) -> WorkflowValidationResponse:
    """Validates a draft workflow definition against the strict DSL schema,

    syntax rules, deny-by-default provider policy and static URL constraints.
    Supports both JSON and raw YAML payloads.
    """
    content_type = request.headers.get("content-type", "").lower()
    raw_body = await request.body()

    if "yaml" in content_type:
        workflow = parse_and_validate_workflow(raw_body, format_hint="yaml")
    else:
        # JSON payload
        try:
            parsed_data = parse_workflow_content(raw_body, format_hint="json")
        except WorkflowSyntaxError:
            raise
        except Exception as e:
            raise WorkflowSyntaxError(f"Malformed JSON request payload: {e}") from e

        workflow = validate_workflow_definition(parsed_data)

    return WorkflowValidationResponse(
        valid=True,
        provider=workflow.provider,
        step_count=len(workflow.steps),
        message="Workflow conforms to schema and provider policy",
    )
