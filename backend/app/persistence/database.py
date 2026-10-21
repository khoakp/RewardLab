"""Database engine, session management, and base declarative class (Milestone M3.1).

Configures async SQLAlchemy 2.0 with aiosqlite for single-host bootstrap,
enforcing foreign key constraints on SQLite connections.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """Base declarative class for all ADBAP persistence models."""

    pass


# Enable SQLite foreign key constraint enforcement
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Enforces SQLite foreign key constraints, WAL journal mode, and busy timeout."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()
    with suppress(Exception):
        dbapi_connection.isolation_level = None


@event.listens_for(Engine, "begin")
def set_sqlite_begin(conn):
    """Emits BEGIN IMMEDIATE for SQLite engines to avoid concurrent upgrade deadlocks."""
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def get_async_engine(db_url: str | None = None, echo: bool = False) -> AsyncEngine:
    """Creates an async SQLAlchemy engine with connection pool settings."""
    url = db_url or settings.database_url
    return create_async_engine(
        url,
        echo=echo,
        future=True,
    )


def get_session_factory(
    engine: AsyncEngine | str | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Returns an async sessionmaker bound to the given engine or database URL."""
    eng = get_async_engine(engine) if isinstance(engine, str) else (engine or get_async_engine())
    return async_sessionmaker(
        bind=eng,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


# Default global async sessionmaker
async_session_factory = get_session_factory()


@asynccontextmanager
async def get_db_session(
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> AsyncGenerator[AsyncSession, None]:
    """Context manager for acquiring an async database session with auto-rollback."""
    maker = factory or async_session_factory
    async with maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
