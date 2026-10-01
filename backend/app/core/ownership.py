"""Which agent is acting right now (FR-044: each agent writes only its own records).

Harness nodes run as their action's agent; an event handler runs as its consuming agent. The
ownership guard test listens to database flushes and checks every write against this.
"""

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
