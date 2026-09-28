"""Telegram bot: a second front end to ApprovalService (contracts/telegram-bot.md, research R13).

Long polling (no public webhook needed). Holds no business state: every answer goes through
ApprovalService.resolve(), so role checks and first-answer-wins are identical to the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
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

# Upload handlers registered by the stories: key -> (min_role, fn(user, file_bytes, mime, name, caption) -> reply)
UploadFn = Callable[[User, bytes, str, str, str], Awaitable[str]]
UPLOAD_HANDLERS: dict[str, tuple[str, UploadFn]] = {}
STATUS_PROVIDERS: list[Callable[[uuid.UUID, str], Awaitable[str]]] = []

_bot: Any = None
_seen_callbacks: set[str] = set()
_retry_queue: list[str] = []  # request ids whose send failed


def register_upload(key: str, min_role: str, fn: UploadFn) -> None:
    UPLOAD_HANDLERS[key] = (min_role, fn)


def _keyboard(req: dict[str, Any], lang: str) -> Any:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    label = "label_ar" if lang == "ar" else "label_en"
    buttons = [[InlineKeyboardButton(o[label], callback_data=f"ar:{req['request_token']}:{o['key']}")]
               for o in req["options"]]
    return InlineKeyboardMarkup(buttons)


def _text(req: dict[str, Any], lang: str) -> str:
    return req["text_ar"] if lang == "ar" else req["text_en"]


async def _linked_users(business_id: str, min_role: str) -> list[User]:
    async with read_session() as s:
        users = (await s.execute(select(User).where(User.business_id == uuid.UUID(business_id),
                                                    User.telegram_chat_id.is_not(None),
                                                    User.active.is_(True)))).scalars().all()
    return [u for u in users if can(u.role, min_role)]


async def _send_request(req: dict[str, Any]) -> None:
    if _bot is None:
        return
    refs: list[dict[str, Any]] = []
    failed = False
    for u in await _linked_users(req["business_id"], req["required_role"]):
        try:
            msg = await _bot.send_message(chat_id=u.telegram_chat_id, text=_text(req, u.language),
                                          reply_markup=_keyboard(req, u.language) if req["status"] == "pending" else None)
            refs.append({"chat_id": u.telegram_chat_id, "message_id": msg.message_id, "lang": u.language})
        except Exception as exc:
            failed = True
            await audit("telegram_send_failed", business_id=uuid.UUID(req["business_id"]),
                        inputs={"request_id": req["id"], "user": u.username}, outputs={"error": repr(exc)})
    if refs:
        async with write_session() as s:
            row = await s.get(ApprovalRequest, uuid.UUID(req["id"]))
            if row is not None:
                row.telegram_message_refs = [*row.telegram_message_refs, *refs]
    if failed and req["id"] not in _retry_queue:
        _retry_queue.append(req["id"])  # stays answerable in the dashboard meanwhile (FR-010a)


async def _mark_resolved(req: dict[str, Any]) -> None:
    if _bot is None:
        return
    row = await approvals.get(req["id"])
    if row is None:
        return
    for ref in row.telegram_message_refs:
        lang = ref.get("lang", "en")
        option = next((o for o in row.options if o["key"] == row.resolved_option), None)
        label = (option or {}).get("label_ar" if lang == "ar" else "label_en", row.resolved_option or "")
        suffix = (f"\n✅ {label} — {row.resolved_via}" if row.status == "resolved"
                  else f"\n⏱ {label}")
        try:
            await _bot.edit_message_text(chat_id=ref["chat_id"], message_id=ref["message_id"],
                                         text=_text(req, lang) + suffix)
        except Exception:
            log.debug("could not edit telegram message", exc_info=True)


async def on_approval_event(kind: str, req: dict[str, Any]) -> None:
    if kind == "created":
        await _send_request(req)
    elif kind == "resolved":
        await _mark_resolved(req)


# --------------------------------------------------------------------- handlers
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
    async with write_session() as s:
        user = (await s.execute(select(User).where(User.telegram_link_code == code.strip().upper()))).scalar_one_or_none()
        if user is None or (user.telegram_link_expires and user.telegram_link_expires < datetime.now()):
            await update.message.reply_text(t("link_prompt", "en"))
            return
        # One chat <-> one user: unlink any other user bound to this chat first.
        others = (await s.execute(select(User).where(User.telegram_chat_id == str(chat_id), User.id != user.id))).scalars()
        for o in others:
            o.telegram_chat_id = None
        user.telegram_chat_id = str(chat_id)
        user.telegram_link_code = None
        add_audit(s, "telegram_linked", business_id=user.business_id, user_id=user.id)
        lang, name = user.language, user.username
    await update.message.reply_text(t("linked", lang, user=name))


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
    parts = [await p(user.business_id, user.language) for p in STATUS_PROVIDERS]
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
    if query.id in _seen_callbacks:  # Telegram may retry callbacks
        await query.answer()
        return
    _seen_callbacks.add(query.id)
    user = await _user_for_chat(query.message.chat.id)
    if user is None:
        await query.answer(t("link_prompt", "en"), show_alert=True)
        return
    try:
        _, token, option_key = query.data.split(":", 2)
    except ValueError:
        await query.answer()
        return
    res = await approvals.resolve(token, option_key, user, "telegram")
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
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()
        mime, name = "image/jpeg", "photo.jpg"
    else:
        tg_file = await msg.document.get_file()
        mime, name = msg.document.mime_type or "application/octet-stream", msg.document.file_name or "file"
    data = bytes(await tg_file.download_as_bytearray())
    await msg.reply_text(t("received_reading", user.language))
    reply = await fn(user, data, mime, name, caption)
    await msg.reply_text(reply)


async def _retry_loop() -> None:
    while True:
        await asyncio.sleep(60)
        pending = list(_retry_queue)
        _retry_queue.clear()
        for rid in pending:
            row = await approvals.get(rid)
            if row is not None and row.status == "pending" and not row.telegram_message_refs:
                await _send_request(approvals.to_dict(row))


async def run_polling(token: str) -> None:
    """Start the bot with long polling until cancelled."""
    global _bot
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

    application = Application.builder().token(token).build()
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
