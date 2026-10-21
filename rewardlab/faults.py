import contextlib
import copy
import os
import threading
from typing import Any

from pydantic import BaseModel


class FaultConfigPayload(BaseModel):
    fault_type: str  # delay, transient_error, rate_limit, expire_session, navigation, ui_variant
    path: str | None = None
    count: int = 1
    seconds: float = 0.0
    status_code: int = 500
    retry_after: int = 2
    session_id: str | None = None
    username: str | None = None
    variant_key: str | None = None
    variant_value: Any | None = None
    message: str | None = None


class FaultController:
    """Thread-safe controller for deterministic fault injection in RewardLab.

    Only active when test mode is enabled (REWARDLAB_TEST_MODE=true).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._test_mode_override: bool | None = None
        self.delays: dict[str, dict[str, Any]] = {}
        self.transient_errors: dict[str, dict[str, Any]] = {}
        self.rate_limits: dict[str, dict[str, Any]] = {}
        self.expired_sessions: set[str] = set()
        self.expired_users: set[str] = set()
        self.navigation_faults: dict[str, dict[str, Any]] = {}
        self.ui_variants: dict[str, Any] = {}

    def is_test_mode(self) -> bool:
        """Return True if RewardLab is operating in test mode."""
        with self._lock:
            if self._test_mode_override is not None:
                return self._test_mode_override
        env_val = os.environ.get("REWARDLAB_TEST_MODE", "false").strip().lower()
        return env_val in ("1", "true", "yes", "on")

    def set_test_mode(self, enabled: bool | None) -> None:
        """Override test mode for unit testing."""
        with self._lock:
            self._test_mode_override = enabled

    def reset(self) -> None:
        """Clear all active fault rules and reset to normal operation."""
        with self._lock:
            self.delays.clear()
            self.transient_errors.clear()
            self.rate_limits.clear()
            self.expired_sessions.clear()
            self.expired_users.clear()
            self.navigation_faults.clear()
            self.ui_variants.clear()
            self._test_mode_override = None

    def set_delay(self, path: str, seconds: float, count: int = 1) -> None:
        """Configure delayed rendering/response for a path."""
        with self._lock:
            self.delays[path] = {"seconds": max(0.0, seconds), "count": max(1, count)}

    def set_transient_error(
        self,
        path: str,
        status_code: int = 500,
        count: int = 1,
        message: str = "Simulated transient 500 fault",
    ) -> None:
        """Configure transient HTTP error for a path."""
        with self._lock:
            self.transient_errors[path] = {
                "status_code": status_code,
                "count": max(1, count),
                "message": message,
            }

    def set_rate_limit(
        self,
        path: str,
        retry_after: int = 2,
        count: int = 1,
        message: str = "Rate limit exceeded, retry later",
    ) -> None:
        """Configure HTTP 429 Rate Limit with Retry-After for a path."""
        with self._lock:
            self.rate_limits[path] = {
                "retry_after": max(1, retry_after),
                "count": max(1, count),
                "message": message,
            }

    def expire_session(self, session_id: str) -> None:
        """Mark a specific session as expired."""
        with self._lock:
            self.expired_sessions.add(session_id)

    def expire_user(self, username: str) -> None:
        """Mark all sessions for a username as expired."""
        with self._lock:
            self.expired_users.add(username)

    def is_session_expired(self, session_id: str | None, username: str | None = None) -> bool:
        """Check if given session or username has been forced to expired state."""
        if not self.is_test_mode():
            return False
        with self._lock:
            if session_id and session_id in self.expired_sessions:
                return True
            return bool(username and username in self.expired_users)

    def set_navigation_fault(
        self,
        path: str,
        status_code: int = 503,
        count: int = 1,
        message: str = "Simulated navigation failure",
    ) -> None:
        """Configure a page navigation failure for a path."""
        with self._lock:
            self.navigation_faults[path] = {
                "status_code": status_code,
                "count": max(1, count),
                "message": message,
            }

    def set_ui_variant(self, key: str, value: Any) -> None:
        """Set a UI variant, e.g. 'button_label' -> 'Confirm Review'."""
        with self._lock:
            if value is None and key in self.ui_variants:
                del self.ui_variants[key]
            else:
                self.ui_variants[key] = value

    def get_ui_variant(self, key: str, default: Any = None) -> Any:
        """Retrieve active UI variant if in test mode."""
        if not self.is_test_mode():
            return default
        with self._lock:
            return self.ui_variants.get(key, default)

    def get_state(self) -> dict[str, Any]:
        """Return snapshot of active fault rules."""
        with self._lock:
            return {
                "test_mode": self.is_test_mode(),
                "delays": copy.deepcopy(self.delays),
                "transient_errors": copy.deepcopy(self.transient_errors),
                "rate_limits": copy.deepcopy(self.rate_limits),
                "expired_sessions_count": len(self.expired_sessions),
                "expired_users": list(self.expired_users),
                "navigation_faults": copy.deepcopy(self.navigation_faults),
                "ui_variants": copy.deepcopy(self.ui_variants),
            }

    async def evaluate_request(
        self, path: str, headers: dict[str, str]
    ) -> tuple[int | None, dict[str, str], dict[str, Any] | None, float]:
        """Evaluate incoming request against active fault rules and headers.

        Returns: (status_code, response_headers, response_body, delay_seconds).
        If status_code is None, request proceeds normally after any delay.
        """
        if not self.is_test_mode():
            return None, {}, None, 0.0

        delay_seconds = 0.0
        norm_headers = {k.lower(): v for k, v in headers.items()}

        # 1. Header-based transient overrides (scoped to this specific request)
        if "x-test-fault-delay" in norm_headers:
            with contextlib.suppress(ValueError):
                delay_seconds = float(norm_headers["x-test-fault-delay"])

        if "x-test-fault-expire-session" in norm_headers:
            return (
                401,
                {},
                {"code": "SESSION_EXPIRED", "message": "Authentication session has expired"},
                delay_seconds,
            )

        if "x-test-fault-500" in norm_headers:
            return (
                500,
                {},
                {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "Simulated transient 500 fault",
                    "fault": "transient_500",
                },
                delay_seconds,
            )

        if "x-test-fault-429" in norm_headers:
            retry_after = norm_headers.get("x-test-fault-429", "2")
            retry_int = 2
            with contextlib.suppress(ValueError):
                retry_int = int(retry_after)
            return (
                429,
                {"Retry-After": str(retry_int)},
                {
                    "code": "RATE_LIMITED",
                    "message": f"Rate limit exceeded, retry after {retry_int}s",
                    "retry_after": retry_int,
                },
                delay_seconds,
            )

        if "x-test-fault-navigation" in norm_headers:
            nav_status = 503
            with contextlib.suppress(ValueError):
                nav_status = int(norm_headers["x-test-fault-navigation"])
            return (
                nav_status,
                {},
                {"code": "NAVIGATION_FAILED", "message": "Simulated navigation failure"},
                delay_seconds,
            )

        # 2. Stateful path-based fault rules
        with self._lock:
            # Check configured delay
            if path in self.delays:
                rule = self.delays[path]
                delay_seconds = max(delay_seconds, rule["seconds"])
                rule["count"] -= 1
                if rule["count"] <= 0:
                    del self.delays[path]

            # Check navigation fault
            if path in self.navigation_faults:
                rule = self.navigation_faults[path]
                status_code = rule["status_code"]
                msg = rule["message"]
                rule["count"] -= 1
                if rule["count"] <= 0:
                    del self.navigation_faults[path]
                return (
                    status_code,
                    {},
                    {"code": "NAVIGATION_FAILED", "message": msg},
                    delay_seconds,
                )

            # Check transient error (e.g. 500)
            if path in self.transient_errors:
                rule = self.transient_errors[path]
                status_code = rule["status_code"]
                msg = rule["message"]
                rule["count"] -= 1
                if rule["count"] <= 0:
                    del self.transient_errors[path]
                return (
                    status_code,
                    {},
                    {
                        "code": "INTERNAL_SERVER_ERROR",
                        "message": msg,
                        "fault": "transient_error",
                    },
                    delay_seconds,
                )

            # Check rate limit (429)
            if path in self.rate_limits:
                rule = self.rate_limits[path]
                retry_after = rule["retry_after"]
                msg = rule["message"]
                rule["count"] -= 1
                if rule["count"] <= 0:
                    del self.rate_limits[path]
                return (
                    429,
                    {"Retry-After": str(retry_after)},
                    {
                        "code": "RATE_LIMITED",
                        "message": msg,
                        "retry_after": retry_after,
                    },
                    delay_seconds,
                )

        return None, {}, None, delay_seconds


# Global fault controller singleton
fault_controller = FaultController()
