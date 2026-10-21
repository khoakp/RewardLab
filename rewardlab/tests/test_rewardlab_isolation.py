from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from data import SyntheticTask, set_test_mode_override, store


def test_12_synthetic_accounts_exist_and_unique() -> None:
    assert len(store.users) == 12

    expected_usernames = [f"test_{i:02d}" for i in range(1, 13)]
    assert sorted(store.users.keys()) == expected_usernames

    # Ensure uniqueness of usernames, passwords, and display names
    usernames = [u.username for u in store.users.values()]
    passwords = [u.password for u in store.users.values()]
    display_names = [u.display_name for u in store.users.values()]

    assert len(set(usernames)) == 12
    assert len(set(passwords)) == 12
    assert len(set(display_names)) == 12

    # Verify each account has tasks seeded
    for username in expected_usernames:
        tasks = store.get_tasks_for_user(username)
        assert len(tasks) >= 3
        task_ids = [t.id for t in tasks]
        assert "task_01" in task_ids
        assert "task_02" in task_ids
        assert "task_03" in task_ids


def test_cross_account_task_isolation_on_review(client: TestClient) -> None:
    # 1. Authenticate test_01
    login_res = client.post(
        "/login",
        data={"username": "test_01", "password": "test_password_01"},
        follow_redirects=False,
    )
    assert login_res.status_code == 303
    session_cookie = login_res.cookies["rewardlab_session"]
    client.cookies.set("rewardlab_session", session_cookie)

    # Initial state checks
    assert store.users["test_01"].points == 120
    assert store.users["test_02"].points == 50
    assert store.get_task_for_user("test_01", "task_01").reviewed is False
    assert store.get_task_for_user("test_02", "task_01").reviewed is False

    # 2. test_01 reviews task_01
    review_res = client.post("/tasks/task_01/review")
    assert review_res.status_code == 200

    # 3. Verify test_01 updated
    assert store.users["test_01"].points == 130
    assert store.get_task_for_user("test_01", "task_01").reviewed is True

    # 4. Verify test_02 and ALL other accounts remain completely unchanged
    assert store.users["test_02"].points == 50
    assert store.get_task_for_user("test_02", "task_01").reviewed is False

    for i in range(2, 13):
        acc = f"test_{i:02d}"
        task = store.get_task_for_user(acc, "task_01")
        assert task.reviewed is False, f"Account {acc} task_01 was modified unexpectedly!"


def test_cross_account_task_isolation_via_api(client: TestClient) -> None:
    # Mark task_01 on test_01 directly
    store.mark_task_reviewed("test_01", "task_01")

    # Inspect test_01 via API
    res_01 = client.get("/api/accounts/test_01/tasks/task_01")
    assert res_01.status_code == 200
    data_01 = res_01.json()
    assert data_01["reviewed"] is True
    assert data_01["total_points"] == 130

    # Inspect test_02 via API
    res_02 = client.get("/api/accounts/test_02/tasks/task_01")
    assert res_02.status_code == 200
    data_02 = res_02.json()
    assert data_02["reviewed"] is False
    assert data_02["total_points"] == 50

    # Inspect test_03 via API
    res_03 = client.get("/api/accounts/test_03/tasks/task_01")
    assert res_03.status_code == 200
    data_03 = res_03.json()
    assert data_03["reviewed"] is False
    assert data_03["total_points"] == 80


def test_cross_account_read_protection(client: TestClient) -> None:
    # Inject a private task unique to test_02
    store.user_tasks["test_02"].append(
        SyntheticTask(
            id="task_private_02",
            title="Private Task for Account 02",
            description="Confidential synthetic task.",
            reward_points=50,
            reviewed=False,
        )
    )

    # Login as test_01
    login_res = client.post(
        "/login",
        data={"username": "test_01", "password": "test_password_01"},
        follow_redirects=False,
    )
    client.cookies.set("rewardlab_session", login_res.cookies["rewardlab_session"])

    # Attempt to read test_02's private task
    res = client.get("/tasks/task_private_02")
    assert res.status_code == 404
    assert 'data-testid="not-found-message"' in res.text


def test_cross_account_write_protection(client: TestClient) -> None:
    # Inject a private task unique to test_02
    store.user_tasks["test_02"].append(
        SyntheticTask(
            id="task_private_02",
            title="Private Task for Account 02",
            description="Confidential synthetic task.",
            reward_points=50,
            reviewed=False,
        )
    )

    # Login as test_01
    login_res = client.post(
        "/login",
        data={"username": "test_01", "password": "test_password_01"},
        follow_redirects=False,
    )
    client.cookies.set("rewardlab_session", login_res.cookies["rewardlab_session"])

    # Attempt to mark test_02's private task reviewed from test_01's session
    res = client.post("/tasks/task_private_02/review")
    assert res.status_code == 404

    # Verify test_02 task was NOT modified
    task = store.get_task_for_user("test_02", "task_private_02")
    assert task is not None
    assert task.reviewed is False
    assert store.users["test_02"].points == 50


def test_concurrent_review_requests_idempotent() -> None:
    # Test concurrency at store level using ThreadPoolExecutor
    assert store.users["test_01"].points == 120
    assert store.get_task_for_user("test_01", "task_01").reviewed is False

    num_concurrent_workers = 10

    def attempt_review() -> tuple[bool, str, dict]:
        return store.mark_task_reviewed("test_01", "task_01")

    with ThreadPoolExecutor(max_workers=num_concurrent_workers) as executor:
        futures = [executor.submit(attempt_review) for _ in range(num_concurrent_workers)]
        results = [f.result() for f in futures]

    # Exactly one request should award points, the others return idempotent review
    points_awarded_list = [r[2].get("points_awarded", 0) for r in results]
    assert points_awarded_list.count(10) == 1
    assert points_awarded_list.count(0) == num_concurrent_workers - 1

    # Final balance must be exactly 130
    assert store.users["test_01"].points == 130
    assert store.get_task_for_user("test_01", "task_01").reviewed is True


def test_deterministic_reset() -> None:
    # Mutate multiple accounts
    store.users["test_01"].points = 9999
    store.mark_task_reviewed("test_02", "task_01")
    store.sessions["fake_session"] = "test_03"

    # Reset
    store.reset()

    # Verify restoration to clean initial values
    assert store.users["test_01"].points == 120
    assert store.users["test_02"].points == 50
    assert store.get_task_for_user("test_02", "task_01").reviewed is False
    assert len(store.sessions) == 0


def test_all_12_accounts_can_login_and_access_dashboard(client: TestClient) -> None:
    for i in range(1, 13):
        acc = f"test_{i:02d}"
        pwd = f"test_password_{i:02d}"

        # Login
        login_res = client.post(
            "/login",
            data={"username": acc, "password": pwd},
            follow_redirects=False,
        )
        assert login_res.status_code == 303, f"Failed login for {acc}"
        session_cookie = login_res.cookies["rewardlab_session"]

        # View dashboard
        client.cookies.set("rewardlab_session", session_cookie)
        dash_res = client.get("/dashboard")
        assert dash_res.status_code == 200
        html = dash_res.text
        assert f"Synthetic Test Account {i:02d}" in html
        assert f"<code>{acc}</code>" in html

        # Clean session
        client.post("/logout", follow_redirects=False)
        client.cookies.clear()


def test_inspection_api_security_and_test_mode_protection(client: TestClient) -> None:
    """Security check: inspection endpoints are allowed in test mode,
    denied (403) outside test mode.
    """
    # 1. In test mode (default): inspection endpoints are accessible for test verification
    res_acc = client.get("/api/accounts/test_01")
    assert res_acc.status_code == 200
    assert res_acc.json()["username"] == "test_01"
    assert res_acc.json()["points"] == 120

    res_task = client.get("/api/accounts/test_01/tasks/task_01")
    assert res_task.status_code == 200
    assert res_task.json()["task_id"] == "task_01"

    # Non-existent account/task returns 404
    assert client.get("/api/accounts/non_existent_user").status_code == 404
    assert client.get("/api/accounts/test_01/tasks/non_existent_task").status_code == 404

    # 2. Outside test mode (disabled): inspection endpoints MUST return 403 Forbidden
    set_test_mode_override(False)
    try:
        denied_acc = client.get("/api/accounts/test_01")
        assert denied_acc.status_code == 403
        data_acc = denied_acc.json()
        assert data_acc["code"] == "FORBIDDEN"
        assert "disabled outside test mode" in data_acc["message"]

        denied_task = client.get("/api/accounts/test_01/tasks/task_01")
        assert denied_task.status_code == 403
        data_task = denied_task.json()
        assert data_task["code"] == "FORBIDDEN"
        assert "disabled outside test mode" in data_task["message"]
    finally:
        set_test_mode_override(None)

