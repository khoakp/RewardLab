from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app import app
from data import store
from faults import fault_controller


@pytest.fixture(autouse=True)
def test_environment_setup() -> Generator[None, None, None]:
    """Ensure in-memory store and fault controller are reset and test mode is explicitly enabled."""
    store.reset()
    fault_controller.reset()
    fault_controller.set_test_mode(True)
    yield
    store.reset()
    fault_controller.reset()
    fault_controller.set_test_mode(None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def authenticated_client(client: TestClient) -> tuple[TestClient, str]:
    """Provides a TestClient already authenticated as test_01."""
    response = client.post(
        "/login",
        data={"username": "test_01", "password": "test_password_01"},
        follow_redirects=False,
    )
    session_cookie = response.cookies.get("rewardlab_session")
    assert session_cookie is not None
    client.cookies.set("rewardlab_session", session_cookie)
    return client, session_cookie
