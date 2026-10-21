import time

from fastapi.testclient import TestClient

from faults import fault_controller


def test_rate_limit_and_retry_after_lifecycle(client: TestClient) -> None:
    """Fault Lifecycle: Rate limit decrements count deterministically and exhausts."""
    target_path = "/api/health"
    fault_controller.set_rate_limit(path=target_path, retry_after=4, count=2)

    # Call 1: 429
    r1 = client.get(target_path)
    assert r1.status_code == 429
    assert r1.headers.get("Retry-After") == "4"

    # Call 2: 429
    r2 = client.get(target_path)
    assert r2.status_code == 429
    assert r2.headers.get("Retry-After") == "4"

    # Call 3: 200 OK (count exhausted)
    r3 = client.get(target_path)
    assert r3.status_code == 200
    assert "Retry-After" not in r3.headers


def test_transient_error_and_reset_lifecycle(client: TestClient) -> None:
    """Fault Lifecycle: Transient errors can be cleared immediately via reset."""
    target_path = "/api/health"
    fault_controller.set_transient_error(path=target_path, status_code=500, count=5)

    # First request fails
    assert client.get(target_path).status_code == 500

    # Call reset endpoint
    reset_resp = client.post("/_test/faults/reset")
    assert reset_resp.status_code == 200

    # Next request immediately succeeds
    assert client.get(target_path).status_code == 200


def test_delayed_response_lifecycle(client: TestClient) -> None:
    """Fault Lifecycle: Delay rule applies for configured count, then restores normal speed."""
    target_path = "/api/health"
    fault_controller.set_delay(path=target_path, seconds=0.15, count=1)

    t0 = time.perf_counter()
    r1 = client.get(target_path)
    t1 = time.perf_counter()
    assert r1.status_code == 200
    assert (t1 - t0) >= 0.12

    # Second call has no delay
    t2 = time.perf_counter()
    r2 = client.get(target_path)
    t3 = time.perf_counter()
    assert r2.status_code == 200
    assert (t3 - t2) < 0.10


def test_changed_button_label_lifecycle(authenticated_client: tuple[TestClient, str]) -> None:
    """Fault Lifecycle: Dynamic UI label variant can be set, verified and reset."""
    client, _ = authenticated_client

    # Default
    r_default = client.get("/tasks/task_01")
    assert "Mark as reviewed" in r_default.text
    assert "Proceed with Audit" not in r_default.text

    # Set UI variant via control endpoint
    cfg = client.post(
        "/_test/faults/configure",
        json={
            "fault_type": "ui_variant",
            "variant_key": "button_label",
            "variant_value": "Proceed with Audit",
        },
    )
    assert cfg.status_code == 200

    r_variant = client.get("/tasks/task_01")
    assert "Proceed with Audit" in r_variant.text
    assert 'aria-label="Proceed with Audit"' in r_variant.text

    # Reset
    client.post("/_test/faults/reset")
    r_restored = client.get("/tasks/task_01")
    assert "Mark as reviewed" in r_restored.text
    assert "Proceed with Audit" not in r_restored.text


def test_fault_counts_and_order_independence(client: TestClient) -> None:
    """Fault Lifecycle: Multiple rules operate independently without ordering dependencies."""
    fault_controller.set_transient_error(path="/login", status_code=500, count=1)
    fault_controller.set_rate_limit(path="/api/health", retry_after=2, count=1)
    fault_controller.set_navigation_fault(path="/dashboard", status_code=503, count=1)

    # Interleaved execution
    # 1. /api/health hits 429
    assert client.get("/api/health").status_code == 429
    # 2. /dashboard hits 503
    assert client.get("/dashboard").status_code == 503
    # 3. /login hits 500
    assert client.get("/login").status_code == 500

    # All counts now exhausted: subsequent calls return normal responses
    assert client.get("/api/health").status_code == 200
    assert client.get("/login").status_code == 200
    # /dashboard redirects unauthenticated user to /login (302)
    assert client.get("/dashboard", follow_redirects=False).status_code == 302
