"""Async engine / session plumbing. Commits are explicit (services call `await db.commit()`),
so a response is never sent before its data is durable."""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import Settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    global _engine, _sessionmaker
    kwargs: dict = {"pool_pre_ping": True}
    if settings.is_sqlite:
        kwargs = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in settings.database_url:  # one shared connection, or every checkout would see an empty database
            kwargs["poolclass"] = StaticPool
    else:
        kwargs.update(pool_size=10, max_overflow=10)
    _engine = create_async_engine(settings.database_url, **kwargs)
    if settings.is_sqlite:
        @event.listens_for(_engine.sync_engine, "connect")
        def _fk_on(dbapi_conn, _):  # SQLite ignores FKs unless asked
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)
    return _engine


def engine() -> AsyncEngine:
    assert _engine is not None, "init_engine() was not called"
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    assert _sessionmaker is not None, "init_engine() was not called"
    return _sessionmaker


async def get_db() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        try:
            yield session
        finally:
            await session.rollback()  # no-op after a commit; discards anything uncommitted on error


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = _sessionmaker = None
