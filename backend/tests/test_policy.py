import pytest
from fastapi.testclient import TestClient

from app.core.errors import POLICY_DENIED, SCHEMA_VALIDATION_FAILED
from app.core.policy import PolicyEvaluator

# =====================================================================
# Unit Tests: Static URL Validation
# =====================================================================


def test_static_url_valid_loopback(policy_evaluator: PolicyEvaluator) -> None:
    allowlist = ["localhost", "127.0.0.1", "rewardlab.local"]
    valid, reason, _ = policy_evaluator.validate_static_url(
        "http://127.0.0.1:8001/tasks", allowlist
    )
    assert valid is True
    assert reason == "URL allowed"

    valid, reason, _ = policy_evaluator.validate_static_url(
        "http://localhost:8001/dashboard", allowlist
    )
    assert valid is True


def test_static_url_valid_https(policy_evaluator: PolicyEvaluator) -> None:
    allowlist = ["rewardlab.local"]
    valid, reason, _ = policy_evaluator.validate_static_url(
        "https://rewardlab.local/tasks", allowlist
    )
    assert valid is True


@pytest.mark.parametrize(
    "invalid_url,expected_keyword",
    [
        ("ftp://rewardlab.local/resource", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("data:text/html;base64,PHNjcmlwdD4=", "scheme"),
    ],
)
def test_static_url_prohibited_schemes(
    policy_evaluator: PolicyEvaluator, invalid_url: str, expected_keyword: str
) -> None:
    allowlist = ["localhost", "127.0.0.1", "rewardlab.local"]
    valid, reason, _ = policy_evaluator.validate_static_url(invalid_url, allowlist)
    assert valid is False
    assert expected_keyword in reason.lower()


def test_static_url_prohibits_insecure_http_on_remote_hosts(
    policy_evaluator: PolicyEvaluator,
) -> None:
    allowlist = ["rewardlab.local"]
    valid, reason, _ = policy_evaluator.validate_static_url(
        "http://rewardlab.local/tasks", allowlist
    )
    assert valid is False
    assert "insecure http scheme prohibited" in reason.lower()


@pytest.mark.parametrize(
    "credential_url",
    [
        "http://admin:secret123@127.0.0.1:8001/login",
        "https://user:password@rewardlab.local/dashboard",
    ],
)
def test_static_url_prohibits_embedded_credentials(
    policy_evaluator: PolicyEvaluator, credential_url: str
) -> None:
    allowlist = ["127.0.0.1", "rewardlab.local"]
    valid, reason, sanitized = policy_evaluator.validate_static_url(credential_url, allowlist)
    assert valid is False
    assert "embedded credentials" in reason.lower()
    assert "secret123" not in str(sanitized)
    assert "password" not in str(sanitized)


@pytest.mark.parametrize(
    "spoofed_url",
    [
        "https://rewardlab.local.attacker.com/tasks",
        "https://attacker-rewardlab.local/tasks",
        "https://evil.rewardlab.local/tasks",
    ],
)
def test_static_url_prohibits_domain_spoofing(
    policy_evaluator: PolicyEvaluator, spoofed_url: str
) -> None:
    allowlist = ["rewardlab.local"]
    valid, reason, _ = policy_evaluator.validate_static_url(spoofed_url, allowlist)
    assert valid is False
    assert "not in provider domain allowlist" in reason.lower()


@pytest.mark.parametrize(
    "metadata_url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
    ],
)
def test_static_url_prohibits_metadata_addresses(
    policy_evaluator: PolicyEvaluator, metadata_url: str
) -> None:
    allowlist = ["localhost", "127.0.0.1", "169.254.169.254", "metadata.google.internal"]
    valid, reason, _ = policy_evaluator.validate_static_url(metadata_url, allowlist)
    assert valid is False
    assert "metadata" in reason.lower() or "prohibited" in reason.lower()


# =====================================================================
# Unit Tests: Policy Evaluator Core
# =====================================================================


def test_policy_evaluator_allows_rewardlab_valid_action(
    policy_evaluator: PolicyEvaluator,
) -> None:
    decision = policy_evaluator.evaluate_action(
        provider="rewardlab",
        action="navigate",
        target_url="http://127.0.0.1:8001/tasks",
    )
    assert decision.allowed is True
    assert decision.error_code is None


def test_policy_evaluator_denies_microsoft_rewards_automation(
    policy_evaluator: PolicyEvaluator,
) -> None:
    decision = policy_evaluator.evaluate_action(
        provider="microsoft_rewards",
        action="navigate",
        target_url="https://rewards.bing.com",
    )
    assert decision.allowed is False
    assert decision.error_code == POLICY_DENIED
    assert "companion-only" in decision.reason


def test_policy_evaluator_denies_unknown_provider(
    policy_evaluator: PolicyEvaluator,
) -> None:
    decision = policy_evaluator.evaluate_action(
        provider="unknown_third_party",
        action="navigate",
        target_url="https://example.com",
    )
    assert decision.allowed is False
    assert decision.error_code == POLICY_DENIED


def test_policy_evaluator_denies_disallowed_action(
    policy_evaluator: PolicyEvaluator,
) -> None:
    decision = policy_evaluator.evaluate_action(
        provider="rewardlab",
        action="eval_arbitrary_code",
        target_url="http://127.0.0.1:8001/tasks",
    )
    assert decision.allowed is False
    assert decision.error_code == POLICY_DENIED
    assert "not permitted" in decision.reason


def test_policy_evaluator_records_audit_event(
    policy_evaluator: PolicyEvaluator,
) -> None:
    policy_evaluator.evaluate_action(
        provider="microsoft_rewards",
        action="click",
        target_url="https://rewards.bing.com",
    )
    assert len(policy_evaluator.audit_log) == 1
    event = policy_evaluator.audit_log[0]
    assert event.outcome == "DENIED"
    assert event.resource == "provider:microsoft_rewards"
    assert event.action == "click"


# =====================================================================
# Integration Tests: Draft Workflow Validation API Endpoint
# =====================================================================


def test_validate_draft_workflow_allowed(client: TestClient) -> None:
    payload = {
        "schema_version": 1,
        "provider": "rewardlab",
        "steps": [
            {
                "id": "step_1",
                "action": "navigate",
                "target_url": "http://127.0.0.1:8001/tasks",
            },
            {
                "id": "step_2",
                "action": "click",
                "target": {"by": "test_id", "value": "submit-button"},
                "side_effect": True,
            },
        ],
    }
    response = client.post("/api/v1/workflows/validate", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True
    assert data["provider"] == "rewardlab"
    assert data["step_count"] == 2


def test_validate_draft_workflow_denied_for_microsoft_rewards(
    client: TestClient,
) -> None:
    payload = {
        "schema_version": 1,
        "provider": "microsoft_rewards",
        "steps": [
            {
                "id": "step_1",
                "action": "navigate",
                "target_url": "https://rewards.bing.com",
            }
        ],
    }
    response = client.post("/api/v1/workflows/validate", json=payload)
    assert response.status_code == 403
    data = response.json()
    assert data["code"] == POLICY_DENIED
    assert "microsoft rewards is companion-only" in data["message"].lower()
    assert data["details"]["provider"] == "microsoft_rewards"


def test_validate_draft_workflow_denied_for_unknown_provider(
    client: TestClient,
) -> None:
    payload = {
        "schema_version": 1,
        "provider": "external_unauthorized_site",
        "steps": [
            {
                "id": "step_1",
                "action": "navigate",
                "target_url": "https://unauthorized.com",
            }
        ],
    }
    response = client.post("/api/v1/workflows/validate", json=payload)
    assert response.status_code == 403
    data = response.json()
    assert data["code"] == POLICY_DENIED


def test_validate_draft_workflow_denied_for_invalid_url(client: TestClient) -> None:
    payload = {
        "schema_version": 1,
        "provider": "rewardlab",
        "steps": [
            {
                "id": "step_1",
                "action": "navigate",
                "target_url": "http://user:secretpass@127.0.0.1:8001/tasks",
            }
        ],
    }
    response = client.post("/api/v1/workflows/validate", json=payload)
    assert response.status_code == 403
    data = response.json()
    assert data["code"] == POLICY_DENIED
    assert "embedded credentials" in data["message"].lower()
    # Confirm secretpass was redacted and not leaked
    assert "secretpass" not in str(data)


def test_validate_draft_workflow_schema_error_on_empty_steps(
    client: TestClient,
) -> None:
    payload = {
        "schema_version": 1,
        "provider": "rewardlab",
        "steps": [],
    }
    response = client.post("/api/v1/workflows/validate", json=payload)
    assert response.status_code == 422
    data = response.json()
    assert data["code"] == SCHEMA_VALIDATION_FAILED
    assert "errors" in data["details"]


def test_validate_draft_workflow_schema_error_on_missing_required_fields(
    client: TestClient,
) -> None:
    payload = {
        "schema_version": "not-an-integer",
    }
    response = client.post("/api/v1/workflows/validate", json=payload)
    assert response.status_code == 422
    data = response.json()
    assert data["code"] == SCHEMA_VALIDATION_FAILED
