"""Unit and integration tests for Secure Per-Account Session Store (Milestone M3.2).

Tests:
1. Session encryption round-trip with AES-256-GCM.
2. Safe rejection on wrong key or tampered ciphertext.
3. Cryptographic cross-account session transplantation denial via AAD.
4. Encrypted session persistence (no plaintext token in database rows).
5. Raw SQLite database file binary scan (plaintext session token NOT found in .db file).
6. Owner-bound session access and cross-account retrieval denial.
7. Session expiration handling (marks EXPIRED and excludes from retrieval).
8. Explicit session revocation (marks REVOKED and updates account state).
9. Session invalidation on provider probe failure (401 or server restart).
10. Synthetic account reauthentication flow with RewardLab.
11. Secret redaction in string and repr representations.
12. Database restart persistence (file-backed SQLite re-open retains data).
13. Alembic baseline and M3.2 migrations on fresh SQLite database.
14. Foreign key and unique constraint enforcement after migration.
15. BrowserContext cookie injection and account isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from alembic import command
from app.persistence.database import (
    Base,
    get_session_factory,
)
from app.persistence.models import (
    AccountAuthState,
    AccountModel,
    ProviderModel,
    SessionRefModel,
    SessionStatus,
)
from app.persistence.repository import (
    AccountRepository,
)
from app.security.reauth import (
    AuthenticationFailedError,
    ReauthenticationManager,
)
from app.security.session_crypto import (
    SessionCrypto,
    SessionKeyConfigurationError,
    SessionTamperedError,
    generate_session_encryption_key,
    parse_encryption_key,
)
from app.security.session_store import (
    SecureSessionStore,
    SessionAccessDeniedError,
    SessionData,
)

# Test encryption key (256-bit base64)
TEST_KEY_1 = "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="
TEST_KEY_2 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU="


@pytest.fixture
def crypto() -> SessionCrypto:
    """Fixture providing a deterministic AES-256-GCM crypto instance."""
    return SessionCrypto(key=TEST_KEY_1)


@pytest.fixture
async def async_db_session():
    """Provides an isolated in-memory SQLite AsyncSession with foreign keys enabled."""
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        echo=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        # Seed provider
        prov = ProviderModel(
            id="rewardlab",
            adapter_kind="mock",
            policy_version=1,
            capabilities_json=["read_tasks", "mark_reviewed"],
        )
        session.add(prov)

        # Seed accounts
        acc1 = AccountModel(
            id="test_01",
            provider_id="rewardlab",
            owner_id="team_qa",
            label="Synthetic Account 1",
            auth_state=AccountAuthState.READY,
        )
        acc2 = AccountModel(
            id="test_02",
            provider_id="rewardlab",
            owner_id="team_qa",
            label="Synthetic Account 2",
            auth_state=AccountAuthState.READY,
        )
        acc3 = AccountModel(
            id="test_03",
            provider_id="rewardlab",
            owner_id="other_team",
            label="Synthetic Account 3",
            auth_state=AccountAuthState.READY,
        )
        session.add_all([acc1, acc2, acc3])
        await session.commit()

        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# =============================================================================
# 1. Cryptographic Unit Tests
# =============================================================================


def test_key_parsing_and_generation():
    """Tests key parsing across base64, hex, raw bytes and error cases."""
    gen_key = generate_session_encryption_key()
    parsed = parse_encryption_key(gen_key)
    assert len(parsed) == 32

    # Hex key (64 chars)
    hex_key = "00" * 32
    assert len(parse_encryption_key(hex_key)) == 32

    # Raw bytes (32 bytes)
    raw = b"a" * 32
    assert parse_encryption_key(raw) == raw

    # Invalid lengths
    with pytest.raises(SessionKeyConfigurationError):
        parse_encryption_key("too_short")

    with pytest.raises(SessionKeyConfigurationError):
        parse_encryption_key(b"short_bytes")


def test_missing_key_fails_closed(monkeypatch):
    """Verifies that SessionCrypto raises error when no key is provided or in env."""
    monkeypatch.delenv("ADBAP_SESSION_ENCRYPTION_KEY", raising=False)
    with pytest.raises(SessionKeyConfigurationError):
        SessionCrypto(key=None)


def test_encryption_roundtrip(crypto: SessionCrypto):
    """Tests encryption and decryption with correct context."""
    plaintext = "super_secret_bearer_token_12345"
    ct = crypto.encrypt(plaintext, provider_id="rewardlab", account_id="test_01", key_version=1)
    assert ct != plaintext
    assert plaintext not in ct

    decrypted = crypto.decrypt(ct, provider_id="rewardlab", account_id="test_01", key_version=1)
    assert decrypted == plaintext


def test_wrong_key_fails_safe(crypto: SessionCrypto):
    """Tests that decrypting with a different key is safely rejected."""
    plaintext = "secret_token"
    ct = crypto.encrypt(plaintext, provider_id="rewardlab", account_id="test_01", key_version=1)

    crypto2 = SessionCrypto(key=TEST_KEY_2)
    with pytest.raises(SessionTamperedError):
        crypto2.decrypt(ct, provider_id="rewardlab", account_id="test_01", key_version=1)


def test_tampered_ciphertext_fails_safe(crypto: SessionCrypto):
    """Tests that altering any bit in ciphertext triggers authentication failure."""
    plaintext = "secret_token"
    ct = crypto.encrypt(plaintext, provider_id="rewardlab", account_id="test_01", key_version=1)

    # Corrupt last character
    corrupted = ct[:-1] + ("A" if ct[-1] != "A" else "B")
    with pytest.raises(SessionTamperedError):
        crypto.decrypt(corrupted, provider_id="rewardlab", account_id="test_01", key_version=1)


def test_cross_account_aad_mismatch_fails(crypto: SessionCrypto):
    """Tests that ciphertext encrypted for account A cannot be decrypted for account B.

    Additional Authenticated Data (AAD) cryptographically prevents cross-account transplantation.
    """
    plaintext = "secret_token_for_test_01"
    ct = crypto.encrypt(plaintext, provider_id="rewardlab", account_id="test_01", key_version=1)

    # Attempt to decrypt for test_02 with identical key
    with pytest.raises(SessionTamperedError):
        crypto.decrypt(ct, provider_id="rewardlab", account_id="test_02", key_version=1)


# =============================================================================
# 2. Session Store Persistence & Lifecycle Tests
# =============================================================================


async def test_encrypted_session_persistence(
    async_db_session: AsyncSession, crypto: SessionCrypto
):
    """Verifies that saved sessions are encrypted in database rows with zero plaintext tokens."""
    store = SecureSessionStore(async_db_session, crypto=crypto)
    raw_token = "secret_bearer_token_xyz_999"

    session_data = SessionData(
        account_id="test_01",
        provider_id="rewardlab",
        session_token=raw_token,
        cookies=[{"name": "rewardlab_session", "value": raw_token}],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    ref = await store.save_session(
        account_id="test_01",
        provider_id="rewardlab",
        session_data=session_data,
    )
    assert ref.id is not None
    assert ref.status == SessionStatus.ACTIVE
    assert ref.encrypted_payload is not None
    assert raw_token not in ref.encrypted_payload

    # Inspect directly from DB
    stmt = select(SessionRefModel).where(SessionRefModel.id == ref.id)
    res = await async_db_session.execute(stmt)
    db_row = res.scalar_one()

    assert db_row.encrypted_payload != raw_token
    assert raw_token not in db_row.encrypted_payload

    # Decrypt via store
    retrieved = await store.get_valid_session(account_id="test_01", provider_id="rewardlab")
    assert retrieved is not None
    assert retrieved.session_token == raw_token


async def test_plaintext_session_token_not_found_in_db_file(tmp_path: Path):
    """Inspects raw binary contents of file-backed SQLite database to prove zero plaintext tokens.
    """
    db_path = tmp_path / "raw_check.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    raw_secret_token = "super_unique_secret_token_never_plain_12345"
    crypto = SessionCrypto(key=TEST_KEY_1)

    async with AsyncSession(engine) as session:
        # Seed provider and account
        session.add(
            ProviderModel(
                id="rewardlab",
                adapter_kind="mock",
                policy_version=1,
                capabilities_json=[],
            )
        )
        session.add(
            AccountModel(
                id="test_01",
                provider_id="rewardlab",
                owner_id="team_qa",
                label="Acc 1",
                auth_state=AccountAuthState.READY,
            )
        )
        await session.commit()

        store = SecureSessionStore(session, crypto=crypto)
        session_data = SessionData(
            account_id="test_01",
            provider_id="rewardlab",
            session_token=raw_secret_token,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await store.save_session("test_01", "rewardlab", session_data)
        await session.commit()

    await engine.dispose()

    # Read binary bytes of the actual database file
    db_bytes = db_path.read_bytes()

    # Plaintext token MUST NOT appear in the database file
    assert raw_secret_token.encode("utf-8") not in db_bytes

    # Non-sensitive public identifiers like account_id DO appear
    assert b"test_01" in db_bytes
    assert b"rewardlab" in db_bytes


async def test_owner_bound_access_denial(async_db_session: AsyncSession, crypto: SessionCrypto):
    """Verifies that accessing a session with a mismatched owner raises SessionAccessDeniedError."""
    store = SecureSessionStore(async_db_session, crypto=crypto)
    session_data = SessionData(
        account_id="test_01",
        provider_id="rewardlab",
        session_token="token_1",
    )
    await store.save_session("test_01", "rewardlab", session_data)

    # test_01 is owned by "team_qa". Passing expected_owner_id="unauthorized_owner" must fail.
    with pytest.raises(SessionAccessDeniedError):
        await store.get_valid_session(
            account_id="test_01",
            provider_id="rewardlab",
            expected_owner_id="unauthorized_owner",
        )

    # Valid owner succeeds
    s = await store.get_valid_session(
        account_id="test_01",
        provider_id="rewardlab",
        expected_owner_id="team_qa",
    )
    assert s is not None


async def test_cross_account_save_mismatch_denied(
    async_db_session: AsyncSession, crypto: SessionCrypto
):
    """Verifies that saving session data for account A into account B is rejected."""
    store = SecureSessionStore(async_db_session, crypto=crypto)
    session_data = SessionData(
        account_id="test_01",
        provider_id="rewardlab",
        session_token="token_1",
    )

    with pytest.raises(SessionAccessDeniedError):
        await store.save_session("test_02", "rewardlab", session_data)


async def test_session_expiration(async_db_session: AsyncSession, crypto: SessionCrypto):
    """Verifies that expired sessions are automatically excluded and updated to EXPIRED."""
    store = SecureSessionStore(async_db_session, crypto=crypto)
    expired_time = datetime.now(UTC) - timedelta(minutes=5)

    session_data = SessionData(
        account_id="test_01",
        provider_id="rewardlab",
        session_token="expired_token",
        expires_at=expired_time,
    )
    ref = await store.save_session("test_01", "rewardlab", session_data)

    retrieved = await store.get_valid_session("test_01", "rewardlab")
    assert retrieved is None

    # Verify status in database is EXPIRED
    stmt = select(SessionRefModel).where(SessionRefModel.id == ref.id)
    res = await async_db_session.execute(stmt)
    assert res.scalar_one().status == SessionStatus.EXPIRED


async def test_session_revocation(async_db_session: AsyncSession, crypto: SessionCrypto):
    """Verifies that revoking a session updates status and excludes it from retrieval."""
    store = SecureSessionStore(async_db_session, crypto=crypto)
    session_data = SessionData(
        account_id="test_01",
        provider_id="rewardlab",
        session_token="revokable_token",
    )
    ref = await store.save_session("test_01", "rewardlab", session_data)

    # Revoke
    revoked_count = await store.revoke_session("test_01", "rewardlab", reason="test_revocation")
    assert revoked_count == 1

    # Cannot retrieve
    retrieved = await store.get_valid_session("test_01", "rewardlab")
    assert retrieved is None

    stmt = select(SessionRefModel).where(SessionRefModel.id == ref.id)
    res = await async_db_session.execute(stmt)
    row = res.scalar_one()
    assert row.status == SessionStatus.REVOKED
    assert row.revoked_at is not None


async def test_session_invalidation_on_server_restart(
    async_db_session: AsyncSession, crypto: SessionCrypto
):
    """Tests that provider probe failure (e.g. 401 on server restart) invalidates session."""
    store = SecureSessionStore(async_db_session, crypto=crypto)
    session_data = SessionData(
        account_id="test_01",
        provider_id="rewardlab",
        session_token="stale_server_token",
    )
    ref = await store.save_session("test_01", "rewardlab", session_data)

    # Mock provider returning 401
    mock_resp = httpx.Response(status_code=401, json={"detail": "Session expired or invalid"})

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        is_valid = await store.verify_session_with_provider(session_data)
        assert is_valid is False

    # Check that session is marked INVALID
    stmt = select(SessionRefModel).where(SessionRefModel.id == ref.id)
    res = await async_db_session.execute(stmt)
    assert res.scalar_one().status == SessionStatus.INVALID


# =============================================================================
# 3. Synthetic Account Reauthentication Tests
# =============================================================================


async def test_reauthentication_flow_success(
    async_db_session: AsyncSession, crypto: SessionCrypto
):
    """Tests successful reauthentication: calls /login, gets cookie, saves encrypted session."""
    store = SecureSessionStore(async_db_session, crypto=crypto)
    reauth_mgr = ReauthenticationManager(async_db_session, store)

    login_resp = httpx.Response(
        status_code=303,
        headers={
            "Location": "/tasks",
            "Set-Cookie": "rewardlab_session=new_reauth_token_456; Path=/",
        },
    )
    probe_resp = httpx.Response(
        status_code=200,
        json={"username": "test_01", "tasks": []},
    )

    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
    ):
        mock_post.return_value = login_resp
        mock_get.return_value = probe_resp

        session_data = await reauth_mgr.reauthenticate_synthetic_account(
            account_id="test_01",
            provider_id="rewardlab",
            password="test_password_123",
        )

        assert session_data.session_token == "new_reauth_token_456"
        assert session_data.account_id == "test_01"

    # Verify session is now valid in store
    retrieved = await store.get_valid_session("test_01", "rewardlab")
    assert retrieved is not None
    assert retrieved.session_token == "new_reauth_token_456"


async def test_reauthentication_failed_credentials(
    async_db_session: AsyncSession, crypto: SessionCrypto
):
    """Tests that bad credentials update account to INVALID_CREDENTIALS and raise error."""
    store = SecureSessionStore(async_db_session, crypto=crypto)
    reauth_mgr = ReauthenticationManager(async_db_session, store)

    login_fail_resp = httpx.Response(status_code=200, content=b"Invalid username or password")

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = login_fail_resp
        with pytest.raises(AuthenticationFailedError):
            await reauth_mgr.reauthenticate_synthetic_account(
                account_id="test_01",
                provider_id="rewardlab",
                password="wrong_password",
            )

    account = await AccountRepository(async_db_session).get_by_id("test_01")
    assert account is not None
    assert account.auth_state == AccountAuthState.INVALID_CREDENTIALS


def test_secret_redaction():
    """Verifies that repr and str on SessionData and SessionCrypto never leak secrets."""
    s = SessionData(
        account_id="test_01",
        provider_id="rewardlab",
        session_token="super_secret_token_123",
    )
    rep = repr(s)
    st = str(s)
    assert "super_secret_token_123" not in rep
    assert "super_secret_token_123" not in st
    assert "[REDACTED]" in rep

    c = SessionCrypto(key=TEST_KEY_1)
    assert "[REDACTED]" in repr(c)
    assert TEST_KEY_1 not in repr(c)


# =============================================================================
# 4. M3.1 Persistence Assumptions Verification
# =============================================================================


async def test_sqlite_file_persistence_restart(tmp_path: Path):
    """Verifies that file-backed SQLite database actually persists data across engine restarts."""
    db_file = tmp_path / "persist_test.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    # Engine 1: Create schema and insert data
    engine1 = create_async_engine(db_url)
    async with engine1.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSession(engine1) as s1:
        s1.add(
            ProviderModel(
                id="rewardlab",
                adapter_kind="mock",
                policy_version=1,
                capabilities_json=["read_tasks"],
            )
        )
        s1.add(
            AccountModel(
                id="test_01",
                provider_id="rewardlab",
                owner_id="team_qa",
                label="Persist Acc",
                auth_state=AccountAuthState.READY,
            )
        )
        await s1.commit()

    # Completely shutdown engine 1
    await engine1.dispose()

    # Engine 2: Reopen the database file
    engine2 = create_async_engine(db_url)

    async with AsyncSession(engine2) as s2:
        prov = await s2.get(ProviderModel, "rewardlab")
        acc = await s2.get(AccountModel, "test_01")
        assert prov is not None
        assert prov.adapter_kind == "mock"
        assert acc is not None
        assert acc.label == "Persist Acc"

    await engine2.dispose()


def test_alembic_migration_fresh_db(tmp_path: Path):
    """Verifies that Alembic baseline and M3.2 migrations run successfully on a fresh database."""
    db_file = tmp_path / "fresh_alembic.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"

    # Configure Alembic pointing to the fresh database
    backend_dir = Path(__file__).resolve().parent.parent
    alembic_ini_path = backend_dir / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # Run upgrade head
    command.upgrade(alembic_cfg, "head")

    # Verify tables using synchronous SQLite connection
    from sqlalchemy import create_engine

    sync_engine = create_engine(f"sqlite:///{db_file}")
    inspector = inspect(sync_engine)
    tables = inspector.get_table_names()

    expected_tables = {
        "providers",
        "accounts",
        "session_refs",
        "workflow_versions",
        "jobs",
        "job_steps",
        "nodes",
        "audit_events",
        "activity_records",
    }
    assert expected_tables.issubset(set(tables))

    # Verify session_refs columns
    columns = {col["name"] for col in inspector.get_columns("session_refs")}
    assert "encrypted_payload" in columns
    assert "status" in columns
    assert "revoked_at" in columns


def test_migrated_db_foreign_keys_and_uniques(tmp_path: Path):
    """Verifies that foreign keys and unique constraints function properly on migrated database."""
    db_file = tmp_path / "constraints_check.db"
    db_url = f"sqlite:///{db_file}"

    backend_dir = Path(__file__).resolve().parent.parent
    alembic_ini_path = backend_dir / "alembic.ini"
    alembic_cfg = Config(str(alembic_ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_file}")

    command.upgrade(alembic_cfg, "head")

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session

    engine = create_engine(db_url)

    @event.listens_for(engine, "connect")
    def do_connect(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    with Session(engine) as session:
        # FK violation: account referencing non-existent provider
        bad_acc = AccountModel(
            id="bad_acc",
            provider_id="non_existent_provider",
            owner_id="team_qa",
            label="Bad",
            auth_state=AccountAuthState.READY,
        )
        session.add(bad_acc)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        # Valid provider and account
        prov = ProviderModel(
            id="rewardlab",
            adapter_kind="mock",
            policy_version=1,
            capabilities_json=[],
        )
        acc1 = AccountModel(
            id="acc_1",
            provider_id="rewardlab",
            owner_id="team_qa",
            label="Label 1",
            auth_state=AccountAuthState.READY,
        )
        session.add_all([prov, acc1])
        session.commit()

        # Unique violation: duplicate (provider_id, owner_id, label)
        dup_acc = AccountModel(
            id="acc_2",
            provider_id="rewardlab",
            owner_id="team_qa",
            label="Label 1",
            auth_state=AccountAuthState.READY,
        )
        session.add(dup_acc)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


async def test_browser_context_isolation_with_stored_session(
    async_db_session: AsyncSession, crypto: SessionCrypto
):
    """Verifies that decrypted session cookies are injected into BrowserContext with zero leakage.
    """
    from app.execution.browser_manager import BrowserManager

    store = SecureSessionStore(async_db_session, crypto=crypto)
    session_data = SessionData(
        account_id="test_01",
        provider_id="rewardlab",
        session_token="secret_cookie_token_test_01",
    )
    await store.save_session("test_01", "rewardlab", session_data)

    retrieved = await store.get_valid_session("test_01", "rewardlab")
    assert retrieved is not None

    bm = BrowserManager(headless=True)
    await bm.start()

    try:
        # Context 1: Account test_01 with retrieved session cookie
        ctx1 = await bm.new_context(
            account_id="test_01",
            session_id=retrieved.session_token,
            base_url="http://127.0.0.1:8001",
        )
        cookies1 = await ctx1.cookies()
        session_cookie = next((c for c in cookies1 if c["name"] == "rewardlab_session"), None)
        assert session_cookie is not None
        assert session_cookie["value"] == "secret_cookie_token_test_01"

        # Context 2: Account test_02 without session
        ctx2 = await bm.new_context(
            account_id="test_02",
            session_id=None,
            base_url="http://127.0.0.1:8001",
        )
        cookies2 = await ctx2.cookies()
        assert not any(c["name"] == "rewardlab_session" for c in cookies2)

        # Contexts are strictly isolated
        assert len(cookies1) > len(cookies2)
    finally:
        await bm.stop()

