import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from faults import fault_controller


def test_default_test_mode_is_false() -> None:
    """Security: Verify REWARDLAB_TEST_MODE defaults to false when env var is unset."""
    with patch.dict(os.environ, {}, clear=True):
        # Clear any override
        fault_controller.set_test_mode(None)
        assert fault_controller.is_test_mode() is False


def test_test_mode_requires_explicit_opt_in() -> None:
    """Security: Verify test mode only activates with explicit truthy values."""
    for truthy in ["1", "true", "True", "yes", "on", "ON"]:
        with patch.dict(os.environ, {"REWARDLAB_TEST_MODE": truthy}):
            fault_controller.set_test_mode(None)
            assert fault_controller.is_test_mode() is True

    for falsy in ["0", "false", "False", "no", "off", "anything_else", ""]:
        with patch.dict(os.environ, {"REWARDLAB_TEST_MODE": falsy}):
            fault_controller.set_test_mode(None)
            assert fault_controller.is_test_mode() is False


def test_production_like_default_denies_fault_injection(client: TestClient) -> None:
    """Security: When test mode is disabled, test control and inspection endpoints return 403."""
    fault_controller.set_test_mode(False)
    try:
        # Fault control endpoints
        assert client.get("/_test/faults/state").status_code == 403
        assert (
            client.post(
                "/_test/faults/configure",
                json={"fault_type": "delay", "path": "/api/tasks", "seconds": 0.1},
            ).status_code
            == 403
        )
        assert client.post("/_test/faults/reset").status_code == 403

        # Account inspection endpoints
        assert client.get("/api/accounts/test_01").status_code == 403
        assert client.get("/api/accounts/test_01/tasks/task_01").status_code == 403
    finally:
        fault_controller.set_test_mode(None)


def test_fault_headers_ignored_when_test_mode_disabled(
    authenticated_client: tuple[TestClient, str],
) -> None:
    """Security: X-Test-Fault-* headers must be ignored when test mode is disabled."""
    client, _ = authenticated_client
    fault_controller.set_test_mode(False)

    try:
        # 1. Delay header ignored
        resp_delay = client.get("/api/health", headers={"X-Test-Fault-Delay": "0.5"})
        assert resp_delay.status_code == 200

        # 2. 500 header ignored
        resp_500 = client.get("/api/health", headers={"X-Test-Fault-500": "1"})
        assert resp_500.status_code == 200

        # 3. 429 header ignored
        resp_429 = client.get("/api/health", headers={"X-Test-Fault-429": "5"})
        assert resp_429.status_code == 200

        # 4. Expire-session header ignored
        resp_auth = client.get("/api/tasks", headers={"X-Test-Fault-Expire-Session": "1"})
        assert resp_auth.status_code == 200

        # 5. Navigation failure header ignored
        resp_nav = client.get("/api/health", headers={"X-Test-Fault-Navigation": "503"})
        assert resp_nav.status_code == 200

        # 6. UI Button variant header ignored
        resp_ui = client.get("/tasks/task_01", headers={"X-Test-Fault-UI-Button": "Compromised"})
        assert resp_ui.status_code == 200
        assert "Mark as reviewed" in resp_ui.text
        assert "Compromised" not in resp_ui.text
    finally:
        fault_controller.set_test_mode(None)


def test_defense_in_depth_remote_ip_denied(client: TestClient) -> None:
    """Security: Even in test mode, non-loopback clients are denied with 403."""
    fault_controller.set_test_mode(True)

    remote_headers = {"X-Forwarded-For": "203.0.113.195"}

    # Test control endpoints
    assert client.get("/_test/faults/state", headers=remote_headers).status_code == 403
    assert (
        client.post(
            "/_test/faults/configure",
            json={"fault_type": "delay", "path": "/api/tasks", "seconds": 0.1},
            headers=remote_headers,
        ).status_code
        == 403
    )
    assert client.post("/_test/faults/reset", headers=remote_headers).status_code == 403

    # Account inspection endpoints
    assert client.get("/api/accounts/test_01", headers=remote_headers).status_code == 403
    assert (
        client.get("/api/accounts/test_01/tasks/task_01", headers=remote_headers).status_code == 403
    )


def test_credentials_and_tokens_not_leaked(client: TestClient) -> None:
    """Security: Credentials, passwords and raw session tokens are never leaked."""
    fault_controller.set_test_mode(True)

    # 1. Check fault state endpoint does NOT expose raw session tokens
    fault_controller.expire_session("raw-session-token-secret-12345")
    state_resp = client.get("/_test/faults/state")
    assert state_resp.status_code == 200
    state = state_resp.json()
    assert "expired_sessions" not in state
    assert state["expired_sessions_count"] >= 1
    assert "raw-session-token-secret-12345" not in state_resp.text

    # 2. Account inspection does not expose passwords
    acc_resp = client.get("/api/accounts/test_01")
    assert acc_resp.status_code == 200
    acc_data = acc_resp.json()
    assert "password" not in acc_data
    assert "test_password_01" not in acc_resp.text

    # 3. Failed login does not leak password in response
    login_resp = client.post(
        "/login",
        data={"username": "test_01", "password": "super-secret-password-xyz"},
        follow_redirects=False,
    )
    assert login_resp.status_code == 401
    assert "super-secret-password-xyz" not in login_resp.text
