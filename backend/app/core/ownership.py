"""Tracks which agent is acting, so tests can check each agent only writes its own records."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

AGENTS = ("stock", "cashflow", "accountant")
_acting: ContextVar[str | None] = ContextVar("acting_agent", default=None)


def acting_agent() -> str | None:
    return _acting.get()


@contextmanager
def acting_as(agent: str | None) -> Iterator[None]:
    token = _acting.set(agent if agent in AGENTS else None)
    try:
        yield
    finally:
        _acting.reset(token)
