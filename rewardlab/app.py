import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Header, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from data import is_test_mode, store
from faults import FaultConfigPayload, fault_controller

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="RewardLab",
    version="0.1.0",
    description="RewardLab — Owned Browser Automation Test Site (ADBAP)",
)

# Static assets and template engine
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

SESSION_COOKIE_NAME = "rewardlab_session"


class FaultInjectionMiddleware:
    """Pure ASGI middleware for deterministic fault injection.

    Avoids Starlette BaseHTTPMiddleware request stream deadlocks in test runners.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Do not inject faults on static assets or test control endpoints
        if path.startswith("/static") or path.startswith("/_test/faults"):
            await self.app(scope, receive, send)
            return

        headers: dict[str, str] = {}
        for raw_k, raw_v in scope.get("headers", []):
            with contextlib.suppress(Exception):
                headers[raw_k.decode("latin-1")] = raw_v.decode("latin-1")

        status_code, resp_headers, resp_body, delay_sec = await fault_controller.evaluate_request(
            path=path, headers=headers
        )

        if delay_sec > 0:
            await asyncio.sleep(delay_sec)

        if status_code is not None:
            accept = headers.get("accept", "")
            if "text/html" in accept and not path.startswith("/api/"):
                code = resp_body.get("code", "ERROR") if resp_body else "ERROR"
                msg = resp_body.get("message", "Fault") if resp_body else "Fault"
                content = (
                    f"<!DOCTYPE html><html><body><h1>{code}</h1><p>{msg}</p></body></html>"
                )
                response = HTMLResponse(
                    content=content, status_code=status_code, headers=resp_headers
                )
            else:
                response = JSONResponse(
                    status_code=status_code,
                    content=resp_body or {},
                    headers=resp_headers,
                )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


app.add_middleware(FaultInjectionMiddleware)


def get_current_user(request: Request) -> Any | None:
    """Retrieve authenticated user, giving explicit Authorization header precedence over cookie."""
    auth_header = request.headers.get("Authorization")
    session_id = None
    if auth_header and auth_header.startswith("Bearer "):
        session_id = auth_header[len("Bearer ") :].strip()
    if not session_id:
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
    return store.get_user_by_session(session_id)


def is_loopback_request(request: Request) -> bool:
    """Verify that request strictly originates from loopback and is not forwarded from remote."""
    client_host = request.client.host if request.client else None
    if not client_host:
        return False
    allowed_hosts = {"127.0.0.1", "::1", "localhost", "testclient"}
    if client_host not in allowed_hosts:
        return False
    # If X-Forwarded-For is present, ensure all forwarded IPs are loopback
    xff = request.headers.get("x-forwarded-for")
    if xff:
        for ip in [p.strip() for p in xff.split(",")]:
            if ip and ip not in allowed_hosts:
                return False
    return True


def get_button_label(request: Request) -> str:
    """Retrieve button label, honoring test variant only when test mode is active."""
    if fault_controller.is_test_mode():
        header_val = request.headers.get("x-test-fault-ui-button")
        if header_val:
            return header_val
        variant_val = fault_controller.get_ui_variant("button_label")
        if variant_val:
            return str(variant_val)
    return "Mark as reviewed"


class TaskCompletionPayload(BaseModel):
    idempotency_key: str | None = None


# =====================================================================
# API Endpoints
# =====================================================================


@app.get("/api/health", tags=["system"])
async def api_health() -> dict[str, str]:
    """Health check endpoint for RewardLab service."""
    return {
        "status": "ok",
        "service": "rewardlab",
        "version": "0.1.0",
    }


@app.get("/api/accounts/{username}", tags=["system"])
async def api_get_account(username: str, request: Request) -> Response:
    """Retrieve synthetic account status (test mode only, loopback only)."""
    if not is_test_mode() or not is_loopback_request(request):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "code": "FORBIDDEN",
                "message": "Inspection endpoints are disabled outside test mode or loopback",
            },
        )
    user = store.users.get(username)
    if not user:
        return JSONResponse(status_code=404, content={"message": "Account not found"})
    tasks = store.get_tasks_for_user(username)
    return JSONResponse(
        status_code=200,
        content={
            "username": user.username,
            "display_name": user.display_name,
            "points": user.points,
            "tasks_count": len(tasks),
            "reviewed_count": sum(1 for t in tasks if t.reviewed),
        },
    )


@app.get("/api/accounts/{username}/tasks/{task_id}", tags=["system"])
async def api_get_account_task(username: str, task_id: str, request: Request) -> Response:
    """Retrieve task state for a specific synthetic account (test mode only, loopback only)."""
    if not is_test_mode() or not is_loopback_request(request):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "code": "FORBIDDEN",
                "message": "Inspection endpoints are disabled outside test mode or loopback",
            },
        )
    user = store.users.get(username)
    if not user:
        return JSONResponse(status_code=404, content={"message": "Account not found"})
    task = store.get_task_for_user(username, task_id)
    if not task:
        return JSONResponse(status_code=404, content={"message": "Task not found"})
    return JSONResponse(
        status_code=200,
        content={
            "username": user.username,
            "task_id": task.id,
            "title": task.title,
            "reviewed": task.reviewed,
            "reward_points": task.reward_points,
            "total_points": user.points,
        },
    )


@app.get("/api/tasks", tags=["tasks_api"])
async def api_get_tasks(request: Request) -> Response:
    """Retrieve tasks for the authenticated user session."""
    user = get_current_user(request)
    if not user:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"code": "UNAUTHORIZED", "message": "Authentication required"},
        )
    tasks = store.get_tasks_for_user(user.username)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "username": user.username,
            "total_points": user.points,
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "reward_points": t.reward_points,
                    "reviewed": t.reviewed,
                }
                for t in tasks
            ],
        },
    )


@app.get("/api/tasks/{task_id}", tags=["tasks_api"])
async def api_get_single_task(request: Request, task_id: str) -> Response:
    """Retrieve task status for the authenticated user session."""
    user = get_current_user(request)
    if not user:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"code": "UNAUTHORIZED", "message": "Authentication required"},
        )
    task = store.get_task_for_user(user.username, task_id)
    if not task:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "code": "TASK_NOT_FOUND",
                "message": f"Task '{task_id}' not found for account '{user.username}'",
            },
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "username": user.username,
            "task": {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "reward_points": task.reward_points,
                "reviewed": task.reviewed,
            },
        },
    )


@app.post("/api/tasks/{task_id}/complete", tags=["tasks_api"])
async def api_complete_task(
    request: Request,
    task_id: str,
    payload: TaskCompletionPayload | None = None,
    idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    """Idempotently complete a task for the authenticated account."""
    user = get_current_user(request)
    if not user:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"code": "UNAUTHORIZED", "message": "Authentication required"},
        )

    # Prefer header, fallback to optional JSON payload
    idempotency_key = idempotency_key_header or (payload.idempotency_key if payload else None)

    success, msg, details = store.mark_task_reviewed(
        username=user.username,
        task_id=task_id,
        idempotency_key=idempotency_key,
    )
    if not success:
        if details.get("code") == "IDEMPOTENCY_CONFLICT":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "code": "IDEMPOTENCY_CONFLICT",
                    "message": msg,
                    "conflicting_task_id": details.get("conflicting_task_id"),
                },
            )
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "code": "TASK_NOT_FOUND",
                "message": f"Task '{task_id}' not found for account '{user.username}'",
            },
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "username": user.username,
            "task_id": details["task_id"],
            "reviewed": details["reviewed"],
            "points_awarded": details["points_awarded"],
            "total_points": details["total_points"],
            "idempotent_replay": details["idempotent_replay"],
            "idempotency_key": details["idempotency_key"],
            "message": msg,
        },
    )


# =====================================================================
# Web UI Endpoints
# =====================================================================


@app.get("/", response_class=HTMLResponse)
async def index_redirect(request: Request) -> Response:
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"user": None, "error": None},
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> Response:
    session_id = store.authenticate(username=username.strip(), password=password)
    if not session_id:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "user": None,
                "error": "Invalid username or password for synthetic test account.",
                "username": username,
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/logout", response_class=HTMLResponse)
async def logout(request: Request) -> Response:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    store.revoke_session(session_id)
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> Response:
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    tasks = store.get_tasks_for_user(user.username)
    reviewed_count = sum(1 for t in tasks if t.reviewed)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "tasks": tasks,
            "reviewed_count": reviewed_count,
            "total_count": len(tasks),
        },
    )


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_list_page(request: Request) -> Response:
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    tasks = store.get_tasks_for_user(user.username)

    return templates.TemplateResponse(
        request=request,
        name="tasks.html",
        context={
            "user": user,
            "tasks": tasks,
        },
    )


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail_page(request: Request, task_id: str) -> Response:
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    task = store.get_task_for_user(user.username, task_id)
    if not task:
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context={"user": user, "task_id": task_id},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    button_label = get_button_label(request)

    simulate_lost = request.query_params.get("simulate_lost_response") == "true"
    return templates.TemplateResponse(
        request=request,
        name="task_detail.html",
        context={
            "user": user,
            "task": task,
            "success_msg": None,
            "button_label": button_label,
            "simulate_lost_response": simulate_lost,
        },
    )


@app.get("/tasks/{task_id}/review", response_class=HTMLResponse)
async def mark_task_reviewed_get(request: Request, task_id: str) -> Response:
    """GET review handler to simulate lost responses or direct navigation."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    store.mark_task_reviewed(user.username, task_id)
    if (
        request.headers.get("x-test-fault-lost-response") == "true"
        or request.query_params.get("simulate_lost_response") == "true"
    ):
        content = (
            "<!DOCTYPE html><html><body><h1>504 Gateway Timeout</h1>"
            "<p>Response lost</p></body></html>"
        )
        return HTMLResponse(
            content=content,
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        )
    return RedirectResponse(url=f"/tasks/{task_id}", status_code=status.HTTP_302_FOUND)


@app.post("/tasks/{task_id}/review", response_class=HTMLResponse)
async def mark_task_reviewed(request: Request, task_id: str) -> Response:
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)

    success, msg, details = store.mark_task_reviewed(user.username, task_id)
    if not success:
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context={"user": user, "task_id": task_id},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # Check for simulated lost response after successful mutation
    if (
        request.headers.get("x-test-fault-lost-response") == "true"
        or request.query_params.get("simulate_lost_response") == "true"
    ):
        content = (
            "<!DOCTYPE html><html><body><h1>504 Gateway Timeout</h1>"
            "<p>Response lost</p></body></html>"
        )
        return HTMLResponse(
            content=content,
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        )

    # Refresh user & task data after state update
    updated_user = store.users.get(user.username)
    updated_task = store.get_task_for_user(user.username, task_id)
    button_label = get_button_label(request)

    return templates.TemplateResponse(
        request=request,
        name="task_detail.html",
        context={
            "user": updated_user,
            "task": updated_task,
            "success_msg": msg,
            "button_label": button_label,
        },
    )


@app.get("/ui-fixtures", response_class=HTMLResponse)
async def ui_fixtures_page(request: Request) -> Response:
    """Fixture page for testing browser interactions, hover, scroll, delay, detached elements."""
    return templates.TemplateResponse(
        request=request,
        name="ui_fixtures.html",
        context={"user": get_current_user(request)},
    )


@app.get("/security-challenge", response_class=HTMLResponse)
async def security_challenge_page(request: Request) -> Response:
    """Fixture page simulating security checkpoint/challenge requiring operator review."""
    return templates.TemplateResponse(
        request=request,
        name="security_challenge.html",
        context={"user": get_current_user(request)},
    )


# =====================================================================
# Internal Test Fault Control Endpoints (Test Mode Only)
# =====================================================================


@app.get("/_test/faults/state", tags=["test_faults"])
async def test_get_faults_state(request: Request) -> Response:
    """Retrieve current fault injection state. Denied outside test mode or for non-loopback."""
    if not fault_controller.is_test_mode() or not is_loopback_request(request):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "code": "FORBIDDEN",
                "message": "Fault injection disabled outside test mode or for non-loopback clients",
            },
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=fault_controller.get_state())


@app.post("/_test/faults/configure", tags=["test_faults"])
async def test_configure_fault(payload: FaultConfigPayload, request: Request) -> Response:
    """Configure a deterministic fault injection rule. Denied outside test mode/loopback."""
    if not fault_controller.is_test_mode() or not is_loopback_request(request):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "code": "FORBIDDEN",
                "message": "Fault injection disabled outside test mode or for non-loopback clients",
            },
        )

    if payload.fault_type == "delay":
        if not payload.path:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"message": "path is required for delay fault"},
            )
        fault_controller.set_delay(payload.path, payload.seconds, payload.count)
    elif payload.fault_type == "transient_error":
        if not payload.path:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"message": "path is required for transient_error fault"},
            )
        fault_controller.set_transient_error(
            payload.path,
            payload.status_code,
            payload.count,
            payload.message or "Simulated transient 500 fault",
        )
    elif payload.fault_type == "rate_limit":
        if not payload.path:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"message": "path is required for rate_limit fault"},
            )
        fault_controller.set_rate_limit(
            payload.path,
            payload.retry_after,
            payload.count,
            payload.message or "Rate limit exceeded, retry later",
        )
    elif payload.fault_type == "expire_session":
        if payload.session_id:
            fault_controller.expire_session(payload.session_id)
        if payload.username:
            fault_controller.expire_user(payload.username)
    elif payload.fault_type == "navigation":
        if not payload.path:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"message": "path is required for navigation fault"},
            )
        fault_controller.set_navigation_fault(
            payload.path,
            payload.status_code,
            payload.count,
            payload.message or "Simulated navigation failure",
        )
    elif payload.fault_type == "ui_variant":
        if not payload.variant_key:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"message": "variant_key is required for ui_variant fault"},
            )
        fault_controller.set_ui_variant(payload.variant_key, payload.variant_value)
    else:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"message": f"Unknown fault_type '{payload.fault_type}'"},
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ok", "state": fault_controller.get_state()},
    )


@app.post("/_test/faults/reset", tags=["test_faults"])
async def test_reset_faults(request: Request) -> Response:
    """Reset all active fault rules. Denied outside test mode or for non-loopback."""
    if not fault_controller.is_test_mode() or not is_loopback_request(request):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "code": "FORBIDDEN",
                "message": "Fault injection disabled outside test mode or for non-loopback clients",
            },
        )
    fault_controller.reset()
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ok", "message": "Fault controller reset successfully"},
    )
