from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from faults import fault_controller


def test_repeated_completion_requests_same_key(
    authenticated_client: tuple[TestClient, str],
) -> None:
    """Idempotency: Repeated completions with identical key award points exactly once."""
    client, _ = authenticated_client
    target_path = "/api/tasks/task_01/complete"
    key = "idem-repeat-test-001"

    # 1. First execution awards points
    r1 = client.post(target_path, headers={"Idempotency-Key": key})
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["reviewed"] is True
    assert d1["points_awarded"] == 10
    assert d1["total_points"] == 130
    assert d1["idempotent_replay"] is False

    # 2. Second execution returns idempotent replay (0 points awarded)
    r2 = client.post(target_path, headers={"Idempotency-Key": key})
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["reviewed"] is True
    assert d2["points_awarded"] == 0
    assert d2["total_points"] == 130
    assert d2["idempotent_replay"] is True

    # 3. Third execution remains stable
    r3 = client.post(target_path, headers={"Idempotency-Key": key})
    assert r3.status_code == 200
    assert r3.json()["points_awarded"] == 0
    assert r3.json()["total_points"] == 130


def test_concurrent_completion_requests(authenticated_client: tuple[TestClient, str]) -> None:
    """Idempotency: Concurrent requests with identical key produce exactly one state mutation."""
    client, _ = authenticated_client
    target_path = "/api/tasks/task_01/complete"
    key = "concurrent-idempotency-key-002"

    def send_complete() -> tuple[int, dict]:
        resp = client.post(target_path, headers={"Idempotency-Key": key})
        return resp.status_code, resp.json()

    # Dispatch 10 parallel requests
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda _: send_complete(), range(10)))

    # All 10 requests must return HTTP 200
    assert all(status == 200 for status, _ in results)

    # Exactly 1 request must have awarded points, 9 must be replays
    awarded_count = sum(1 for _, data in results if data["points_awarded"] == 10)
    replayed_count = sum(1 for _, data in results if data["idempotent_replay"] is True)

    assert awarded_count == 1, f"Expected 1 mutation, got {awarded_count}"
    assert replayed_count == 9, f"Expected 9 replays, got {replayed_count}"

    # Verify final account state: initial 120 + 10 = 130
    acc_resp = client.get("/api/tasks")
    assert acc_resp.json()["total_points"] == 130


def test_idempotency_key_collision_across_different_tasks(
    authenticated_client: tuple[TestClient, str],
) -> None:
    """Idempotency: Reusing an idempotency key across different tasks triggers 409 Conflict."""
    client, _ = authenticated_client
    shared_key = "collision-key-xyz"

    # Task 01 completes with shared_key
    r1 = client.post("/api/tasks/task_01/complete", headers={"Idempotency-Key": shared_key})
    assert r1.status_code == 200

    # Task 02 attempts to use shared_key for same account
    r2 = client.post("/api/tasks/task_02/complete", headers={"Idempotency-Key": shared_key})
    assert r2.status_code == 409
    d2 = r2.json()
    assert d2["code"] == "IDEMPOTENCY_CONFLICT"
    assert d2["conflicting_task_id"] == "task_01"


def test_idempotency_key_isolation_between_accounts(client: TestClient) -> None:
    """Idempotency: Identical idempotency keys used by different accounts do not collide."""
    # Login test_01 and test_02
    resp1 = client.post(
        "/login",
        data={"username": "test_01", "password": "test_password_01"},
        follow_redirects=False,
    )
    auth1 = {"Authorization": f"Bearer {resp1.cookies['rewardlab_session']}"}

    resp2 = client.post(
        "/login",
        data={"username": "test_02", "password": "test_password_02"},
        follow_redirects=False,
    )
    auth2 = {"Authorization": f"Bearer {resp2.cookies['rewardlab_session']}"}

    client.cookies.clear()
    key = "global-reused-key-123"

    # test_01 completes task_01 with key
    r1 = client.post(
        "/api/tasks/task_01/complete",
        headers={**auth1, "Idempotency-Key": key},
    )
    assert r1.status_code == 200
    assert r1.json()["username"] == "test_01"

    # test_02 completes task_01 with the SAME key -> succeeds because namespace is partitioned
    r2 = client.post(
        "/api/tasks/task_01/complete",
        headers={**auth2, "Idempotency-Key": key},
    )
    assert r2.status_code == 200
    assert r2.json()["username"] == "test_02"
    assert r2.json()["idempotent_replay"] is False


def test_transient_http_500_before_side_effect(
    authenticated_client: tuple[TestClient, str],
) -> None:
    """Retry Safety: HTTP 500 intercepted before business logic prevents any side-effect."""
    client, _ = authenticated_client
    target_path = "/api/tasks/task_01/complete"
    idem_key = "transient-key-task-01"

    # Inject transient 500
    fault_controller.set_transient_error(path=target_path, status_code=500, count=1)

    fail_resp = client.post(target_path, headers={"Idempotency-Key": idem_key})
    assert fail_resp.status_code == 500

    # Verify task state is unchanged (task_01 remains unreviewed)
    task_resp = client.get("/api/tasks/task_01")
    assert task_resp.json()["task"]["reviewed"] is False

    # Retry completes successfully and commits exactly once
    retry_resp = client.post(target_path, headers={"Idempotency-Key": idem_key})
    assert retry_resp.status_code == 200
    assert retry_resp.json()["reviewed"] is True
    assert retry_resp.json()["points_awarded"] == 10


def test_lost_response_simulation_and_at_least_once_retry(
    authenticated_client: tuple[TestClient, str],
) -> None:
    """Retry Safety: Simulate lost response after server commit; retry returns cached state.

    Proves that at-least-once network delivery with idempotency keys achieves
    side-effect safety without unproven 'exactly-once network' claims.
    """
    client, _ = authenticated_client
    target_path = "/api/tasks/task_02/complete"
    idem_key = "lost-response-scenario-key"

    # Step 1: Client sends request; server commits side-effect (task_02 awards 15 pts)
    r1 = client.post(target_path, headers={"Idempotency-Key": idem_key})
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["reviewed"] is True
    assert d1["points_awarded"] == 15
    total_after_first = d1["total_points"]

    # Step 2: Simulate client experiencing network failure / lost response
    # Client has no knowledge of whether request succeeded or failed.
    # Client reconnects and blindly retries with the SAME Idempotency-Key.
    r2 = client.post(target_path, headers={"Idempotency-Key": idem_key})
    assert r2.status_code == 200
    d2 = r2.json()

    # Step 3: Verify server recognized previous execution and returned cached replay
    assert d2["reviewed"] is True
    assert d2["points_awarded"] == 0, "Duplicate points must NOT be awarded on lost response retry"
    assert d2["idempotent_replay"] is True
    assert d2["total_points"] == total_after_first

    # Final verification: points balance strictly preserved
    final_resp = client.get("/api/tasks")
    assert final_resp.json()["total_points"] == total_after_first
