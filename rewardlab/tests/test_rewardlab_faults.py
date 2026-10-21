import time

from fastapi.testclient import TestClient

from faults import fault_controller


def test_delayed_rendering_fault(authenticated_client: tuple[TestClient, str]) -> None:
    """Scenario 1: Delayed rendering on specified route; clears after count."""
    client, _ = authenticated_client

    # Configure 0.15s delay on /tasks for 1 request
    fault_controller.set_delay(path="/tasks", seconds=0.15, count=1)

    t0 = time.perf_counter()
    resp1 = client.get("/tasks")
    elapsed1 = time.perf_counter() - t0

    assert resp1.status_code == 200
    assert elapsed1 >= 0.12
    assert "Daily Research Memo" in resp1.text

    # Next call should return without injected delay (rule exhausted)
    t1 = time.perf_counter()
    resp2 = client.get("/tasks")
    elapsed2 = time.perf_counter() - t1

    assert resp2.status_code == 200
    assert elapsed2 < 0.1


def test_delayed_rendering_via_header(authenticated_client: tuple[TestClient, str]) -> None:
    """Scenario 1 (Header): Request-scoped delay via X-Test-Fault-Delay header."""
    client, _ = authenticated_client

    t0 = time.perf_counter()
    resp = client.get("/api/tasks", headers={"X-Test-Fault-Delay": "0.15"})
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 200
    assert elapsed >= 0.12


def test_transient_http_500_and_safe_retry(authenticated_client: tuple[TestClient, str]) -> None:
    """Scenario 2: Transient 500 error causes NO side-effect; retry completes safely once."""
    client, _ = authenticated_client

    target_path = "/api/tasks/task_01/complete"
    fault_controller.set_transient_error(path=target_path, status_code=500, count=1)

    # 1. Check baseline: task_01 is not reviewed, test_01 points = 120
    status_resp = client.get("/api/tasks/task_01")
    assert status_resp.status_code == 200
    assert status_resp.json()["task"]["reviewed"] is False

    # 2. Attempt completion with Idempotency-Key
    idem_key = "transient-key-001"
    err_resp = client.post(
        target_path,
        headers={"Idempotency-Key": idem_key},
    )
    assert err_resp.status_code == 500
    assert err_resp.json()["code"] == "INTERNAL_SERVER_ERROR"

    # CRITICAL INVARIANT: Verify NO side effect occurred!
    status_resp_after_fail = client.get("/api/tasks/task_01")
    assert status_resp_after_fail.json()["task"]["reviewed"] is False
    acc_resp = client.get("/api/accounts/test_01")
    assert acc_resp.json()["points"] == 120

    # 3. Safe retry with same Idempotency-Key: succeeds and awards points exactly once
    retry_resp = client.post(
        target_path,
        headers={"Idempotency-Key": idem_key},
    )
    assert retry_resp.status_code == 200
    data = retry_resp.json()
    assert data["reviewed"] is True
    assert data["points_awarded"] == 10
    assert data["total_points"] == 130
    assert data["idempotent_replay"] is False

    # 4. Third attempt (idempotent replay): 0 points awarded
    replay_resp = client.post(
        target_path,
        headers={"Idempotency-Key": idem_key},
    )
    assert replay_resp.status_code == 200
    assert replay_resp.json()["points_awarded"] == 0
    assert replay_resp.json()["idempotent_replay"] is True
    assert replay_resp.json()["total_points"] == 130


def test_transient_http_500_via_header(client: TestClient) -> None:
    """Scenario 2 (Header): Transient 500 via request header returning HTML or JSON."""
    resp_json = client.get("/api/health", headers={"X-Test-Fault-500": "1"})
    assert resp_json.status_code == 500
    assert resp_json.json()["code"] == "INTERNAL_SERVER_ERROR"

    resp_html = client.get(
        "/login",
        headers={"X-Test-Fault-500": "1", "Accept": "text/html"},
    )
    assert resp_html.status_code == 500
    assert "INTERNAL_SERVER_ERROR" in resp_html.text


def test_rate_limiting_429_with_retry_after(authenticated_client: tuple[TestClient, str]) -> None:
    """Scenario 3: HTTP 429 returns standard Retry-After header and recovers."""
    client, _ = authenticated_client

    fault_controller.set_rate_limit(path="/api/tasks", retry_after=3, count=1)

    # 1. First call throttled
    resp1 = client.get("/api/tasks")
    assert resp1.status_code == 429
    assert resp1.headers.get("Retry-After") == "3"
    data = resp1.json()
    assert data["code"] == "RATE_LIMITED"
    assert data["retry_after"] == 3

    # 2. Next call succeeds (count exhausted)
    resp2 = client.get("/api/tasks")
    assert resp2.status_code == 200
    assert "Retry-After" not in resp2.headers


def test_rate_limiting_429_via_header(authenticated_client: tuple[TestClient, str]) -> None:
    """Scenario 3 (Header): HTTP 429 via request header."""
    client, _ = authenticated_client

    resp = client.get("/api/tasks", headers={"X-Test-Fault-429": "5"})
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "5"
    assert resp.json()["retry_after"] == 5


def test_expired_session_behavior(client: TestClient) -> None:
    """Scenario 4: Expired session invalidates access without affecting other accounts."""
    # Login test_01
    resp1 = client.post(
        "/login",
        data={"username": "test_01", "password": "test_password_01"},
        follow_redirects=False,
    )
    cookie1 = resp1.cookies.get("rewardlab_session")
    assert cookie1 is not None

    # Login test_02
    resp2 = client.post(
        "/login",
        data={"username": "test_02", "password": "test_password_02"},
        follow_redirects=False,
    )
    cookie2 = resp2.cookies.get("rewardlab_session")
    assert cookie2 is not None

    # Clear cookie jar so we test explicit Authorization headers
    client.cookies.clear()
    auth1 = {"Authorization": f"Bearer {cookie1}"}
    auth2 = {"Authorization": f"Bearer {cookie2}"}

    assert client.get("/api/tasks", headers=auth1).status_code == 200
    assert client.get("/api/tasks", headers=auth2).status_code == 200

    # Expire test_01's session
    fault_controller.expire_session(cookie1)

    # test_01 API call fails with 401
    fail_api = client.get("/api/tasks", headers=auth1)
    assert fail_api.status_code == 401

    # test_01 UI call redirects to /login (302)
    client.cookies.set("rewardlab_session", cookie1)
    fail_ui = client.get("/dashboard", follow_redirects=False)
    assert fail_ui.status_code == 302
    assert "/login" in fail_ui.headers["location"]
    client.cookies.clear()

    # test_02 remains completely unaffected!
    test_02_api = client.get("/api/tasks", headers=auth2)
    assert test_02_api.status_code == 200
    assert test_02_api.json()["username"] == "test_02"

    # test_01 re-login works and gets a new functional session
    new_login = client.post(
        "/login",
        data={"username": "test_01", "password": "test_password_01"},
        follow_redirects=False,
    )
    new_cookie = new_login.cookies.get("rewardlab_session")
    new_auth = {"Authorization": f"Bearer {new_cookie}"}
    assert client.get("/api/tasks", headers=new_auth).status_code == 200


def test_browser_navigation_failure_and_recovery(
    authenticated_client: tuple[TestClient, str],
) -> None:
    """Scenario 5: Navigation failure returns 503 on target page, leaves others unharmed."""
    client, _ = authenticated_client

    fault_controller.set_navigation_fault(path="/dashboard", status_code=503, count=1)

    # First access to /dashboard fails with 503
    fail_resp = client.get("/dashboard", headers={"Accept": "text/html"})
    assert fail_resp.status_code == 503
    assert "NAVIGATION_FAILED" in fail_resp.text

    # Access to another page works normally
    tasks_resp = client.get("/tasks")
    assert tasks_resp.status_code == 200

    # Subsequent access to /dashboard recovers (rule exhausted)
    recovered_resp = client.get("/dashboard")
    assert recovered_resp.status_code == 200
    assert "Synthetic Test Account 01" in recovered_resp.text


def test_browser_navigation_failure_via_header(client: TestClient) -> None:
    """Scenario 5 (Header): Request-scoped navigation failure."""
    resp = client.get("/api/health", headers={"X-Test-Fault-Navigation": "504"})
    assert resp.status_code == 504
    assert resp.json()["code"] == "NAVIGATION_FAILED"


def test_changed_button_label_ui_variant(
    authenticated_client: tuple[TestClient, str],
) -> None:
    """Scenario 6: Button label changed to 'Confirm Review' while maintaining functionality."""
    client, _ = authenticated_client

    # Default rendering
    default_resp = client.get("/tasks/task_01")
    assert "Mark as reviewed" in default_resp.text
    assert "Confirm Review" not in default_resp.text

    # Set UI variant
    fault_controller.set_ui_variant("button_label", "Confirm Review")

    variant_resp = client.get("/tasks/task_01")
    assert "Confirm Review" in variant_resp.text
    assert 'data-testid="mark-reviewed-btn"' in variant_resp.text
    assert 'aria-label="Confirm Review"' in variant_resp.text

    # Submitting the review still works and increments points
    review_resp = client.post("/tasks/task_01/review", follow_redirects=False)
    assert review_resp.status_code == 200
    assert "Task reviewed successfully" in review_resp.text
    assert "Confirm Review" in review_resp.text

    acc_resp = client.get("/api/accounts/test_01")
    assert acc_resp.json()["points"] == 130


def test_test_mode_protection_and_endpoints(client: TestClient) -> None:
    """Security: /_test/faults endpoints are accessible in test mode, denied (403) when disabled."""
    # 1. In test mode: configure, state and reset endpoints respond 200
    state_resp = client.get("/_test/faults/state")
    assert state_resp.status_code == 200
    assert state_resp.json()["test_mode"] is True

    cfg_resp = client.post(
        "/_test/faults/configure",
        json={"fault_type": "rate_limit", "path": "/api/tasks", "retry_after": 4, "count": 1},
    )
    assert cfg_resp.status_code == 200
    assert "/api/tasks" in cfg_resp.json()["state"]["rate_limits"]

    reset_resp = client.post("/_test/faults/reset")
    assert reset_resp.status_code == 200

    # 2. Disable test mode
    fault_controller.set_test_mode(False)

    try:
        # Endpoints must return 403 Forbidden
        assert client.get("/_test/faults/state").status_code == 403
        assert (
            client.post(
                "/_test/faults/configure",
                json={"fault_type": "rate_limit", "path": "/api/tasks"},
            ).status_code
            == 403
        )
        assert client.post("/_test/faults/reset").status_code == 403

        # Header fault injection must be ignored when test mode is False
        normal_resp = client.get("/api/health", headers={"X-Test-Fault-500": "1"})
        assert normal_resp.status_code == 200
        assert normal_resp.json()["status"] == "ok"
    finally:
        # Re-enable test mode for other tests
        fault_controller.set_test_mode(None)
