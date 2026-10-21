from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from data import SyntheticTask, store


def test_unauthenticated_api_requests_return_401(client: TestClient) -> None:
    for method, path in [
        ("GET", "/api/tasks"),
        ("GET", "/api/tasks/task_01"),
        ("POST", "/api/tasks/task_01/complete"),
    ]:
        res = client.request(method, path)
        assert res.status_code == 401
        data = res.json()
        assert data["code"] == "UNAUTHORIZED"
        assert "Authentication required" in data["message"]


def test_authenticated_task_status_via_cookie(client: TestClient) -> None:
    # Login as test_01
    login_res = client.post(
        "/login",
        data={"username": "test_01", "password": "test_password_01"},
        follow_redirects=False,
    )
    client.cookies.set("rewardlab_session", login_res.cookies["rewardlab_session"])

    # Query all tasks for user
    res_list = client.get("/api/tasks")
    assert res_list.status_code == 200
    data_list = res_list.json()
    assert data_list["username"] == "test_01"
    assert data_list["total_points"] == 120
    assert len(data_list["tasks"]) >= 3

    # Query single task
    res_single = client.get("/api/tasks/task_01")
    assert res_single.status_code == 200
    data_single = res_single.json()
    assert data_single["username"] == "test_01"
    assert data_single["task"]["id"] == "task_01"
    assert data_single["task"]["reviewed"] is False


def test_authenticated_task_status_via_bearer_token(client: TestClient) -> None:
    # Authenticate test_02 to acquire a session_id
    session_id = store.authenticate("test_02", "test_password_02")
    assert session_id is not None

    # Query using Authorization header without cookies
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {session_id}"}

    res = client.get("/api/tasks", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["username"] == "test_02"
    assert data["total_points"] == 50


def test_api_task_status_not_found(client: TestClient) -> None:
    session_id = store.authenticate("test_01", "test_password_01")
    headers = {"Authorization": f"Bearer {session_id}"}

    res = client.get("/api/tasks/non_existent_task_id", headers=headers)
    assert res.status_code == 404
    data = res.json()
    assert data["code"] == "TASK_NOT_FOUND"


def test_api_task_completion_idempotency_key_flow(client: TestClient) -> None:
    session_id = store.authenticate("test_01", "test_password_01")
    headers = {
        "Authorization": f"Bearer {session_id}",
        "Idempotency-Key": "req-idempotency-token-12345",
    }

    # 1. First completion call
    res_1 = client.post("/api/tasks/task_01/complete", headers=headers)
    assert res_1.status_code == 200
    data_1 = res_1.json()
    assert data_1["username"] == "test_01"
    assert data_1["task_id"] == "task_01"
    assert data_1["reviewed"] is True
    assert data_1["points_awarded"] == 10
    assert data_1["total_points"] == 130
    assert data_1["idempotent_replay"] is False
    assert data_1["idempotency_key"] == "req-idempotency-token-12345"

    # 2. Replay with the same Idempotency-Key header
    res_2 = client.post("/api/tasks/task_01/complete", headers=headers)
    assert res_2.status_code == 200
    data_2 = res_2.json()
    assert data_2["reviewed"] is True
    assert data_2["points_awarded"] == 0
    assert data_2["idempotent_replay"] is True
    assert data_2["total_points"] == 130

    # 3. Third call with different key or no key
    res_3 = client.post(
        "/api/tasks/task_01/complete",
        headers={"Authorization": f"Bearer {session_id}"},
    )
    assert res_3.status_code == 200
    data_3 = res_3.json()
    assert data_3["reviewed"] is True
    assert data_3["points_awarded"] == 0
    assert data_3["total_points"] == 130
    assert data_3["idempotent_replay"] is True

    # Confirm user total points are strictly 130 (awarded once)
    assert store.users["test_01"].points == 130


def test_api_cross_account_isolation(client: TestClient) -> None:
    # 1. test_01 completes task_01
    s1 = store.authenticate("test_01", "test_password_01")
    res_1 = client.post("/api/tasks/task_01/complete", headers={"Authorization": f"Bearer {s1}"})
    assert res_1.status_code == 200

    # 2. test_02 checks task_01
    s2 = store.authenticate("test_02", "test_password_02")
    res_2 = client.get("/api/tasks/task_01", headers={"Authorization": f"Bearer {s2}"})
    assert res_2.status_code == 200
    data_2 = res_2.json()
    assert data_2["task"]["reviewed"] is False
    assert data_2["username"] == "test_02"
    assert store.users["test_02"].points == 50

    # 3. Inject private task for test_02 only
    store.user_tasks["test_02"].append(
        SyntheticTask(
            id="task_private_02",
            title="Private Task 02",
            description="Private task description.",
            reward_points=30,
            reviewed=False,
        )
    )

    # 4. test_01 attempts to read test_02's private task
    res_read = client.get("/api/tasks/task_private_02", headers={"Authorization": f"Bearer {s1}"})
    assert res_read.status_code == 404
    assert res_read.json()["code"] == "TASK_NOT_FOUND"

    # 5. test_01 attempts to complete test_02's private task
    res_write = client.post(
        "/api/tasks/task_private_02/complete", headers={"Authorization": f"Bearer {s1}"}
    )
    assert res_write.status_code == 404
    assert res_write.json()["code"] == "TASK_NOT_FOUND"

    # Verify test_02 task was NOT modified
    task = store.get_task_for_user("test_02", "task_private_02")
    assert task is not None
    assert task.reviewed is False


def test_concurrent_api_completions(client: TestClient) -> None:
    session_id = store.authenticate("test_01", "test_password_01")
    assert store.users["test_01"].points == 120

    num_threads = 10

    def make_completion_request(idx: int) -> int:
        headers = {
            "Authorization": f"Bearer {session_id}",
            "Idempotency-Key": f"concurrent-req-token-{idx}",
        }
        res = client.post("/api/tasks/task_01/complete", headers=headers)
        return res.status_code

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        statuses = list(executor.map(make_completion_request, range(num_threads)))

    assert all(status == 200 for status in statuses)

    # Exactly one award of 10 points
    assert store.users["test_01"].points == 130
    assert store.get_task_for_user("test_01", "task_01").reviewed is True


def test_api_idempotency_key_collision_across_different_tasks(client: TestClient) -> None:
    """Edge Case: Reusing the same Idempotency-Key for two different tasks
    returns HTTP 409 Conflict.
    """
    s1 = store.authenticate("test_01", "test_password_01")
    shared_key = "shared-idempotency-key-xyz"

    # 1. Complete task_01 with shared_key -> OK (200)
    res_1 = client.post(
        "/api/tasks/task_01/complete",
        headers={"Authorization": f"Bearer {s1}", "Idempotency-Key": shared_key},
    )
    assert res_1.status_code == 200
    data_1 = res_1.json()
    assert data_1["task_id"] == "task_01"
    assert data_1["points_awarded"] == 10
    assert data_1["total_points"] == 130
    assert data_1["idempotent_replay"] is False

    # 2. Replay for task_01 with same shared_key -> OK (200, idempotent replay)
    res_2 = client.post(
        "/api/tasks/task_01/complete",
        headers={"Authorization": f"Bearer {s1}", "Idempotency-Key": shared_key},
    )
    assert res_2.status_code == 200
    data_2 = res_2.json()
    assert data_2["task_id"] == "task_01"
    assert data_2["points_awarded"] == 0
    assert data_2["idempotent_replay"] is True

    # 3. Attempt to use shared_key for task_02 -> HTTP 409 Conflict
    res_3 = client.post(
        "/api/tasks/task_02/complete",
        headers={"Authorization": f"Bearer {s1}", "Idempotency-Key": shared_key},
    )
    assert res_3.status_code == 409
    data_3 = res_3.json()
    assert data_3["code"] == "IDEMPOTENCY_CONFLICT"
    assert data_3["conflicting_task_id"] == "task_01"
    assert "already used for task 'task_01'" in data_3["message"]

    # Verify task_02 was NOT modified and points were not added
    task_02 = store.get_task_for_user("test_01", "task_02")
    assert task_02 is not None
    assert task_02.reviewed is False
    assert store.users["test_01"].points == 130

    # 4. Another user (test_02) can use shared_key without conflict (scoped per user)
    s2 = store.authenticate("test_02", "test_password_02")
    res_user2 = client.post(
        "/api/tasks/task_01/complete",
        headers={"Authorization": f"Bearer {s2}", "Idempotency-Key": shared_key},
    )
    assert res_user2.status_code == 200
    assert res_user2.json()["username"] == "test_02"
    assert res_user2.json()["points_awarded"] == 10
