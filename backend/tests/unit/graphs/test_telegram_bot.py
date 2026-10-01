"""Telegram bot and approval notifications: taps answer at once, sends never block, resends back off."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from sqlalchemy import func, select
from telegram.error import Forbidden, TimedOut

from app.approvals import service as approvals
from app.approvals import telegram_bot
from app.core.i18n import t
from app.db.engine import read_session, write_session
from app.graphs import runtime
from app.harness.graph import run_action
from app.models.harness import Action, ApprovalRequest, AuditLogEntry
from app.models.tenancy import User
from tests.unit.graphs import test_harness_graph as specs  # registers the fake specs


@pytest_asyncio.fixture(autouse=True)
async def _clean_bot_state() -> AsyncIterator[None]:
    telegram_bot._retries.clear()
    telegram_bot._seen_callbacks.clear()
    yield
    await telegram_bot.drain()
    await approvals.drain_notifications()
    telegram_bot._retries.clear()
    telegram_bot._bot = None


class FakeBot:
    def __init__(self, errors: dict[str, list[Exception]] | None = None) -> None:
        self.errors = errors or {}  # chat id -> errors raised by the next sends, in order
        self.sends: list[str] = []
        self.edits: list[tuple[str, int]] = []

    async def send_message(self, chat_id: str, text: str, reply_markup: Any = None) -> Any:
        self.sends.append(chat_id)
        if self.errors.get(chat_id):
            raise self.errors[chat_id].pop(0)
        return SimpleNamespace(message_id=len(self.sends))

    async def edit_message_text(self, chat_id: str, message_id: int, text: str) -> None:
        self.edits.append((chat_id, message_id))


async def _link(user_id: uuid.UUID, chat_id: str) -> None:
    async with write_session() as s:
        u = await s.get(User, user_id)
        assert u is not None
        u.telegram_chat_id = chat_id


async def _pending(action_id: uuid.UUID) -> ApprovalRequest:
    async with read_session() as s:
        return (await s.execute(select(ApprovalRequest).where(ApprovalRequest.action_id == action_id,
                                                              ApprovalRequest.status == "pending"))).scalar_one()


async def _audits(event: str) -> int:
    async with read_session() as s:
        return int((await s.execute(select(func.count()).select_from(AuditLogEntry)
                                    .where(AuditLogEntry.event == event))).scalar_one())


def _tap(token: str, option_key: str, chat_id: int, answers: list[tuple[str, bool]]) -> Any:
    async def answer(text: str = "", show_alert: bool = False) -> None:
        answers.append((text, show_alert))

    return SimpleNamespace(callback_query=SimpleNamespace(
        id=f"cb-{uuid.uuid4().hex}", data=f"ar:{token}:{option_key}", answer=answer,
        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id))))


async def test_tap_is_answered_before_the_graph_resumes(business: dict[str, Any], monkeypatch: Any) -> None:
    specs.calls.clear()
    await _link(business["users"]["owner"], "501")
    out = await run_action("t_send", {"tag": "tap"}, business["id"])
    req = await _pending(out["action_id"])
    gate = asyncio.Event()
    real_resume = runtime.resume

    async def slow_resume(*args: Any, **kwargs: Any) -> Any:
        await gate.wait()
        return await real_resume(*args, **kwargs)

    monkeypatch.setattr(runtime, "resume", slow_resume)
    answers: list[tuple[str, bool]] = []
    await asyncio.wait_for(telegram_bot.on_callback(_tap(req.request_token, "approve", 501, answers), None), 5)
    assert answers == [("✓", False)]
    assert "execute:tap" not in specs.calls  # the continuation is still waiting
    gate.set()
    await telegram_bot.drain()
    assert specs.calls["execute:tap"] == 1
    async with read_session() as s:
        assert (await s.get(Action, out["action_id"])).stage == "completed"  # type: ignore[union-attr]

    # A second tap on the same request loses the claim and does not resume the graph again.
    again: list[tuple[str, bool]] = []
    await telegram_bot.on_callback(_tap(req.request_token, "approve", 501, again), None)
    await telegram_bot.drain()
    assert again and again[0][1] is True
    assert specs.calls["execute:tap"] == 1


async def test_notifications_never_wait_for_the_channel(business: dict[str, Any]) -> None:
    gate = asyncio.Event()
    seen: list[str] = []

    async def slow(kind: str, req: dict[str, Any]) -> None:
        await gate.wait()
        seen.append(kind)

    async def broken(kind: str, req: dict[str, Any]) -> None:
        raise RuntimeError("channel down")

    approvals.add_notifier(broken)
    approvals.add_notifier(slow)
    await asyncio.wait_for(approvals.post_alert(business_id=business["id"], agent="stock", text_en="Hi",
                                                text_ar="مرحبا"), 5)
    assert seen == []
    gate.set()
    await approvals.drain_notifications()
    assert seen == ["created"]
    # The worker survives a failing notifier and keeps going.
    await approvals.post_alert(business_id=business["id"], agent="stock", text_en="Again", text_ar="مرة أخرى")
    await approvals.drain_notifications()
    assert seen == ["created", "created"]


async def test_failed_sends_back_off_skip_permanent_errors_and_give_up(business: dict[str, Any],
                                                                       monkeypatch: Any) -> None:
    await _link(business["users"]["owner"], "601")
    await _link(business["users"]["manager"], "602")
    bot = FakeBot({"601": [Forbidden("bot was blocked")],
                   "602": [TimedOut() for _ in range(telegram_bot.MAX_SEND_ATTEMPTS)]})
    monkeypatch.setattr(telegram_bot, "_bot", bot)
    rid = await approvals.post_alert(business_id=business["id"], agent="stock", text_en="Hi", text_ar="مرحبا")
    req = approvals.to_dict(await approvals.get(str(rid)))  # type: ignore[arg-type]

    await telegram_bot._send_request(req)
    retry = telegram_bot._retries[req["id"]]
    assert retry.chats == {"602"} and retry.attempt == 1
    first_delay = retry.due - asyncio.get_running_loop().time()
    assert telegram_bot.RETRY_BASE_S - 5 < first_delay <= telegram_bot.RETRY_BASE_S
    assert await _audits("telegram_send_failed") == 2

    await telegram_bot._retry_due()  # not due yet: nothing is sent
    assert bot.sends.count("602") == 1
    delays = []
    for _ in range(telegram_bot.MAX_SEND_ATTEMPTS - 1):
        r = telegram_bot._retries[req["id"]]
        r.due = 0
        await telegram_bot._retry_due()
        if req["id"] in telegram_bot._retries:
            delays.append(telegram_bot._retries[req["id"]].due - asyncio.get_running_loop().time())
    assert all(b > a for a, b in zip(delays, delays[1:], strict=False))  # exponential backoff
    assert req["id"] not in telegram_bot._retries
    assert bot.sends.count("601") == 1  # a blocked bot is never retried
    assert bot.sends.count("602") == telegram_bot.MAX_SEND_ATTEMPTS
    assert await _audits("telegram_send_gave_up") == 1


async def test_resend_reaches_only_the_failed_chat(business: dict[str, Any], monkeypatch: Any) -> None:
    await _link(business["users"]["owner"], "701")
    await _link(business["users"]["manager"], "702")
    bot = FakeBot({"702": [TimedOut()]})
    monkeypatch.setattr(telegram_bot, "_bot", bot)
    rid = await approvals.post_alert(business_id=business["id"], agent="stock", text_en="Hi", text_ar="مرحبا")
    req = approvals.to_dict(await approvals.get(str(rid)))  # type: ignore[arg-type]
    await telegram_bot._send_request(req)
    telegram_bot._retries[req["id"]].due = 0
    await telegram_bot._retry_due()
    assert sorted(bot.sends) == ["701", "702", "702"]
    assert req["id"] not in telegram_bot._retries
    row = await approvals.get(req["id"])
    assert row is not None and sorted(r["chat_id"] for r in row.telegram_message_refs) == ["701", "702"]
    await telegram_bot._mark_resolved(req)
    assert len(bot.edits) == 2


async def test_retry_loop_survives_an_error(monkeypatch: Any) -> None:
    monkeypatch.setattr(telegram_bot, "RETRY_TICK_S", 0.001)
    passes: list[int] = []
    done = asyncio.Event()

    async def flaky() -> None:
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("database is locked")
        done.set()

    monkeypatch.setattr(telegram_bot, "_retry_due", flaky)
    loop_task = asyncio.create_task(telegram_bot._retry_loop())
    try:
        await asyncio.wait_for(done.wait(), 5)
    finally:
        loop_task.cancel()
    assert len(passes) >= 2


def _upload(replies: list[str], downloads: list[dict[str, Any]], chat_id: int) -> Any:
    async def reply_text(text: str, **_: Any) -> None:
        replies.append(text)

    async def download_as_bytearray(**kwargs: Any) -> bytearray:
        downloads.append(kwargs)
        return bytearray(b"%PDF-1.4")

    async def get_file() -> Any:
        return SimpleNamespace(download_as_bytearray=download_as_bytearray)

    msg = SimpleNamespace(caption="", photo=None, reply_text=reply_text,
                          document=SimpleNamespace(get_file=get_file, mime_type="application/pdf", file_name="i.pdf"))
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=msg)


async def test_upload_replies_at_once_and_reads_in_the_background(business: dict[str, Any], monkeypatch: Any) -> None:
    await _link(business["users"]["owner"], "801")
    gate = asyncio.Event()

    async def handler(user: Any, data: bytes, mime: str, name: str, caption: str) -> str:
        await gate.wait()
        return f"read {name} ({len(data)} bytes)"

    monkeypatch.setitem(telegram_bot.UPLOAD_HANDLERS, "document", ("staff", handler))
    replies: list[str] = []
    downloads: list[dict[str, Any]] = []
    await asyncio.wait_for(telegram_bot.on_upload(_upload(replies, downloads, 801), None), 5)
    assert replies == [t("received_reading", "en")]
    gate.set()
    await telegram_bot.drain()
    assert replies[1] == "read i.pdf (8 bytes)"
    assert downloads == [{"read_timeout": telegram_bot.DOWNLOAD_READ_TIMEOUT_S}]

    async def broken(user: Any, data: bytes, mime: str, name: str, caption: str) -> str:
        raise ValueError("unreadable")

    monkeypatch.setitem(telegram_bot.UPLOAD_HANDLERS, "document", ("staff", broken))
    await telegram_bot.on_upload(_upload(replies, downloads, 801), None)
    await telegram_bot.drain()
    assert replies[-1].startswith("Could not read that file")


async def test_link_replies_after_the_write_transaction(business: dict[str, Any]) -> None:
    async with write_session() as s:
        u = await s.get(User, business["users"]["owner"])
        assert u is not None
        u.telegram_link_code = "ABC123"
    replies: list[str] = []

    async def reply_text(text: str, **_: Any) -> None:
        async with asyncio.timeout(2):  # would deadlock if the reply ran inside the write session
            async with write_session():
                pass
        replies.append(text)

    def update(chat_id: int) -> Any:
        return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=SimpleNamespace(reply_text=reply_text))

    await telegram_bot.cmd_start(update(901), SimpleNamespace(args=["wrong"]))
    await telegram_bot.cmd_start(update(901), SimpleNamespace(args=["abc123"]))
    assert replies == [t("link_prompt", "en"), t("linked", "en", user="owner")]
    async with read_session() as s:
        assert (await s.get(User, business["users"]["owner"])).telegram_chat_id == "901"  # type: ignore[union-attr]


async def test_status_skips_a_failing_provider(business: dict[str, Any], monkeypatch: Any) -> None:
    await _link(business["users"]["owner"], "911")

    async def ok(bid: uuid.UUID, lang: str) -> str:
        return "stock ok"

    async def broken(bid: uuid.UUID, lang: str) -> str:
        raise RuntimeError("no data")

    monkeypatch.setattr(telegram_bot, "STATUS_PROVIDERS", [ok, broken, ok])
    replies: list[str] = []

    async def reply_text(text: str, **_: Any) -> None:
        replies.append(text)

    await telegram_bot.cmd_status(SimpleNamespace(effective_chat=SimpleNamespace(id=911),
                                                  message=SimpleNamespace(reply_text=reply_text)), None)
    assert replies == ["stock ok\nstock ok"]


async def test_seen_callbacks_stay_bounded(business: dict[str, Any], monkeypatch: Any) -> None:
    monkeypatch.setattr(telegram_bot, "SEEN_CALLBACKS_MAX", 3)
    answers: list[tuple[str, bool]] = []
    taps = [_tap("nope", "approve", 999, answers) for _ in range(5)]  # an unlinked chat
    for tap in taps:
        await telegram_bot.on_callback(tap, None)
    assert list(telegram_bot._seen_callbacks) == [tap.callback_query.id for tap in taps[2:]]
    await telegram_bot.on_callback(taps[-1], None)  # a repeated callback is answered quietly
    assert answers[-1] == ("", False)
