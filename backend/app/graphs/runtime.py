"""LangGraph runtime: checkpointer, graph registry, start / resume.

Checkpoints get their own SQLite file so they don't contend with business-data writes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph
from langgraph.types import Command

from app.config import get_settings
from app.graphs.streaming import broker

log = logging.getLogger(__name__)

_saver: BaseCheckpointSaver[Any] | None = None
_conn: Any = None
_builders: dict[str, Callable[[], StateGraph[Any]]] = {}
_compiled: dict[str, Any] = {}


async def init(saver: BaseCheckpointSaver[Any] | None = None) -> None:
    """Open the checkpointer. Call once at startup (or per test)."""
    global _saver, _conn
    _compiled.clear()
    if saver is not None:
        _saver = saver
        return
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    settings = get_settings()
    path = Path(settings.CHECKPOINT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(str(path))
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute(f"PRAGMA busy_timeout={int(settings.SQLITE_BUSY_TIMEOUT_MS)}")
    _saver = AsyncSqliteSaver(_conn)
    await _saver.setup()


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def register(name: str, builder: Callable[[], StateGraph[Any]]) -> None:
    _builders[name] = builder
    _compiled.pop(name, None)


def get(name: str) -> Any:
    if name not in _compiled:
        if _saver is None:
            raise RuntimeError("graph runtime not initialised; call runtime.init() first")
        _compiled[name] = _builders[name]().compile(checkpointer=_saver)
    return _compiled[name]


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}


async def _run(name: str, payload: Any, thread_id: str) -> dict[str, Any]:
    graph = get(name)
    config = _config(thread_id)
    async for update in graph.astream(payload, config=config, stream_mode="updates"):
        for node, delta in update.items():
            if node == "__interrupt__":
                continue
            broker.publish("harness", {"graph": name, "thread_id": thread_id, "node": node, "update": delta})
    snapshot = await graph.aget_state(config)
    interrupts = [i.value for task in snapshot.tasks for i in task.interrupts]
    return {"values": dict(snapshot.values), "interrupted": bool(snapshot.next), "interrupts": interrupts}


async def start(name: str, state: dict[str, Any], thread_id: str) -> dict[str, Any]:
    return await _run(name, state, thread_id)


async def resume(name: str, thread_id: str, value: Any) -> dict[str, Any]:
    """Continue a paused thread with the owner's answer."""
    return await _run(name, Command(resume=value), thread_id)


async def state(name: str, thread_id: str) -> dict[str, Any]:
    snapshot = await get(name).aget_state(_config(thread_id))
    return {"values": dict(snapshot.values), "next": list(snapshot.next)}
