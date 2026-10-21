import socket
import threading
import time
from collections.abc import Generator
from contextlib import closing

import httpx
import pytest
import uvicorn

from app import app
from faults import fault_controller


def get_free_port() -> int:
    """Find an available port on 127.0.0.1."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def live_server_url() -> Generator[str, None, None]:
    """Spawn an actual Uvicorn server on loopback in a background thread."""
    port = get_free_port()
    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"

    # Poll until live server is responding over real TCP socket
    started = False
    for _ in range(50):
        try:
            with httpx.Client(base_url=base_url, timeout=1.0) as client:
                r = client.get("/api/health")
                if r.status_code == 200:
                    started = True
                    break
        except Exception:
            time.sleep(0.05)

    if not started:
        server.should_exit = True
        raise RuntimeError("Live Uvicorn server failed to start within timeout")

    yield base_url

    server.should_exit = True
    thread.join(timeout=3.0)


def test_live_http_health(live_server_url: str) -> None:
    """Live HTTP: Send real HTTP request to loopback TCP socket."""
    with httpx.Client(base_url=live_server_url) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "rewardlab"


def test_live_http_login_and_task_lifecycle(live_server_url: str) -> None:
    """Live HTTP: Full login, session cookie exchange, and dashboard access over TCP."""
    with httpx.Client(base_url=live_server_url, follow_redirects=False) as client:
        # 1. Login with synthetic account
        login_resp = client.post(
            "/login",
            data={"username": "test_01", "password": "test_password_01"},
        )
        assert login_resp.status_code == 303
        cookie = login_resp.cookies.get("rewardlab_session")
        assert cookie is not None

        # 2. Access dashboard with real cookie header
        client.cookies.set("rewardlab_session", cookie)
        dash_resp = client.get("/dashboard")
        assert dash_resp.status_code == 200
        assert "Synthetic Test Account 01" in dash_resp.text

        # 3. Complete task via authenticated API
        auth_headers = {
            "Authorization": f"Bearer {cookie}",
            "Idempotency-Key": "live-http-idem-01",
        }
        complete_resp = client.post("/api/tasks/task_01/complete", headers=auth_headers)
        assert complete_resp.status_code == 200
        d1 = complete_resp.json()
        assert d1["reviewed"] is True
        assert d1["points_awarded"] == 10
        assert d1["idempotent_replay"] is False

        # 4. Retry completion over live TCP -> verify idempotent replay
        replay_resp = client.post("/api/tasks/task_01/complete", headers=auth_headers)
        assert replay_resp.status_code == 200
        d2 = replay_resp.json()
        assert d2["reviewed"] is True
        assert d2["points_awarded"] == 0
        assert d2["idempotent_replay"] is True


def test_live_http_fault_injection_and_recovery(live_server_url: str) -> None:
    """Live HTTP: Verify rate limiting (429 + Retry-After) and transient faults over TCP."""
    with httpx.Client(base_url=live_server_url) as client:
        fault_controller.set_test_mode(True)

        # 1. Test header-based rate limit 429
        rate_resp = client.get("/api/health", headers={"X-Test-Fault-429": "3"})
        assert rate_resp.status_code == 429
        assert rate_resp.headers.get("Retry-After") == "3"

        # 2. Configure transient 500 via control endpoint over real HTTP
        cfg_resp = client.post(
            "/_test/faults/configure",
            json={
                "fault_type": "transient_error",
                "path": "/api/health",
                "status_code": 500,
                "count": 1,
            },
        )
        assert cfg_resp.status_code == 200

        # 3. Next real HTTP call hits 500
        err_resp = client.get("/api/health")
        assert err_resp.status_code == 500

        # 4. Count exhausted -> subsequent real HTTP call recovers
        recovered_resp = client.get("/api/health")
        assert recovered_resp.status_code == 200


def test_live_http_security_and_remote_defense(live_server_url: str) -> None:
    """Live HTTP: Verify defense-in-depth loopback check rejects forwarded remote clients."""
    with httpx.Client(base_url=live_server_url) as client:
        fault_controller.set_test_mode(True)

        # Simulated remote forwarded client
        remote_headers = {"X-Forwarded-For": "203.0.113.88"}

        # Real TCP connection with remote XFF header must receive 403 Forbidden
        assert client.get("/_test/faults/state", headers=remote_headers).status_code == 403
        assert client.post("/_test/faults/reset", headers=remote_headers).status_code == 403
        assert client.get("/api/accounts/test_01", headers=remote_headers).status_code == 403
