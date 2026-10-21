"""Secure per-account session storage, lifecycle, and access control (Milestone M3.2).

Guarantees:
1. No plaintext passwords, tokens, or cookies in database storage.
2. Authenticated encryption (AES-256-GCM) with Additional Authenticated Data (AAD)
   strictly bound to provider ID, account ID, and key version.
3. Owner-bound access control: sessions cannot be decrypted or accessed across accounts.
4. Complete session lifecycle management: creation, retrieval, validation, expiration,
   invalidation, and revocation.
5. All secrets redacted from error messages, string representations, and audit traces.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logging import redact_url_credentials
from app.persistence.models import (
    AccountAuthState,
    SessionRefModel,
    SessionStatus,
    utc_now,
)
from app.persistence.repository import (
    AccountRepository,
    AuditEventRepository,
    SessionRefRepository,
)
from app.security.session_crypto import (
    SessionCrypto,
    SessionDecryptionError,
    SessionTamperedError,
)

logger = logging.getLogger("adbap.security.session_store")


# =============================================================================
# Domain Models & Exceptions
# =============================================================================


class SessionStoreError(Exception):
    """Base exception for session store operations."""


class SessionNotFoundError(SessionStoreError):
    """Raised when no active session exists for the specified account."""


class SessionExpiredError(SessionStoreError):
    """Raised when the session exists but has exceeded its expiration timestamp."""


class SessionRevokedError(SessionStoreError):
    """Raised when the session has been explicitly revoked."""


class SessionInvalidError(SessionStoreError):
    """Raised when the session has been rejected by the provider or invalidated."""


class SessionAccessDeniedError(SessionStoreError):
    """Raised on cross-account session access attempts or ownership mismatch."""


class SessionData(BaseModel):
    """Structured session authentication data and state."""

    model_config = ConfigDict(extra="ignore")

    account_id: str
    provider_id: str
    session_token: str
    cookies: list[dict[str, Any]] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"<SessionData account_id={self.account_id!r} provider_id={self.provider_id!r} "
            f"token='[REDACTED]' cookies_count={len(self.cookies)}>"
        )

    def __str__(self) -> str:
        return self.__repr__()


# =============================================================================
# Secure Session Store Service
# =============================================================================


class SecureSessionStore:
    """Encrypted per-account session storage and lifecycle manager."""

    def __init__(
        self,
        db_session: AsyncSession,
        crypto: SessionCrypto | None = None,
    ) -> None:
        self.db = db_session
        self.crypto = crypto if crypto is not None else SessionCrypto()
        self.account_repo = AccountRepository(db_session)
        self.session_repo = SessionRefRepository(db_session)
        self.audit_repo = AuditEventRepository(db_session)

    async def save_session(
        self,
        account_id: str,
        provider_id: str,
        session_data: SessionData,
        vault_ref: str | None = None,
        key_version: int = 1,
    ) -> SessionRefModel:
        """Encrypts and persists session credentials for an account.

        Guarantees:
        - Plaintext session tokens are never written to the database.
        - Encryption is cryptographically bound to account_id and provider_id.
        - Any prior active session references for this account are marked superseded.
        - Account auth_state is updated to ACTIVE.
        """
        if session_data.account_id != account_id:
            raise SessionAccessDeniedError(
                f"Session data account '{session_data.account_id}' "
                f"does not match target account '{account_id}'."
            )
        if session_data.provider_id != provider_id:
            raise SessionAccessDeniedError(
                f"Session data provider '{session_data.provider_id}' "
                f"does not match target provider '{provider_id}'."
            )

        # Verify account exists in persistence
        account = await self.account_repo.get_by_id(account_id)
        if not account or account.provider_id != provider_id:
            raise SessionStoreError(
                f"Account '{account_id}' not found for provider '{provider_id}'."
            )

        # Supersede any existing active sessions for this account
        prior_active = await self.session_repo.get_active_session_ref(account_id)
        if prior_active:
            prior_active.status = SessionStatus.REVOKED
            prior_active.revoked_at = utc_now()

        # Serialize and encrypt session payload
        serialized = session_data.model_dump_json()
        encrypted_payload = self.crypto.encrypt(
            plaintext=serialized,
            provider_id=provider_id,
            account_id=account_id,
            key_version=key_version,
        )

        effective_vault_ref = vault_ref or f"vault://sessions/{provider_id}/{account_id}"

        # Persist encrypted session reference
        ref = await self.session_repo.create_session_ref(
            account_id=account_id,
            vault_ref=effective_vault_ref,
            key_version=key_version,
            expires_at=session_data.expires_at,
            encrypted_payload=encrypted_payload,
            status=SessionStatus.ACTIVE,
        )

        # Update account auth state
        account.auth_state = AccountAuthState.ACTIVE
        account.updated_at = utc_now()

        # Append immutable audit event (with zero secrets)
        await self.audit_repo.record_event(
            actor="system",
            action="SESSION_CREATED",
            resource=f"account:{account_id}",
            outcome="SUCCESS",
            sanitized_details={
                "provider_id": provider_id,
                "session_ref_id": ref.id,
                "key_version": key_version,
                "has_expires_at": session_data.expires_at is not None,
            },
        )

        await self.db.flush()
        return ref

    async def get_valid_session(
        self,
        account_id: str,
        provider_id: str,
        expected_owner_id: str | None = None,
    ) -> SessionData | None:
        """Retrieves and decrypts the active session for an account.

        Guarantees:
        - Owner-bound access verification.
        - Expired, revoked, or invalidated sessions are rejected.
        - Ciphertext tampering or account transplanting is cryptographically caught.
        """
        # Verify account ownership if requested
        account = await self.account_repo.get_by_id(account_id)
        if not account or account.provider_id != provider_id:
            return None

        if expected_owner_id and account.owner_id != expected_owner_id:
            raise SessionAccessDeniedError(
                f"Account '{account_id}' is owned by '{account.owner_id}', "
                f"not '{expected_owner_id}'."
            )

        ref = await self.session_repo.get_active_session_ref(account_id)
        if not ref:
            return None

        if ref.status != SessionStatus.ACTIVE:
            return None

        if ref.expires_at:
            exp = ref.expires_at if ref.expires_at.tzinfo else ref.expires_at.replace(tzinfo=UTC)
            if exp <= datetime.now(UTC):
                ref.status = SessionStatus.EXPIRED
                await self.db.flush()
                return None

        if not ref.encrypted_payload:
            return None

        try:
            decrypted_json = self.crypto.decrypt(
                ciphertext_b64=ref.encrypted_payload,
                provider_id=provider_id,
                account_id=account_id,
                key_version=ref.key_version,
            )
        except (SessionTamperedError, SessionDecryptionError) as err:
            logger.warning(
                "Decryption failed for account '%s' (tampered, corrupted, or invalid key): %s",
                account_id,
                str(err),
            )
            raise

        try:
            data_dict = json.loads(decrypted_json)
            session_data = SessionData.model_validate(data_dict)
        except Exception as err:
            raise SessionStoreError("Failed to deserialize decrypted session data.") from err

        # Verify that decrypted content matches requested account and provider
        if session_data.account_id != account_id or session_data.provider_id != provider_id:
            raise SessionAccessDeniedError(
                "Decrypted session payload does not match expected account or provider."
            )

        return session_data

    async def revoke_session(
        self,
        account_id: str,
        provider_id: str,
        reason: str = "operator_revocation",
    ) -> int:
        """Revokes all active sessions for an account immediately."""
        refs = await self.session_repo.list_session_refs_for_account(account_id)
        revoked_count = 0
        now = utc_now()

        for ref in refs:
            if ref.status == SessionStatus.ACTIVE:
                ref.status = SessionStatus.REVOKED
                ref.revoked_at = now
                revoked_count += 1

        account = await self.account_repo.get_by_id(account_id)
        if account:
            account.auth_state = AccountAuthState.UNAUTHENTICATED
            account.updated_at = now

        await self.audit_repo.record_event(
            actor="system",
            action="SESSION_REVOKED",
            resource=f"account:{account_id}",
            outcome="SUCCESS",
            sanitized_details={
                "provider_id": provider_id,
                "reason": reason,
                "revoked_count": revoked_count,
            },
        )

        await self.db.flush()
        return revoked_count

    async def invalidate_session(
        self,
        account_id: str,
        provider_id: str,
        reason: str = "server_rejected",
    ) -> None:
        """Marks active session as invalid (e.g. following provider 401 or server restart)."""
        ref = await self.session_repo.get_active_session_ref(account_id)
        now = utc_now()

        if ref:
            ref.status = SessionStatus.INVALID
            ref.revoked_at = now

        account = await self.account_repo.get_by_id(account_id)
        if account:
            account.auth_state = AccountAuthState.INVALID_CREDENTIALS
            account.updated_at = now

        await self.audit_repo.record_event(
            actor="system",
            action="SESSION_INVALIDATED",
            resource=f"account:{account_id}",
            outcome="REAUTH_REQUIRED",
            sanitized_details={"provider_id": provider_id, "reason": reason},
        )

        await self.db.flush()

    async def verify_session_with_provider(
        self,
        session_data: SessionData,
        base_url: str = "http://127.0.0.1:8001",
    ) -> bool:
        """Probes provider endpoint to verify if session remains valid on the server.

        If server was restarted or session expired, automatically invalidates local session
        and returns False to trigger reauthentication.
        """
        if session_data.provider_id != "rewardlab":
            return True

        headers = {"Authorization": f"Bearer {session_data.session_token}"}
        cookies = {
            c["name"]: c["value"]
            for c in session_data.cookies
            if "name" in c and "value" in c
        }

        try:
            async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=5.0) as client:
                resp = await client.get(
                    "/api/tasks",
                    headers=headers,
                    cookies=cookies,
                    follow_redirects=False,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    owner = data.get("username")
                    if owner and owner != session_data.account_id:
                        logger.warning(
                            "Provider session ownership mismatch: expected '%s', got '%s'",
                            session_data.account_id,
                            owner,
                        )
                        await self.invalidate_session(
                            session_data.account_id,
                            session_data.provider_id,
                            reason="provider_ownership_mismatch",
                        )
                        return False
                    return True

                if resp.status_code in (401, 403) or resp.status_code in (302, 307):
                    logger.info(
                        "Provider rejected session for account '%s' (status %d)",
                        session_data.account_id,
                        resp.status_code,
                    )
                    await self.invalidate_session(
                        session_data.account_id,
                        session_data.provider_id,
                        reason=f"provider_http_{resp.status_code}",
                    )
                    return False

                return True
        except Exception as err:
            logger.debug(
                "Provider session verification probe error: %s",
                redact_url_credentials(str(err)),
            )
            return False
