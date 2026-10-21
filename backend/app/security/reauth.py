"""Synthetic account reauthentication manager for RewardLab (Milestone M3.2).

Provides controlled, audited reauthentication for synthetic test accounts (test_01 to test_12)
when sessions expire, are invalidated, or when the mock server restarts.

Invariants:
- Never automatically switches account identity to continue a workflow.
- Never logs or exposes raw passwords in exceptions, logs, or audit records.
- Cryptographically stores new session tokens immediately in SecureSessionStore.
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logging import redact_url_credentials
from app.persistence.models import AccountAuthState
from app.persistence.repository import AccountRepository, AuditEventRepository
from app.security.session_store import SecureSessionStore, SessionData

logger = logging.getLogger("adbap.security.reauth")


class AuthenticationFailedError(Exception):
    """Raised when authentication or reauthentication with the provider fails."""


class ReauthenticationManager:
    """Manages reauthentication flows for synthetic test accounts."""

    def __init__(
        self,
        db_session: AsyncSession,
        session_store: SecureSessionStore,
    ) -> None:
        self.db = db_session
        self.session_store = session_store
        self.account_repo = AccountRepository(db_session)
        self.audit_repo = AuditEventRepository(db_session)

    async def reauthenticate_synthetic_account(
        self,
        account_id: str,
        provider_id: str = "rewardlab",
        password: str | None = None,
        base_url: str = "http://127.0.0.1:8001",
        session_ttl_hours: int = 2,
    ) -> SessionData:
        """Authenticates synthetic account with RewardLab and stores new encrypted session.

        Guarantees:
        1. Prohibits cross-account identity switching.
        2. Validates that server assigns session matching target account_id.
        3. Plaintext password is never persisted or logged.
        4. Updates account auth_state and records audit trail.
        """
        if provider_id != "rewardlab":
            raise AuthenticationFailedError(
                f"Automated reauthentication is strictly prohibited "
                f"for non-test provider '{provider_id}'."
            )

        account = await self.account_repo.get_by_id(account_id)
        if not account or account.provider_id != provider_id:
            raise AuthenticationFailedError(
                f"Account '{account_id}' does not exist for provider '{provider_id}'."
            )

        effective_password = password or os.environ.get("REWARDLAB_PASSWORD")
        if not effective_password:
            raise AuthenticationFailedError(
                f"No credentials provided for account '{account_id}'. "
                "Set REWARDLAB_PASSWORD in the environment."
            )

        clean_base = base_url.rstrip("/")

        try:
            async with httpx.AsyncClient(base_url=clean_base, timeout=10.0) as client:
                resp = await client.post(
                    "/login",
                    data={"username": account_id, "password": effective_password},
                    follow_redirects=False,
                )

                # Extract cookie from headers or cookies
                session_token = None
                for header_name, header_val in resp.headers.items():
                    if (
                        header_name.lower() == "set-cookie"
                        and "rewardlab_session=" in header_val
                    ):
                        parts = header_val.split(";")[0].split("=", 1)
                        if len(parts) == 2:
                            session_token = parts[1].strip()
                            break

                if not session_token:
                    with contextlib.suppress(Exception):
                        session_token = resp.cookies.get("rewardlab_session")

                if not session_token:
                    account.auth_state = AccountAuthState.INVALID_CREDENTIALS
                    await self.audit_repo.record_event(
                        actor="system",
                        action="REAUTH_FAILED",
                        resource=f"account:{account_id}",
                        outcome="INVALID_CREDENTIALS",
                        sanitized_details={
                            "provider_id": provider_id,
                            "status_code": resp.status_code,
                        },
                    )
                    await self.db.flush()
                    raise AuthenticationFailedError(
                        f"Authentication failed for account '{account_id}': "
                        "server did not return session cookie."
                    )

                # Verification probe: check that the session actually belongs to account_id
                verify_resp = await client.get(
                    "/api/tasks",
                    cookies={"rewardlab_session": session_token},
                    headers={"Authorization": f"Bearer {session_token}"},
                    follow_redirects=False,
                )

                if verify_resp.status_code != 200:
                    account.auth_state = AccountAuthState.INVALID_CREDENTIALS
                    await self.db.flush()
                    raise AuthenticationFailedError(
                        f"Verification probe failed for account '{account_id}' "
                        f"(status {verify_resp.status_code})."
                    )

                probe_data = verify_resp.json()
                owner = probe_data.get("username")
                if owner and owner != account_id:
                    account.auth_state = AccountAuthState.INVALID_CREDENTIALS
                    await self.db.flush()
                    raise AuthenticationFailedError(
                        f"Session owner mismatch: server returned session for '{owner}', "
                        f"expected '{account_id}'."
                    )

        except httpx.RequestError as err:
            logger.warning(
                "Network error during reauthentication for account '%s': %s",
                account_id,
                redact_url_credentials(str(err)),
            )
            raise AuthenticationFailedError(
                f"Network failure communicating with provider endpoint: "
                f"{redact_url_credentials(str(err))}"
            ) from err

        expires_at = datetime.now(UTC) + timedelta(hours=session_ttl_hours)
        cookies_list: list[dict[str, Any]] = [
            {
                "name": "rewardlab_session",
                "value": session_token,
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": True,
                "secure": False,
            }
        ]

        session_data = SessionData(
            account_id=account_id,
            provider_id=provider_id,
            session_token=session_token,
            cookies=cookies_list,
            headers={"Authorization": f"Bearer {session_token}"},
            expires_at=expires_at,
            metadata={"auth_method": "form_login", "verified_at": datetime.now(UTC).isoformat()},
        )

        # Save encrypted session
        await self.session_store.save_session(
            account_id=account_id,
            provider_id=provider_id,
            session_data=session_data,
        )

        account.auth_state = AccountAuthState.READY
        await self.audit_repo.record_event(
            actor="system",
            action="REAUTH_SUCCESS",
            resource=f"account:{account_id}",
            outcome="SUCCESS",
            sanitized_details={"provider_id": provider_id, "expires_at": expires_at.isoformat()},
        )
        await self.db.flush()

        logger.info("Successfully reauthenticated and saved encrypted session for '%s'", account_id)
        return session_data
