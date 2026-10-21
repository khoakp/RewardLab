from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.errors import POLICY_DENIED, PolicyDeniedException
from app.workflows.errors import (
    UnsupportedSchemaVersionError,
    WorkflowSizeExceededError,
    WorkflowSyntaxError,
    WorkflowValidationError,
)
from app.workflows.parser import MAX_WORKFLOW_SIZE_BYTES
from app.workflows.schema import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    ClickStep,
    NavigateStep,
)
from app.workflows.validator import (
    parse_and_validate_workflow,
    validate_workflow_definition,
)

SAMPLE_DIR = Path(__file__).resolve().parent.parent.parent / "examples" / "workflows"


# =============================================================================
# 1. Valid Workflow Parsing & Validation Tests
# =============================================================================


def test_valid_yaml_workflow_parsing_and_validation() -> None:
    """DSL: Verify a complete, valid YAML workflow parses and validates cleanly."""
    yaml_text = """
    schema_version: 1
    id: sample_review_workflow
    name: Sample Task Review
    description: Inspect and click mark as reviewed on RewardLab.
    provider: rewardlab
    requested_capabilities:
      - read_tasks
      - mark_reviewed
    execution:
      timeout_seconds: 45
      max_retries: 2
      step_timeout_seconds: 20
    steps:
      - id: step_nav
        action: navigate
        path: /tasks
      - id: step_wait
        action: wait_visible
        target:
          by: test_id
          value: task-item-01
      - id: step_extract
        action: extract_text
        target:
          by: test_id
          value: task-description
        output: desc_output
      - id: step_click
        action: click
        target:
          by: role
          role: button
          name: Mark as reviewed
        side_effect: true
      - id: step_verify
        action: verify_state
        source: rewardlab_api
        field: reviewed
        equals: true
    """
    wf = parse_and_validate_workflow(yaml_text, format_hint="yaml")
    assert wf.schema_version == 1
    assert wf.id == "sample_review_workflow"
    assert wf.provider == "rewardlab"
    assert len(wf.steps) == 5
    assert isinstance(wf.steps[0], NavigateStep)
    assert wf.steps[0].path == "/tasks"
    assert isinstance(wf.steps[3], ClickStep)
    assert wf.steps[3].side_effect is True


def test_valid_json_workflow_parsing_and_validation() -> None:
    """DSL: Verify a valid JSON workflow parses and validates cleanly."""
    json_text = """{
      "schema_version": 1,
      "id": "json_workflow_01",
      "provider": "rewardlab",
      "requested_capabilities": ["read_tasks"],
      "execution": {
        "timeout_seconds": 30,
        "max_retries": 1
      },
      "steps": [
        {
          "id": "nav_step",
          "action": "navigate",
          "url": "http://127.0.0.1:8001/tasks"
        },
        {
          "id": "assert_step",
          "action": "assert_visible",
          "target": {
            "by": "role",
            "role": "heading",
            "name": "Tasks"
          }
        }
      ]
    }"""
    wf = parse_and_validate_workflow(json_text, format_hint="json")
    assert wf.schema_version == 1
    assert wf.id == "json_workflow_01"
    assert len(wf.steps) == 2


def test_canonical_example_yaml_file_validates() -> None:
    """DSL: Verify the shipped example YAML file in examples/workflows/ validates."""
    sample_file = SAMPLE_DIR / "rewardlab_review_first_task.yaml"
    assert sample_file.exists(), f"Sample YAML not found at {sample_file}"

    content = sample_file.read_text(encoding="utf-8")
    wf = parse_and_validate_workflow(content, format_hint="yaml")
    assert wf.schema_version == CURRENT_SCHEMA_VERSION
    assert wf.id == "rewardlab_review_first_task"
    assert wf.provider == "rewardlab"
    assert len(wf.steps) == 4


# =============================================================================
# 2. Schema Versioning & Error Policy Tests
# =============================================================================


def test_unsupported_schema_version_rejected() -> None:
    """Version: Specifying an unsupported schema_version raises UnsupportedSchemaVersionError."""
    yaml_text = """
    schema_version: 99
    id: future_workflow
    provider: rewardlab
    steps:
      - id: step_1
        action: navigate
        path: /tasks
    """
    with pytest.raises(UnsupportedSchemaVersionError) as exc_info:
        parse_and_validate_workflow(yaml_text)

    err = exc_info.value
    assert err.code == "UNSUPPORTED_SCHEMA_VERSION"
    assert "Unsupported workflow schema_version '99'" in err.message
    assert err.details["provided_version"] == 99
    assert err.details["supported_versions"] == sorted(SUPPORTED_SCHEMA_VERSIONS)


def test_invalid_schema_version_type_rejected() -> None:
    """Version: Non-integer schema_version fails schema validation."""
    yaml_text = """
    schema_version: "v1.0"
    id: bad_version_type
    provider: rewardlab
    steps:
      - id: step_1
        action: navigate
        path: /tasks
    """
    with pytest.raises(WorkflowValidationError):
        parse_and_validate_workflow(yaml_text)


# =============================================================================
# 3. Missing Fields & Unknown Field/Action Rejection Tests
# =============================================================================


def test_missing_required_provider_rejected() -> None:
    """Validation: Missing provider raises WorkflowValidationError."""
    yaml_text = """
    schema_version: 1
    id: missing_provider
    steps:
      - id: step_1
        action: navigate
        path: /tasks
    """
    with pytest.raises(WorkflowValidationError) as exc:
        parse_and_validate_workflow(yaml_text)
    assert "provider" in str(exc.value)


def test_missing_steps_rejected() -> None:
    """Validation: Missing or empty steps raises WorkflowValidationError."""
    yaml_text = """
    schema_version: 1
    id: empty_steps
    provider: rewardlab
    steps: []
    """
    with pytest.raises(WorkflowValidationError) as exc:
        parse_and_validate_workflow(yaml_text)
    assert "steps" in str(exc.value)


def test_unknown_action_type_rejected() -> None:
    """Security: Unknown action types (e.g. execute_script) are rejected by discriminated union."""
    yaml_text = """
    schema_version: 1
    id: malicious_action
    provider: rewardlab
    steps:
      - id: step_eval
        action: execute_arbitrary_code
        script: "import os; os.system('echo compromised')"
    """
    with pytest.raises(WorkflowValidationError) as exc:
        parse_and_validate_workflow(yaml_text)
    assert "action" in str(exc.value).lower()


def test_unknown_fields_strictly_forbidden() -> None:
    """Security: Extra/unknown fields on root or steps are strictly rejected (extra='forbid')."""
    yaml_text = """
    schema_version: 1
    id: extra_fields_workflow
    provider: rewardlab
    malicious_global_hook: "evil_code()"
    steps:
      - id: step_1
        action: navigate
        path: /tasks
        extra_arbitrary_param: "not_allowed"
    """
    with pytest.raises(WorkflowValidationError) as exc:
        parse_and_validate_workflow(yaml_text)
    err_str = str(exc.value)
    assert "extra_forbidden" in err_str or "extra fields" in err_str.lower()


# =============================================================================
# 4. Step Uniqueness & Locator Validation Tests
# =============================================================================


def test_duplicate_step_ids_rejected() -> None:
    """Validation: Duplicate step IDs within a workflow are strictly forbidden."""
    yaml_text = """
    schema_version: 1
    id: dup_step_workflow
    provider: rewardlab
    steps:
      - id: step_duplicate_id
        action: navigate
        path: /tasks
      - id: step_duplicate_id
        action: wait_visible
        target:
          by: test_id
          value: task-list
    """
    with pytest.raises(WorkflowValidationError) as exc:
        parse_and_validate_workflow(yaml_text)
    assert "Duplicate step IDs" in str(exc.value)
    assert "step_duplicate_id" in str(exc.value)


def test_invalid_locators_rejected() -> None:
    """Validation: Invalid locators (empty value, missing role, unknown strategy) are rejected."""
    # 1. Empty test_id value
    y1 = """
    schema_version: 1
    id: bad_loc_1
    provider: rewardlab
    steps:
      - id: s1
        action: wait_visible
        target:
          by: test_id
          value: "   "
    """
    with pytest.raises(WorkflowValidationError):
        parse_and_validate_workflow(y1)

    # 2. Missing role field in role locator
    y2 = """
    schema_version: 1
    id: bad_loc_2
    provider: rewardlab
    steps:
      - id: s1
        action: wait_visible
        target:
          by: role
          name: "Click me"
    """
    with pytest.raises(WorkflowValidationError):
        parse_and_validate_workflow(y2)

    # 3. Unknown locator strategy
    y3 = """
    schema_version: 1
    id: bad_loc_3
    provider: rewardlab
    steps:
      - id: s1
        action: wait_visible
        target:
          by: magical_ai_guess
          value: "something"
    """
    with pytest.raises(WorkflowValidationError):
        parse_and_validate_workflow(y3)


# =============================================================================
# 5. Parser Security: Unsafe Tags, Duplicate Keys & Size Limits
# =============================================================================


def test_unsafe_yaml_tags_rejected() -> None:
    """Security: Arbitrary Python object constructors in YAML are strictly rejected."""
    unsafe_yaml = """
    schema_version: 1
    id: unsafe_tag_workflow
    provider: rewardlab
    steps:
      - id: s1
        action: !!python/object/apply:os.system ["whoami"]
    """
    with pytest.raises(WorkflowSyntaxError) as exc:
        parse_and_validate_workflow(unsafe_yaml)
    assert "Invalid YAML syntax" in exc.value.message or "tag" in exc.value.message.lower()


def test_duplicate_yaml_keys_rejected() -> None:
    """Security: Duplicate keys in YAML mappings are strictly rejected by UniqueKeySafeLoader."""
    dup_yaml = """
    schema_version: 1
    id: dup_key_workflow
    provider: rewardlab
    provider: attacker_override
    steps:
      - id: s1
        action: navigate
        path: /tasks
    """
    with pytest.raises(WorkflowSyntaxError) as exc:
        parse_and_validate_workflow(dup_yaml)
    assert "Duplicate YAML key 'provider' detected" in exc.value.message


def test_duplicate_json_keys_rejected() -> None:
    """Security: Duplicate keys in JSON objects are strictly rejected."""
    dup_json = """{
      "schema_version": 1,
      "id": "dup_json_workflow",
      "provider": "rewardlab",
      "provider": "attacker_override",
      "steps": [
        {"id": "s1", "action": "navigate", "path": "/tasks"}
      ]
    }"""
    with pytest.raises(WorkflowSyntaxError) as exc:
        parse_and_validate_workflow(dup_json, format_hint="json")
    assert "Duplicate JSON key 'provider' detected" in exc.value.message


def test_oversized_workflow_rejected() -> None:
    """Security: Workflows exceeding MAX_WORKFLOW_SIZE_BYTES (256 KB) are rejected immediately."""
    huge_comment = "# " + ("A" * (MAX_WORKFLOW_SIZE_BYTES + 500))
    huge_yaml = f"""
    schema_version: 1
    id: huge_workflow
    provider: rewardlab
    {huge_comment}
    steps:
      - id: s1
        action: navigate
        path: /tasks
    """
    with pytest.raises(WorkflowSizeExceededError) as exc:
        parse_and_validate_workflow(huge_yaml)
    assert exc.value.code == "WORKFLOW_SIZE_EXCEEDED"
    assert "exceeds maximum limit" in exc.value.message


def test_execution_limits_boundary_validation() -> None:
    """Validation: Execution timeouts and retries outside bounds are rejected."""
    # timeout_seconds > 300
    y_timeout = """
    schema_version: 1
    id: bad_timeout
    provider: rewardlab
    execution:
      timeout_seconds: 301
    steps:
      - id: s1
        action: navigate
        path: /tasks
    """
    with pytest.raises(WorkflowValidationError):
        parse_and_validate_workflow(y_timeout)

    # max_retries > 5
    y_retries = """
    schema_version: 1
    id: bad_retries
    provider: rewardlab
    execution:
      max_retries: 10
    steps:
      - id: s1
        action: navigate
        path: /tasks
    """
    with pytest.raises(WorkflowValidationError):
        parse_and_validate_workflow(y_retries)


# =============================================================================
# 6. Policy Integration & URL Security Tests
# =============================================================================


def test_policy_denial_for_microsoft_rewards() -> None:
    """Policy: Automation workflows targeting Microsoft Rewards are strictly denied."""
    rewards_yaml = """
    schema_version: 1
    id: ms_rewards_auto
    provider: microsoft_rewards
    steps:
      - id: s1
        action: navigate
        url: https://rewards.bing.com
    """
    with pytest.raises(PolicyDeniedException) as exc:
        parse_and_validate_workflow(rewards_yaml)
    assert "companion-only" in exc.value.message.lower()


def test_policy_denial_for_unknown_provider() -> None:
    """Policy: Workflows targeting unauthorized / unknown providers are denied by default."""
    unknown_yaml = """
    schema_version: 1
    id: unknown_prov_wf
    provider: unauthorized_external_bank
    steps:
      - id: s1
        action: navigate
        url: https://bank.example.com
    """
    with pytest.raises(PolicyDeniedException) as exc:
        parse_and_validate_workflow(unknown_yaml)
    assert "prohibited for provider" in exc.value.message.lower()


def test_policy_denial_for_disallowed_destination_url() -> None:
    """Policy: Workflows attempting to navigate to an unauthorized domain are denied."""
    evil_yaml = """
    schema_version: 1
    id: evil_nav_wf
    provider: rewardlab
    steps:
      - id: s1
        action: navigate
        url: https://attacker.com/malicious
    """
    with pytest.raises(PolicyDeniedException) as exc:
        parse_and_validate_workflow(evil_yaml)
    assert "domain allowlist" in exc.value.message.lower()


def test_policy_denial_for_prohibited_url_scheme() -> None:
    """Security: Prohibited schemes (javascript:, file:, ftp:) are rejected."""
    for bad_scheme_url in ["javascript:alert(1)", "file:///etc/passwd", "ftp://rewardlab.local/x"]:
        data = {
            "schema_version": 1,
            "id": "bad_scheme_wf",
            "provider": "rewardlab",
            "steps": [{"id": "s1", "action": "navigate", "url": bad_scheme_url}],
        }
        with pytest.raises((WorkflowValidationError, PolicyDeniedException)):
            validate_workflow_definition(data)


# =============================================================================
# 7. FastAPI Endpoint Integration Tests (POST /api/v1/workflows/validate)
# =============================================================================


def test_api_validate_workflow_with_yaml_payload(client: TestClient) -> None:
    """API: Verify POST /api/v1/workflows/validate accepts raw YAML with application/x-yaml."""
    yaml_content = """
    schema_version: 1
    id: api_yaml_workflow
    provider: rewardlab
    steps:
      - id: s1
        action: navigate
        path: /tasks
      - id: s2
        action: click
        target:
          by: test_id
          value: btn-review
    """
    resp = client.post(
        "/api/v1/workflows/validate",
        content=yaml_content,
        headers={"Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["provider"] == "rewardlab"
    assert data["step_count"] == 2


def test_api_validate_workflow_rejects_duplicate_yaml_keys(client: TestClient) -> None:
    """API: POST /api/v1/workflows/validate returns 422 when YAML contains duplicate keys."""
    yaml_content = """
    schema_version: 1
    id: dup_keys_api
    provider: rewardlab
    provider: compromised
    steps:
      - id: s1
        action: navigate
        path: /tasks
    """
    resp = client.post(
        "/api/v1/workflows/validate",
        content=yaml_content,
        headers={"Content-Type": "application/x-yaml"},
    )
    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "WORKFLOW_SYNTAX_ERROR"
    assert "Duplicate YAML key" in data["message"]


def test_api_validate_workflow_unsupported_version_returns_422(client: TestClient) -> None:
    """API: POST /api/v1/workflows/validate returns 422 with UNSUPPORTED_SCHEMA_VERSION."""
    payload = {
        "schema_version": 42,
        "id": "future_version_api",
        "provider": "rewardlab",
        "steps": [{"id": "s1", "action": "navigate", "path": "/tasks"}],
    }
    resp = client.post("/api/v1/workflows/validate", json=payload)
    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "UNSUPPORTED_SCHEMA_VERSION"
    assert "Unsupported workflow schema_version '42'" in data["message"]


def test_api_validate_workflow_policy_denied_returns_403(client: TestClient) -> None:
    """API: POST /api/v1/workflows/validate returns 403 POLICY_DENIED for unauthorized provider."""
    payload = {
        "schema_version": 1,
        "id": "rewards_api_call",
        "provider": "microsoft_rewards",
        "steps": [{"id": "s1", "action": "navigate", "url": "https://rewards.bing.com"}],
    }
    resp = client.post("/api/v1/workflows/validate", json=payload)
    assert resp.status_code == 403
    data = resp.json()
    assert data["code"] == POLICY_DENIED
    assert "companion-only" in data["message"].lower()
