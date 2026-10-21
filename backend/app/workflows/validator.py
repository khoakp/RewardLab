from typing import Any, Literal

from pydantic import ValidationError

from app.core.errors import PolicyDeniedException
from app.core.policy import default_evaluator
from app.workflows.errors import (
    UnsupportedSchemaVersionError,
    WorkflowValidationError,
)
from app.workflows.parser import parse_workflow_content
from app.workflows.schema import (
    SUPPORTED_SCHEMA_VERSIONS,
    NavigateStep,
    VerifyStateStep,
    WorkflowDefinition,
)


def validate_workflow_definition(
    data: dict[str, Any] | WorkflowDefinition,
    policy_evaluator: Any = None,
) -> WorkflowDefinition:
    """Strictly validates a workflow dictionary or object against the DSL schema

    and provider security policies.

    Raises:
    - UnsupportedSchemaVersionError: if schema_version is unsupported.
    - WorkflowValidationError: if schema constraints, locators, or step IDs fail.
    - PolicyDeniedException: if provider, action, or target URL violates policy.
    """
    evaluator = policy_evaluator or default_evaluator

    # 1. If already a validated WorkflowDefinition instance, use it
    if isinstance(data, WorkflowDefinition):
        workflow = data
    elif isinstance(data, dict):
        # Check integer schema_version before Pydantic parsing for explicit version checking
        raw_version = data.get("schema_version")
        if isinstance(raw_version, int) and raw_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise UnsupportedSchemaVersionError(raw_version, SUPPORTED_SCHEMA_VERSIONS)

        try:
            workflow = WorkflowDefinition.model_validate(data)
        except ValidationError as e:
            errors_summary = []
            for err in e.errors():
                loc = " -> ".join(str(p) for p in err.get("loc", []))
                msg = err.get("msg", "Validation error")
                err_type = err.get("type", "")
                errors_summary.append(f"[{loc}] {msg} ({err_type})")
            raise WorkflowValidationError(
                f"Workflow schema validation failed with {len(errors_summary)} error(s): "
                + "; ".join(errors_summary),
                details={"errors": e.errors()},
            ) from e
    else:
        raise WorkflowValidationError(
            f"Expected dict or WorkflowDefinition, got {type(data).__name__}"
        )

    # 2. Check capabilities requested by the workflow
    if workflow.requested_capabilities and hasattr(evaluator, "evaluate_capabilities"):
        cap_decision = evaluator.evaluate_capabilities(
            provider=workflow.provider,
            requested_capabilities=workflow.requested_capabilities,
        )
        if not cap_decision.allowed:
            raise PolicyDeniedException(
                message=cap_decision.reason,
                details={
                    "provider": workflow.provider,
                    "workflow_id": workflow.id,
                    "reason": cap_decision.reason,
                    "error_code": cap_decision.error_code,
                },
            )

    # 3. Evaluate every step against ProviderPolicy
    for step in workflow.steps:
        target_url = None
        if isinstance(step, NavigateStep):
            target_url = step.url or step.target_url

        # Check API endpoint for SSRF / external URL escaping
        if isinstance(step, VerifyStateStep) and step.source == "rewardlab_api":
            ep = getattr(step, "endpoint", None)
            if ep and isinstance(ep, str):
                ep_clean = ep.strip()
                if "://" in ep_clean or ep_clean.startswith("//"):
                    raise PolicyDeniedException(
                        message=(
                            "API endpoint must be a relative path, not an absolute or foreign URL"
                        ),
                        details={
                            "provider": workflow.provider,
                            "workflow_id": workflow.id,
                            "step_id": step.id,
                            "endpoint": ep_clean,
                        },
                    )
            invalid_task = step.task_id and (
                "/" in step.task_id or "\\" in step.task_id or ".." in step.task_id
            )
            if invalid_task:
                raise PolicyDeniedException(
                    message="API verification task_id contains invalid path traversal characters",
                    details={
                        "provider": workflow.provider,
                        "workflow_id": workflow.id,
                        "step_id": step.id,
                        "task_id": step.task_id,
                    },
                )

        step_decision = evaluator.evaluate_action(
            provider=workflow.provider,
            action=step.action,
            target_url=target_url,
        )
        if not step_decision.allowed:
            raise PolicyDeniedException(
                message=step_decision.reason,
                details={
                    "provider": workflow.provider,
                    "workflow_id": workflow.id,
                    "step_id": step.id,
                    "action": step.action,
                    "target_url": step_decision.sanitized_target,
                    "reason": step_decision.reason,
                },
            )

    return workflow


def parse_and_validate_workflow(
    content: str | bytes,
    format_hint: Literal["auto", "yaml", "json"] = "auto",
    policy_evaluator: Any = None,
) -> WorkflowDefinition:
    """Parse raw YAML or JSON workflow content safely and validate against DSL schema and policy."""
    parsed_dict = parse_workflow_content(content=content, format_hint=format_hint)
    return validate_workflow_definition(parsed_dict, policy_evaluator=policy_evaluator)
