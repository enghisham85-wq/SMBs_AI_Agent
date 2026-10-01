"""Async engine, sessions and the serialised write session (research R3)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
# One process-wide lock: API requests, Telegram callbacks, event handlers and the daily run
# never write at the same time, so SQLite never reports "database is locked".
_write_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None
_is_sqlite = True
# Bumped after every commit; read-side caches key on it so any write invalidates them.
_write_generation = 0


def _get_lock() -> asyncio.Lock:
    """The write lock for the running event loop (tests run each case in a fresh loop)."""
    global _write_lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _write_lock is None or _lock_loop is not loop:
        _write_lock = asyncio.Lock()
        _lock_loop = loop
    return _write_lock


def _install_sqlite_pragmas(engine: AsyncEngine, busy_timeout_ms: int, in_memory: bool) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        if not in_memory:
            cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        cur.close()


def init_engine(url: str | None = None) -> AsyncEngine:
    """Create (or replace) the global engine. Tests call this with an in-memory URL."""
    global _engine, _sessionmaker, _is_sqlite
    settings = get_settings()
    url = url or settings.DATABASE_URL
    _is_sqlite = url.startswith("sqlite")
    kwargs: dict[str, Any] = {}
    in_memory = ":memory:" in url or url.rstrip("/").endswith("sqlite+aiosqlite:")
    if _is_sqlite:
        if in_memory:
            kwargs.update(poolclass=StaticPool, connect_args={"check_same_thread": False})
        else:
            db_path = url.split("///", 1)[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(url, **kwargs)
    if _is_sqlite:
        _install_sqlite_pragmas(_engine, settings.SQLITE_BUSY_TIMEOUT_MS, in_memory)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency for read-only work."""
    async with sessionmaker()() as session:
        yield session


@asynccontextmanager
async def read_session() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session


@asynccontextmanager
async def write_session() -> AsyncIterator[AsyncSession]:
    """The only way to commit. Holds the process-wide write lock for the whole transaction.

    After the commit (and after the lock is released) pending outbox events are dispatched,
    so event handlers can open their own write sessions without deadlocking. The caller waits
    for those handlers; a commit that published nothing skips the outbox check.
    """
    global _write_generation
    from app.core import events

    lock = _get_lock() if _is_sqlite else None
    if lock is not None:
        await lock.acquire()
    try:
        async with sessionmaker()() as session:
            async with session.begin():
                yield session
            events.committed(session)
    finally:
        if lock is not None:
            lock.release()
    _write_generation += 1
    await events.dispatch_if_pending()


def write_generation() -> int:
    return _write_generation


def write_lock_held() -> bool:
    return _write_lock is not None and _write_lock.locked()
