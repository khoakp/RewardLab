from fastapi.testclient import TestClient

from faults import fault_controller


def test_all_12_synthetic_accounts_independent_sessions(client: TestClient) -> None:
    """Isolation: All 12 synthetic accounts can maintain simultaneous, distinct sessions."""
    expected_points = [120, 50, 80, 110, 95, 60, 130, 40, 70, 150, 85, 100]
    sessions: dict[str, str] = {}

    # 1. Log in all 12 accounts and collect session cookies
    for i in range(1, 13):
        acc = f"test_{i:02d}"
        pwd = f"test_password_{i:02d}"
        login_resp = client.post(
            "/login",
            data={"username": acc, "password": pwd},
            follow_redirects=False,
        )
        assert login_resp.status_code == 303
        cookie = login_resp.cookies.get("rewardlab_session")
        assert cookie is not None
        assert cookie not in sessions.values(), f"Duplicate session generated for {acc}"
        sessions[acc] = cookie

    client.cookies.clear()

    # 2. Query each account via Bearer token and verify isolated state
    for i in range(1, 13):
        acc = f"test_{i:02d}"
        auth_header = {"Authorization": f"Bearer {sessions[acc]}"}
        resp = client.get("/api/tasks", headers=auth_header)
        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == acc
        assert data["total_points"] == expected_points[i - 1]
        assert len(data["tasks"]) == 3
        reviewed_map = {t["id"]: t["reviewed"] for t in data["tasks"]}
        assert reviewed_map["task_01"] is False
        assert reviewed_map["task_02"] is False
        assert reviewed_map["task_03"] is True


def test_session_expiration_isolation_across_accounts(client: TestClient) -> None:
    """Isolation: Expiring one account's session does not affect any other account."""
    accounts = [f"test_{i:02d}" for i in range(1, 13)]
    sessions: dict[str, str] = {}

    for acc in accounts:
        idx = int(acc.split("_")[1])
        resp = client.post(
            "/login",
            data={"username": acc, "password": f"test_password_{idx:02d}"},
            follow_redirects=False,
        )
        sessions[acc] = resp.cookies["rewardlab_session"]

    client.cookies.clear()

    # Expire test_04's session explicitly
    target_acc = "test_04"
    fault_controller.expire_session(sessions[target_acc])

    # Verify test_04 is rejected
    target_auth = {"Authorization": f"Bearer {sessions[target_acc]}"}
    assert client.get("/api/tasks", headers=target_auth).status_code == 401

    client.cookies.set("rewardlab_session", sessions[target_acc])
    ui_resp = client.get("/dashboard", follow_redirects=False)
    assert ui_resp.status_code == 302
    assert "/login" in ui_resp.headers["location"]
    client.cookies.clear()

    # Verify ALL other 11 accounts are still 100% active and isolated
    for acc in accounts:
        if acc == target_acc:
            continue
        auth = {"Authorization": f"Bearer {sessions[acc]}"}
        resp = client.get("/api/tasks", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["username"] == acc

    # Re-authenticate test_04 and verify fresh session works
    relogin = client.post(
        "/login",
        data={"username": target_acc, "password": "test_password_04"},
        follow_redirects=False,
    )
    new_token = relogin.cookies["rewardlab_session"]
    relogin_auth = {"Authorization": f"Bearer {new_token}"}
    assert client.get("/api/tasks", headers=relogin_auth).status_code == 200


def test_task_state_and_point_balance_across_fault_scenarios(client: TestClient) -> None:
    """Isolation & Reliability: Check task states and points across fault scenarios."""
    # Log in test_02 and test_05
    login2 = client.post(
        "/login",
        data={"username": "test_02", "password": "test_password_02"},
        follow_redirects=False,
    )
    auth2 = {"Authorization": f"Bearer {login2.cookies['rewardlab_session']}"}

    login5 = client.post(
        "/login",
        data={"username": "test_05", "password": "test_password_05"},
        follow_redirects=False,
    )
    auth5 = {"Authorization": f"Bearer {login5.cookies['rewardlab_session']}"}

    client.cookies.clear()

    # Initial points: test_02 = 50, test_05 = 95
    assert client.get("/api/tasks", headers=auth2).json()["total_points"] == 50
    assert client.get("/api/tasks", headers=auth5).json()["total_points"] == 95

    # 1. Fault Scenario: Transient 500 on test_02 completion
    target_path = "/api/tasks/task_01/complete"
    fault_controller.set_transient_error(path=target_path, status_code=500, count=1)

    fail_resp = client.post(
        target_path,
        headers={**auth2, "Idempotency-Key": "retry-key-test02"},
    )
    assert fail_resp.status_code == 500

    # Verify test_02 state unchanged
    status2 = client.get("/api/tasks/task_01", headers=auth2).json()
    assert status2["task"]["reviewed"] is False
    assert client.get("/api/tasks", headers=auth2).json()["total_points"] == 50

    # Verify test_05 is completely untouched
    assert client.get("/api/tasks", headers=auth5).json()["total_points"] == 95

    # 2. Retry test_02 with same idempotency key -> succeeds and awards points
    retry_resp = client.post(
        target_path,
        headers={**auth2, "Idempotency-Key": "retry-key-test02"},
    )
    assert retry_resp.status_code == 200
    assert retry_resp.json()["points_awarded"] == 10
    assert retry_resp.json()["total_points"] == 60

    # 3. Fault Scenario: Rate limit 429 on test_05
    fault_controller.set_rate_limit(path="/api/tasks", retry_after=2, count=1)
    rate_resp = client.get("/api/tasks", headers=auth5)
    assert rate_resp.status_code == 429
    assert rate_resp.headers.get("Retry-After") == "2"

    # test_02 can still access tasks normally
    assert client.get("/api/tasks", headers=auth2).status_code == 200

    # test_05 recovers after fault count exhausted
    recovered_5 = client.get("/api/tasks", headers=auth5)
    assert recovered_5.status_code == 200
    assert recovered_5.json()["total_points"] == 95
