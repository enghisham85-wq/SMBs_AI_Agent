"""In-process pub/sub for Server-Sent Events (chat panel and live harness pipeline)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from app.harness.audit import jsonable


class Broker:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, channel: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subs[channel].add(q)
        return q

    def unsubscribe(self, channel: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs[channel].discard(q)

    def publish(self, channel: str, data: dict[str, Any]) -> None:
        payload = jsonable(data)
        for q in list(self._subs[channel]):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # slow client; drop rather than block the agents


broker = Broker()
