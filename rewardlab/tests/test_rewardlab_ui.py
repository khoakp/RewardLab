from fastapi.testclient import TestClient

from data import store


def test_api_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "rewardlab"


def test_unauthenticated_redirects(client: TestClient) -> None:
    # Root redirects to /login
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/login"

    # Protected routes redirect to /login
    for path in ["/dashboard", "/tasks", "/tasks/task_01"]:
        res = client.get(path, follow_redirects=False)
        assert res.status_code == 302
        assert res.headers["location"] == "/login"

    # Action redirects to /login
    res = client.post("/tasks/task_01/review", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/login"


def test_login_page_renders_form_and_data_testids(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    html = response.text
    assert 'data-testid="synthetic-notice"' in html
    assert 'data-testid="username-input"' in html
    assert 'data-testid="password-input"' in html
    assert 'data-testid="login-submit-btn"' in html


def test_login_failure_invalid_credentials(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "test_01", "password": "wrong_password"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert 'data-testid="login-error-alert"' in response.text
    assert "Invalid username or password" in response.text


def test_login_success_and_cookie_assignment(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "test_01", "password": "test_password_01"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert "rewardlab_session" in response.cookies


def test_dashboard_page(authenticated_client: tuple[TestClient, str]) -> None:
    client, _ = authenticated_client
    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.text
    assert 'data-testid="dashboard-header"' in html
    assert 'data-testid="user-label"' in html
    assert "Synthetic Test Account 01" in html
    assert 'data-testid="points-balance"' in html
    assert "120 pts" in html
    assert 'data-testid="tasks-reviewed-count"' in html
    assert "1 / 3 Reviewed" in html


def test_tasks_listing_page(authenticated_client: tuple[TestClient, str]) -> None:
    client, _ = authenticated_client
    response = client.get("/tasks")
    assert response.status_code == 200
    html = response.text
    assert 'data-testid="tasks-list"' in html
    assert 'data-testid="task-item-task_01"' in html
    assert 'data-testid="task-item-task_02"' in html
    assert 'data-testid="task-description"' in html
    assert 'data-testid="task-status-task_01"' in html
    assert "Pending" in html
    assert 'data-testid="task-detail-link-task_01"' in html


def test_task_detail_page(authenticated_client: tuple[TestClient, str]) -> None:
    client, _ = authenticated_client
    response = client.get("/tasks/task_01")
    assert response.status_code == 200
    html = response.text
    assert 'data-testid="task-title"' in html
    assert "Review Daily Research Memo" in html
    assert 'data-testid="task-description"' in html
    assert 'data-testid="task-reward"' in html
    assert "+10 pts" in html
    assert 'data-testid="task-status"' in html
    assert 'data-testid="mark-reviewed-btn"' in html
    assert 'aria-label="Mark as reviewed"' in html


def test_task_not_found_returns_404(authenticated_client: tuple[TestClient, str]) -> None:
    client, _ = authenticated_client
    response = client.get("/tasks/non_existent_task_id")
    assert response.status_code == 404
    html = response.text
    assert 'data-testid="not-found-message"' in html
    assert 'data-testid="return-tasks-btn"' in html


def test_mark_task_reviewed_flow(authenticated_client: tuple[TestClient, str]) -> None:
    client, _ = authenticated_client

    # Pre-condition: user has 120 points, task_01 is pending
    user_before = store.users["test_01"]
    assert user_before.points == 120
    task_before = store.get_task_for_user("test_01", "task_01")
    assert task_before is not None
    assert task_before.reviewed is False

    # Execute review action
    response = client.post("/tasks/task_01/review")
    assert response.status_code == 200
    html = response.text

    # Verify UI updates
    assert 'data-testid="review-success-alert"' in html
    assert "+10 fake points awarded" in html
    assert 'data-testid="task-reviewed-note"' in html
    assert "disabled" in html  # button is now disabled

    # Verify store updates
    user_after = store.users["test_01"]
    assert user_after.points == 130
    task_after = store.get_task_for_user("test_01", "task_01")
    assert task_after is not None
    assert task_after.reviewed is True


def test_mark_task_reviewed_idempotent(authenticated_client: tuple[TestClient, str]) -> None:
    client, _ = authenticated_client

    # First review
    client.post("/tasks/task_01/review")
    assert store.users["test_01"].points == 130

    # Second review attempt (simulating duplicate click or replay)
    response = client.post("/tasks/task_01/review")
    assert response.status_code == 200
    assert "Task already marked as reviewed" in response.text

    # Points must remain 130, no double reward
    assert store.users["test_01"].points == 130


def test_logout_clears_session(authenticated_client: tuple[TestClient, str]) -> None:
    client, session_cookie = authenticated_client

    # Confirm active session in store
    assert session_cookie in store.sessions

    # Execute logout
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    # Confirm session revoked in store
    assert session_cookie not in store.sessions

    # Subsequent access redirects to login
    client.cookies.clear()
    res = client.get("/dashboard", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/login"
