"""Tracks the active Chaos injection so incidents it causes are linked to it."""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_current: ContextVar[uuid.UUID | None] = ContextVar("chaos_injection", default=None)
_active: dict[str, uuid.UUID] = {}


def current_injection() -> uuid.UUID | None:
    return _current.get() or _active.get("latest")


def set_active(injection_id: uuid.UUID | None) -> None:
    if injection_id is None:
        _active.pop("latest", None)
    else:
        _active["latest"] = injection_id
    _current.set(injection_id)
