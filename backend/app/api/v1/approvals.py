"""Approvals and the chat stream, shared with Telegram."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from app.api.common import J, with_freshness
from app.approvals import service as approvals
from app.core.auth import ROLE_RANK, CurrentUser, RequireStaff
from app.core.errors import AppError
from app.core.i18n import t
from app.db.engine import read_session
from app.graphs.streaming import broker
from app.models.harness import ApprovalRequest

router = APIRouter(tags=["approvals"])


@router.get("/approvals")
async def list_approvals(status: str | None = "pending", user: CurrentUser = RequireStaff) -> Response:
    async with read_session() as s:
        q = select(ApprovalRequest).where(ApprovalRequest.business_id == user.business_id)
        if status:
            q = q.where(ApprovalRequest.status == status)
        rows = (await s.execute(q.order_by(ApprovalRequest.urgency.desc(), ApprovalRequest.created_at.desc()).limit(200))).scalars().all()
    visible = [approvals.to_dict(r) for r in rows if ROLE_RANK[user.role] >= ROLE_RANK.get(r.required_role, 1)]
    return J(with_freshness({"approvals": visible}, {"approvals": max((r.updated_at for r in rows), default=None)}))


class ResolveIn(BaseModel):
    option_key: str | None = None
    edits: dict[str, Any] | None = None
    text: str | None = Field(default=None, max_length=100)


@router.post("/approvals/{request_id}/resolve")
async def resolve(request_id: uuid.UUID, body: ResolveIn, user: CurrentUser = RequireStaff) -> Response:
    res = await approvals.resolve(str(request_id), body.option_key, user, "dashboard", body.edits, body.text)
    if res.status == "not_found":
        raise AppError(404, "not_found")
    if res.status == "permission_denied":
        raise AppError(403, "permission_denied")
    if res.status == "invalid_option":
        raise AppError(422, "invalid_option", message_en="That option is not valid for this request.",
                       message_ar="هذا الخيار غير صالح لهذا الطلب.")
    if res.status == "already_resolved":
        req = res.request or {}
        opt = req.get("resolved_option")
        raise AppError(409, "already_resolved", channel=req.get("resolved_via"), user=req.get("resolved_by"),
                       option=opt, extra={"request": req})
    return J({"status": "resolved", "request": res.request})


@router.get("/chat/stream")
async def chat_stream(request: Request, user: CurrentUser = RequireStaff) -> EventSourceResponse:
    queue = broker.subscribe("chat")

    async def gen() -> AsyncIterator[dict[str, str]]:
        try:
            yield {"event": "hello", "data": json.dumps({"lang": user.language, "text": t("opt_ok", user.language)})}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                req = msg.get("request", {})
                if req.get("business_id") != str(user.business_id):
                    continue
                if ROLE_RANK[user.role] < ROLE_RANK.get(req.get("required_role", "manager"), 1):
                    continue
                yield {"event": msg.get("kind", "message"), "data": json.dumps(msg, ensure_ascii=False)}
        finally:
            broker.unsubscribe("chat", queue)

    return EventSourceResponse(gen())
