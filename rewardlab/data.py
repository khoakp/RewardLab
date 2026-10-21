import copy
import threading
import uuid
from typing import Any

from pydantic import BaseModel

from faults import fault_controller


def is_test_mode() -> bool:
    """Return True if RewardLab is operating in test mode."""
    return fault_controller.is_test_mode()


def set_test_mode_override(enabled: bool | None) -> None:
    """Explicitly override test mode for isolated unit and security tests."""
    fault_controller.set_test_mode(enabled)


class SyntheticUser(BaseModel):
    username: str
    password: str
    display_name: str
    points: int = 0


class SyntheticTask(BaseModel):
    id: str
    title: str
    description: str
    reward_points: int = 10
    reviewed: bool = False


def build_initial_data() -> tuple[dict[str, SyntheticUser], dict[str, list[SyntheticTask]]]:
    """Build deterministic fixture data for 12 synthetic accounts and independent tasks."""
    users: dict[str, SyntheticUser] = {}
    tasks: dict[str, list[SyntheticTask]] = {}

    initial_points = [120, 50, 80, 110, 95, 60, 130, 40, 70, 150, 85, 100]

    for i in range(1, 13):
        acc_id = f"test_{i:02d}"
        users[acc_id] = SyntheticUser(
            username=acc_id,
            password=f"test_password_{i:02d}",
            display_name=f"Synthetic Test Account {i:02d}",
            points=initial_points[i - 1],
        )

        # Each synthetic user gets an independent copy of tasks
        tasks[acc_id] = [
            SyntheticTask(
                id="task_01",
                title="Review Daily Research Memo",
                description=(
                    "Acknowledge and inspect synthetic research memo #101 "
                    "regarding automation telemetry."
                ),
                reward_points=10,
                reviewed=False,
            ),
            SyntheticTask(
                id="task_02",
                title="Verify Simulated Node Health",
                description="Check synthetic worker cluster health status and sign off.",
                reward_points=15,
                reviewed=False,
            ),
            SyntheticTask(
                id="task_03",
                title="Archive Completed Session Logs",
                description="Verify that test session audit logs are safely rotated.",
                reward_points=5,
                reviewed=True,
            ),
        ]

    return users, tasks


INITIAL_USERS, INITIAL_TASKS = build_initial_data()


class RewardLabStore:
    """In-memory data store for RewardLab synthetic accounts, tasks and sessions.

    Enforces account isolation, concurrency-safe review, and deterministic reset.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.users: dict[str, SyntheticUser] = {}
        self.user_tasks: dict[str, list[SyntheticTask]] = {}
        self.sessions: dict[str, str] = {}  # session_id -> username
        self.idempotency_records: dict[tuple[str, str], dict[str, Any]] = {}
        self.reset()

    def reset(self) -> None:
        """Reset store and fault controller to initial deterministic state."""
        fault_controller.reset()
        with self._lock:
            self.users = copy.deepcopy(INITIAL_USERS)
            self.user_tasks = copy.deepcopy(INITIAL_TASKS)
            self.sessions.clear()
            self.idempotency_records.clear()

    def authenticate(self, username: str, password: str) -> str | None:
        """Authenticate user and return a new session ID if valid."""
        with self._lock:
            user = self.users.get(username)
            if user and user.password == password:
                session_id = str(uuid.uuid4())
                self.sessions[session_id] = username
                return session_id
            return None

    def get_user_by_session(self, session_id: str | None) -> SyntheticUser | None:
        """Retrieve user associated with session_id."""
        if not session_id:
            return None
        with self._lock:
            username = self.sessions.get(session_id)
            if not username:
                return None
            if fault_controller.is_session_expired(session_id, username):
                return None
            return self.users.get(username)

    def revoke_session(self, session_id: str | None) -> None:
        """Remove session_id from active sessions."""
        if not session_id:
            return
        with self._lock:
            if session_id in self.sessions:
                del self.sessions[session_id]

    def get_tasks_for_user(self, username: str) -> list[SyntheticTask]:
        """Retrieve tasks for given user."""
        with self._lock:
            return self.user_tasks.get(username, [])

    def get_task_for_user(self, username: str, task_id: str) -> SyntheticTask | None:
        """Retrieve a specific task by task_id for user."""
        with self._lock:
            tasks = self.user_tasks.get(username, [])
            for task in tasks:
                if task.id == task_id:
                    return task
            return None

    def mark_task_reviewed(
        self, username: str, task_id: str, idempotency_key: str | None = None
    ) -> tuple[bool, str, dict[str, Any]]:
        """Concurrency-safe and idempotent task review action.

        Guarantees that repeated/concurrent reviews award points at most once.
        Supports tracking via explicit idempotency_key.
        """
        with self._lock:
            # Check cached idempotency record
            if idempotency_key:
                cached = self.idempotency_records.get((username, idempotency_key))
                if cached:
                    if cached["task_id"] != task_id:
                        err_msg = (
                            f"Idempotency key '{idempotency_key}' conflict: "
                            f"already used for task '{cached['task_id']}'"
                        )
                        return (
                            False,
                            err_msg,
                            {
                                "code": "IDEMPOTENCY_CONFLICT",
                                "conflicting_task_id": cached["task_id"],
                            },
                        )
                    res = copy.deepcopy(cached)
                    res["idempotent_replay"] = True
                    res["points_awarded"] = 0
                    return True, "Idempotent replay: task already completed", res

            tasks = self.user_tasks.get(username, [])
            task = None
            for t in tasks:
                if t.id == task_id:
                    task = t
                    break

            user = self.users.get(username)
            if not task or not user:
                return False, "Task or user not found", {}

            if task.reviewed:
                res = {
                    "task_id": task.id,
                    "reviewed": True,
                    "points_awarded": 0,
                    "total_points": user.points,
                    "idempotent_replay": True,
                    "idempotency_key": idempotency_key,
                }
                if idempotency_key:
                    self.idempotency_records[(username, idempotency_key)] = res
                return True, "Task already marked as reviewed", res

            task.reviewed = True
            user.points += task.reward_points
            msg = f"Task reviewed successfully! +{task.reward_points} fake points awarded."
            res = {
                "task_id": task.id,
                "reviewed": True,
                "points_awarded": task.reward_points,
                "total_points": user.points,
                "idempotent_replay": False,
                "idempotency_key": idempotency_key,
            }
            if idempotency_key:
                self.idempotency_records[(username, idempotency_key)] = res
            return True, msg, res


# Global in-memory singleton for RewardLab
store = RewardLabStore()
