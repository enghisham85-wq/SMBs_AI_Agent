"""Dev/test guard that turns any JSON response with Money but no `data_as_of` into a 500."""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)


def has_figures(value: Any) -> bool:
    if isinstance(value, dict):
        if "amount_minor" in value and "currency" in value:
            return True
        return any(has_figures(v) for v in value.values())
    if isinstance(value, list):
        return any(has_figures(v) for v in value)
    return False


def missing_freshness(body: Any) -> bool:
    return isinstance(body, dict) and "error" not in body and "data_as_of" not in body and has_figures(body)


class FreshnessCheck:
    """Buffers successful JSON GET responses and checks them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return
        start: Message | None = None
        chunks: list[bytes] = []
        passthrough = False

        async def capture(message: Message) -> None:
            nonlocal start, passthrough
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers") or [])
                ctype = headers.get(b"content-type", b"").decode()
                if message["status"] != 200 or not ctype.startswith("application/json"):
                    passthrough = True
                    await send(message)
                else:
                    start = message
                return
            if passthrough:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            raw = b"".join(chunks)
            try:
                bad = missing_freshness(json.loads(raw))
            except ValueError:
                bad = False
            assert start is not None
            if bad:
                log.error("response for %s has figures but no data_as_of", scope["path"])
                raw = json.dumps({"error": {"code": "missing_data_as_of", "path": scope["path"],
                                            "message_en": "This response shows figures without data_as_of.",
                                            "message_ar": "هذه الاستجابة تعرض أرقاماً دون وقت تحديث البيانات."}}).encode()
                start = {**start, "status": 500,
                         "headers": [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
                         + [(b"content-length", str(len(raw)).encode())]}
            await send(start)
            await send({"type": "http.response.body", "body": raw})

        await self.app(scope, receive, capture)
