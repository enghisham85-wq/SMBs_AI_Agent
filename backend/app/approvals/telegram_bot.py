"""Telegram front end for approvals, using long polling."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.approvals import service as approvals
from app.core.auth import can
from app.core.i18n import t
from app.db.engine import read_session, write_session
from app.harness.audit import add_audit, audit
from app.models.harness import ApprovalRequest
from app.models.tenancy import User

log = logging.getLogger(__name__)

# key -> (min_role, handler)
UploadFn = Callable[[User, bytes, str, str, str], Awaitable[str]]
UPLOAD_HANDLERS: dict[str, tuple[str, UploadFn]] = {}
STATUS_PROVIDERS: list[Callable[[uuid.UUID, str], Awaitable[str]]] = []

CONCURRENT_UPDATES = 8
DOWNLOAD_READ_TIMEOUT_S = 120.0  # library default of 5s is too short for 20 MB uploads
RETRY_TICK_S = 15.0
RETRY_BASE_S = 60.0  # doubles each attempt
RETRY_CAP_S = 3600.0
MAX_SEND_ATTEMPTS = 6
SEEN_CALLBACKS_MAX = 1000

_bot: Any = None
_seen_callbacks: dict[str, None] = {}  # used as an ordered set
_tasks: set[asyncio.Task[Any]] = set()


@dataclass
class _Retry:
    chats: set[str]
    attempt: int
    due: float  # loop.time()


_retries: dict[str, _Retry] = {}  # by request id


def register_upload(key: str, min_role: str, fn: UploadFn) -> None:
    UPLOAD_HANDLERS[key] = (min_role, fn)


def _spawn(coro: Coroutine[Any, Any, Any], what: str) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    task.add_done_callback(lambda done: _log_failure(done, what))


def _log_failure(task: asyncio.Task[Any], what: str) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("telegram %s failed", what, exc_info=task.exception())


async def drain() -> None:
    """Test helper: wait for background work from taps and uploads."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def _is_permanent(exc: BaseException) -> bool:
    """Bad chat id or blocked bot; retrying won't help."""
    from telegram.error import BadRequest, Forbidden

    return isinstance(exc, Forbidden | BadRequest)


def _keyboard(req: dict[str, Any], lang: str) -> Any:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    label = "label_ar" if lang == "ar" else "label_en"
    # editing quantities only works in the dashboard
    buttons = [[InlineKeyboardButton(o[label], callback_data=f"ar:{req['request_token']}:{o['key']}")]
               for o in req["options"] if o.get("effect") != "edit"]
    return InlineKeyboardMarkup(buttons)


def _text(req: dict[str, Any], lang: str) -> str:
    text = req["text_ar"] if lang == "ar" else req["text_en"]
    if any(o.get("effect") == "edit" for o in req.get("options", [])):
        text += "\n(لتعديل الكميات استخدم لوحة التحكم)" if lang == "ar" else "\n(To change quantities, use the dashboard.)"
    return text


async def _linked_users(business_id: str, min_role: str) -> list[User]:
    async with read_session() as s:
        users = (await s.execute(select(User).where(User.business_id == uuid.UUID(business_id),
                                                    User.telegram_chat_id.is_not(None),
                                                    User.active.is_(True)))).scalars().all()
    return [u for u in users if can(u.role, min_role)]


async def _send_request(req: dict[str, Any], only_chats: set[str] | None = None, attempt: int = 1) -> None:
    if _bot is None:
        return
    users = [u for u in await _linked_users(req["business_id"], req["required_role"])
             if only_chats is None or u.telegram_chat_id in only_chats]
    pending = req["status"] == "pending"
    sent = await asyncio.gather(
        *(_bot.send_message(chat_id=u.telegram_chat_id, text=_text(req, u.language),
                            reply_markup=_keyboard(req, u.language) if pending else None) for u in users),
        return_exceptions=True)
    refs: list[dict[str, Any]] = []
    failed: list[tuple[User, BaseException]] = []
    for u, msg in zip(users, sent, strict=True):
        if isinstance(msg, BaseException):
            failed.append((u, msg))
        else:
            refs.append({"chat_id": u.telegram_chat_id, "message_id": msg.message_id, "lang": u.language})
    retry_chats = {str(u.telegram_chat_id) for u, exc in failed if not _is_permanent(exc)}
    gave_up = bool(retry_chats) and attempt >= MAX_SEND_ATTEMPTS
    if retry_chats and not gave_up:
        delay = min(RETRY_CAP_S, RETRY_BASE_S * 2 ** (attempt - 1))
        _retries[req["id"]] = _Retry(retry_chats, attempt, asyncio.get_running_loop().time() + delay)
    if not (refs or failed):
        return
    bid = uuid.UUID(req["business_id"])
    # one transaction, since the write lock is app-wide
    async with write_session() as s:
        if refs:
            row = await s.get(ApprovalRequest, uuid.UUID(req["id"]))
            if row is not None:
                row.telegram_message_refs = [*row.telegram_message_refs, *refs]
        for u, exc in failed:
            add_audit(s, "telegram_send_failed", business_id=bid,
                      inputs={"request_id": req["id"], "user": u.username, "attempt": attempt},
                      outputs={"error": repr(exc), "permanent": _is_permanent(exc)})
        if gave_up:
            add_audit(s, "telegram_send_gave_up", business_id=bid,
                      inputs={"request_id": req["id"], "attempts": attempt, "chats": sorted(retry_chats)})


async def _mark_resolved(req: dict[str, Any]) -> None:
    _retries.pop(req["id"], None)
    if _bot is None:
        return
    row = await approvals.get(req["id"])
    if row is None:
        return
    option = next((o for o in row.options if o["key"] == row.resolved_option), None)

    def edited_text(lang: str) -> str:
        label = (option or {}).get("label_ar" if lang == "ar" else "label_en", row.resolved_option or "")
        suffix = f"\n✅ {label} — {row.resolved_via}" if row.status == "resolved" else f"\n⏱ {label}"
        return _text(req, lang) + suffix

    refs = list(row.telegram_message_refs)
    results = await asyncio.gather(
        *(_bot.edit_message_text(chat_id=ref["chat_id"], message_id=ref["message_id"],
                                 text=edited_text(ref.get("lang", "en"))) for ref in refs),
        return_exceptions=True)
    for ref, res in zip(refs, results, strict=True):
        if isinstance(res, BaseException):
            log.debug("could not edit telegram message %s in chat %s", ref["message_id"], ref["chat_id"],
                      exc_info=res)


async def on_approval_event(kind: str, req: dict[str, Any]) -> None:
    if kind == "created":
        await _send_request(req)
    elif kind == "resolved":
        await _mark_resolved(req)


async def _retry_loop() -> None:
    while True:
        await asyncio.sleep(RETRY_TICK_S)
        try:
            await _retry_due()
        except Exception:  # keep the loop alive
            log.exception("telegram retry pass failed")


async def _retry_due() -> None:
    now = asyncio.get_running_loop().time()
    for rid, r in list(_retries.items()):
        if r.due > now:
            continue
        _retries.pop(rid, None)
        try:
            row = await approvals.get(rid)
            if row is not None and row.status == "pending":
                await _send_request(approvals.to_dict(row), only_chats=r.chats, attempt=r.attempt + 1)
        except Exception:
            log.exception("telegram resend of request %s failed", rid)


# Handlers
async def _user_for_chat(chat_id: int) -> User | None:
    async with read_session() as s:
        return (await s.execute(select(User).where(User.telegram_chat_id == str(chat_id),
                                                   User.active.is_(True)))).scalar_one_or_none()


async def cmd_start(update: Any, context: Any) -> None:
    chat_id = update.effective_chat.id
    code = (context.args or [None])[0]
    if not code:
        await update.message.reply_text(t("link_prompt", "en") + "\n" + t("link_prompt", "ar"))
        return
    linked: tuple[str, str] | None = None
    async with write_session() as s:
        user = (await s.execute(select(User).where(User.telegram_link_code == code.strip().upper()))).scalar_one_or_none()
        if user is not None and not (user.telegram_link_expires and user.telegram_link_expires < datetime.now()):
            # a chat links to one user only
            others = (await s.execute(select(User).where(User.telegram_chat_id == str(chat_id),
                                                         User.id != user.id))).scalars()
            for o in others:
                o.telegram_chat_id = None
            user.telegram_chat_id = str(chat_id)
            user.telegram_link_code = None
            add_audit(s, "telegram_linked", business_id=user.business_id, user_id=user.id)
            linked = (user.language, user.username)
    # reply outside the transaction so we don't hold the write lock
    if linked is None:
        await update.message.reply_text(t("link_prompt", "en"))
        return
    await update.message.reply_text(t("linked", linked[0], user=linked[1]))


async def cmd_pending(update: Any, context: Any) -> None:
    user = await _user_for_chat(update.effective_chat.id)
    if user is None:
        await update.message.reply_text(t("link_prompt", "en"))
        return
    async with read_session() as s:
        rows = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.business_id == user.business_id,
                                                              ApprovalRequest.status == "pending"))).scalars().all()
    visible = [approvals.to_dict(r) for r in rows if can(user.role, r.required_role)]
    if not visible:
        await update.message.reply_text("✅")
    for req in visible:
        await update.message.reply_text(_text(req, user.language), reply_markup=_keyboard(req, user.language))


async def cmd_status(update: Any, context: Any) -> None:
    user = await _user_for_chat(update.effective_chat.id)
    if user is None or not can(user.role, "manager"):
        await update.message.reply_text(t("permission_denied", user.language if user else "en"))
        return
    results = await asyncio.gather(*(p(user.business_id, user.language) for p in STATUS_PROVIDERS),
                                   return_exceptions=True)
    parts: list[str] = []
    for res in results:
        if isinstance(res, BaseException):
            log.error("telegram status provider failed", exc_info=res)
        else:
            parts.append(res)
    await update.message.reply_text("\n".join(parts) or "OK")


async def cmd_lang(update: Any, context: Any) -> None:
    user = await _user_for_chat(update.effective_chat.id)
    lang = (context.args or [""])[0]
    if user is None or lang not in ("en", "ar"):
        return
    async with write_session() as s:
        u = await s.get(User, user.id)
        assert u is not None
        u.language = lang
    await update.message.reply_text("✓")


async def on_callback(update: Any, context: Any) -> None:
    query = update.callback_query
    if query.id in _seen_callbacks:  # Telegram retries callbacks
        await query.answer()
        return
    _seen_callbacks[query.id] = None
    if len(_seen_callbacks) > SEEN_CALLBACKS_MAX:
        del _seen_callbacks[next(iter(_seen_callbacks))]
    user = await _user_for_chat(query.message.chat.id)
    if user is None:
        await query.answer(t("link_prompt", "en"), show_alert=True)
        return
    try:
        _, token, option_key = query.data.split(":", 2)
    except ValueError:
        await query.answer()
        return
    # Telegram wants an answer within seconds; the resumed graph can take far longer.
    claim = await approvals.claim(token, option_key, user, "telegram")
    if claim.resume is not None:
        _spawn(claim.resume(), "approval continuation")
    res = claim.result
    if res.status == "permission_denied":
        await query.answer(t("permission_denied", user.language), show_alert=True)
    elif res.status == "already_resolved":
        req = res.request or {}
        await query.answer(t("already_resolved", user.language, channel=req.get("resolved_via"),
                             user=req.get("resolved_by"), option=req.get("resolved_option")), show_alert=True)
    else:
        await query.answer("✓")


async def on_upload(update: Any, context: Any) -> None:
    user = await _user_for_chat(update.effective_chat.id)
    if user is None:
        await update.message.reply_text(t("link_prompt", "en"))
        return
    msg = update.message
    caption = (msg.caption or "").strip()
    key = "delivery" if caption.lower().startswith("/delivery") else "document"
    if key not in UPLOAD_HANDLERS:
        return
    min_role, fn = UPLOAD_HANDLERS[key]
    if not can(user.role, min_role):
        await audit("permission_denied", business_id=user.business_id, user_id=user.id,
                    inputs={"what": f"telegram upload {key}", "role": user.role, "required": min_role})
        await msg.reply_text(t("permission_denied", user.language))
        return
    await msg.reply_text(t("received_reading", user.language))
    _spawn(_read_upload(msg, user, fn, caption), f"upload {key}")


async def _read_upload(msg: Any, user: User, fn: UploadFn, caption: str) -> None:
    try:
        if msg.photo:
            tg_file = await msg.photo[-1].get_file()
            mime, name = "image/jpeg", "photo.jpg"
        else:
            tg_file = await msg.document.get_file()
            mime, name = msg.document.mime_type or "application/octet-stream", msg.document.file_name or "file"
        data = bytes(await tg_file.download_as_bytearray(read_timeout=DOWNLOAD_READ_TIMEOUT_S))
        reply = await fn(user, data, mime, name, caption)
    except Exception:
        log.exception("telegram upload from %s failed", user.username)
        reply = ("تعذّرت قراءة الملف. حاول مرة أخرى أو استخدم لوحة التحكم." if user.language == "ar"
                 else "Could not read that file. Please try again or use the dashboard.")
    await msg.reply_text(reply)


async def run_polling(token: str) -> None:
    global _bot
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

    application = Application.builder().token(token).concurrent_updates(CONCURRENT_UPDATES).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("pending", cmd_pending))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("lang", cmd_lang))
    application.add_handler(CallbackQueryHandler(on_callback, pattern=r"^ar:"))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, on_upload))
    approvals.add_notifier(on_approval_event)
    retry = asyncio.create_task(_retry_loop())
    try:
        async with application:
            await application.start()
            _bot = application.bot
            assert application.updater is not None
            await application.updater.start_polling()
            await asyncio.Event().wait()
    except Exception:
        log.exception("telegram bot stopped")
    finally:
        retry.cancel()
        _bot = None
        if application.updater and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
