"""ApprovalService: owner approvals, questions and alerts for both channels (FR-010, FR-011, R13).

- `ask_owner()` is called from inside any graph node. It creates the request idempotently
  (keyed by thread + gate, because LangGraph re-runs the node on resume) and pauses the graph
  with `interrupt()`.
- `resolve()` is called by the dashboard and the Telegram bot. An atomic
  `UPDATE ... WHERE status='pending'` makes the first answer win; only the winner resumes the graph.
  `claim()` is that first step on its own, for callers (Telegram) that resume the graph later.
- Channel notifiers (Telegram) run on one background worker, so creating or answering a request
  never waits on the network.
- `expire_due()` applies the safe default to overdue requests (never irreversible).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from langgraph.types import interrupt
from sqlalchemy import select, update

from app.core import settings_store
from app.core.auth import can
from app.core.clock import clock_now
from app.db.engine import read_session, write_session
from app.graphs.streaming import broker
from app.harness.action_spec import OwnerAsk
from app.harness.audit import add_audit, audit
from app.models.harness import ApprovalRequest
from app.models.tenancy import User

log = logging.getLogger(__name__)

Notifier = Callable[[str, dict[str, Any]], Awaitable[None]]
_notifiers: list[Notifier] = []
_queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
_worker: asyncio.Task[None] | None = None
# Standalone questions (post_question key "<kind>:<ref>") act on their answer through these handlers.
QuestionHandler = Callable[[dict[str, Any]], Awaitable[None]]
QUESTION_HANDLERS: dict[str, QuestionHandler] = {}


def register_question_handler(kind: str, fn: QuestionHandler) -> None:
    QUESTION_HANDLERS[kind] = fn


class InvalidRequestError(ValueError):
    pass


class Actor(Protocol):
    id: uuid.UUID
    role: str
    username: str


@dataclass
class ResolveResult:
    status: str  # resolved | already_resolved | permission_denied | not_found | invalid_option
    request: dict[str, Any] | None = None
    graph_result: dict[str, Any] | None = None


@dataclass
class Claim:
    """The outcome of the atomic first-answer-wins step. `resume` is set only for the winner."""

    result: ResolveResult
    resume: Callable[[], Coroutine[Any, Any, dict[str, Any] | None]] | None = None


def add_notifier(fn: Notifier) -> None:
    if fn not in _notifiers:
        _notifiers.append(fn)


def clear_notifiers() -> None:
    _notifiers.clear()


def _notify(kind: str, req: dict[str, Any]) -> None:
    """Publish to the dashboard now and queue the channel notifiers; never waits on the network.

    Callers are graph nodes and resolve paths, and a slow or down channel (e.g. Telegram) must not
    hold them up or block the other channel (FR-010a).
    """
    global _queue, _worker
    broker.publish("chat", {"kind": kind, "request": req})
    if not _notifiers:
        return
    loop = asyncio.get_running_loop()
    if _queue is None or _worker is None or _worker.done() or _worker.get_loop() is not loop:
        _queue = asyncio.Queue()
        _worker = loop.create_task(_drain_forever(_queue))
        _worker.add_done_callback(_worker_stopped)
    _queue.put_nowait((kind, req))


async def _drain_forever(queue: asyncio.Queue[tuple[str, dict[str, Any]]]) -> None:
    # One worker keeps a request's events in order: "resolved" edits the messages "created" sent.
    while True:
        kind, req = await queue.get()
        try:
            results = await asyncio.gather(*(fn(kind, req) for fn in list(_notifiers)), return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    log.error("approval notifier failed", exc_info=r)
        finally:
            queue.task_done()


def _worker_stopped(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("approval notifier worker stopped", exc_info=task.exception())


async def drain_notifications() -> None:
    """Wait until every queued channel notification has been handled (tests)."""
    if _queue is not None and _worker is not None and not _worker.done():
        await _queue.join()


def to_dict(r: ApprovalRequest) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "kind": r.kind,
        "agent": r.agent,
        "text_en": r.text_en,
        "text_ar": r.text_ar,
        "options": r.options,
        "required_role": r.required_role,
        "deadline": r.deadline.isoformat() if r.deadline else None,
        "urgency": r.urgency,
        "status": r.status,
        "resolved_option": r.resolved_option,
        "resolved_by": str(r.resolved_by) if r.resolved_by else None,
        "resolved_via": r.resolved_via,
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        "request_token": r.request_token,
        "action_id": str(r.action_id) if r.action_id else None,
        "context": r.context,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "business_id": str(r.business_id),
        "allow_text": bool(r.context.get("allow_text")),
    }


UNSAFE_DEFAULT_EFFECTS = ("approve", "accept", "override", "continue")
REASK_EFFECTS = ("reask",)


def validate_ask(ask: OwnerAsk) -> None:
    """SC-005: every request is answerable in one tap (2-4 options) or one short reply."""
    if ask.kind == "alert":
        if not 1 <= len(ask.options) <= 4:
            raise InvalidRequestError("alerts need 1-4 options")
    elif ask.allow_text and not ask.options:
        pass
    elif not 2 <= len(ask.options) <= 4:
        raise InvalidRequestError("requests need 2-4 options or must accept one short reply")
    for opt in ask.options:
        if not (opt.get("key") and opt.get("label_en") and opt.get("label_ar") and opt.get("effect")):
            raise InvalidRequestError("each option needs key, label_en, label_ar and effect")
    if not (ask.text_en and ask.text_ar):
        raise InvalidRequestError("requests need English and Arabic text")
    # FR-011: a timeout never does anything irreversible.
    default = next((o for o in ask.options if o["key"] == ask.safe_default), None)
    if default is not None and default["effect"] in UNSAFE_DEFAULT_EFFECTS:
        raise InvalidRequestError(f"safe default {ask.safe_default!r} would approve an action")


async def _create(
    *,
    business_id: uuid.UUID,
    ask: OwnerAsk,
    graph_name: str | None,
    thread_id: str | None,
    gate_key: str,
    action_id: uuid.UUID | None,
    agent: str,
) -> tuple[uuid.UUID, bool]:
    validate_ask(ask)
    async with write_session() as s:
        if thread_id is not None:
            existing = (
                await s.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.graph_thread_id == thread_id, ApprovalRequest.gate_key == gate_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id, False
        deadline = None
        if ask.kind != "alert":
            hours = ask.deadline_hours
            if hours is None:
                hours = float(await settings_store.get(business_id, "approval_timeout_hours", s))
            deadline = clock_now() + timedelta(hours=hours)
        context = dict(ask.context)
        context["allow_text"] = ask.allow_text
        req = ApprovalRequest(
            business_id=business_id,
            action_id=action_id,
            kind=ask.kind,
            text_en=ask.text_en,
            text_ar=ask.text_ar,
            options=ask.options,
            required_role=ask.required_role,
            deadline=deadline,
            safe_default=ask.safe_default if ask.kind != "alert" else None,
            urgency=ask.urgency,
            graph_name=graph_name,
            graph_thread_id=thread_id,
            gate_key=gate_key,
            request_token=secrets.token_hex(8),
            context=context,
            agent=agent,
            reask_count=ask.reask,
        )
        s.add(req)
        await s.flush()
        add_audit(s, "approval_requested", business_id=business_id, agent=agent, action_id=action_id,
                  inputs={"request_id": req.id, "kind": ask.kind, "text_en": ask.text_en})
        data = to_dict(req)
    _notify("created", data)
    return uuid.UUID(data["id"]), True


def ask_owner(
    *,
    business_id: uuid.UUID,
    graph_name: str,
    thread_id: str,
    gate_key: str,
    ask: OwnerAsk,
    action_id: uuid.UUID | None = None,
    agent: str = "harness",
) -> Any:
    """Return an awaitable that creates the request (idempotently) and pauses the graph.

    Must be awaited inside a LangGraph node. The resume value is the owner's answer:
    {option_key, effect, edits, text, user_id, via, timed_out}.
    """

    async def _run() -> Any:
        req_id, _ = await _create(
            business_id=business_id,
            ask=ask,
            graph_name=graph_name,
            thread_id=thread_id,
            gate_key=gate_key,
            action_id=action_id,
            agent=agent,
        )
        return interrupt({"approval_request_id": str(req_id), "gate_key": gate_key})

    return _run()


async def post_alert(
    *, business_id: uuid.UUID, agent: str, text_en: str, text_ar: str, context: dict[str, Any] | None = None,
    action_id: uuid.UUID | None = None, urgency: int = 1, dedupe_key: str | None = None,
) -> uuid.UUID:
    """A non-blocking message to the owner with a single OK button."""
    from app.core.i18n import option

    ask = OwnerAsk(
        kind="alert", text_en=text_en, text_ar=text_ar, options=[option("ok", "opt_ok", "ack")],
        required_role="manager", safe_default=None, urgency=urgency, context=context or {},
    )
    req_id, _ = await _create(
        business_id=business_id, ask=ask, graph_name=None,
        thread_id=f"alert:{dedupe_key}" if dedupe_key else None, gate_key="alert",
        action_id=action_id, agent=agent,
    )
    return req_id


async def post_question(
    *, business_id: uuid.UUID, agent: str, ask: OwnerAsk, key: str, action_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A request not tied to a paused graph; the answer is read later with `answer_for()`."""
    req_id, _ = await _create(
        business_id=business_id, ask=ask, graph_name=None, thread_id=f"q:{key}", gate_key="question",
        action_id=action_id, agent=agent,
    )
    return req_id


async def get(ref: str) -> ApprovalRequest | None:
    async with read_session() as s:
        try:
            rid = uuid.UUID(ref)
            row = await s.get(ApprovalRequest, rid)
        except ValueError:
            row = None
        if row is None:
            row = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.request_token == ref))).scalar_one_or_none()
        return row


async def resolve(
    ref: str,
    option_key: str | None,
    actor: Actor,
    via: str,
    edits: dict[str, Any] | None = None,
    text: str | None = None,
) -> ResolveResult:
    """Answer a request; the winner's continuation (question handler, paused graph) runs inline."""
    c = await claim(ref, option_key, actor, via, edits, text)
    if c.resume is not None:
        c.result.graph_result = await c.resume()
    return c.result


async def claim(
    ref: str,
    option_key: str | None,
    actor: Actor,
    via: str,
    edits: dict[str, Any] | None = None,
    text: str | None = None,
) -> Claim:
    """The first step of `resolve()`: the checks and the atomic first-answer-wins update.

    The winner gets `resume`, which notifies the channels and continues the paused graph; a caller
    that must answer quickly (a Telegram tap) can run it in the background.
    """
    req = await get(ref)
    if req is None:
        return Claim(ResolveResult("not_found"))
    if not can(actor.role, req.required_role):
        await audit("permission_denied", business_id=req.business_id, user_id=actor.id,
                    inputs={"what": "resolve_request", "request_id": req.id, "via": via, "role": actor.role,
                            "required": req.required_role})
        return Claim(ResolveResult("permission_denied", to_dict(req)))

    option = next((o for o in req.options if o["key"] == option_key), None)
    if option is None and not (req.context.get("allow_text") and text):
        return Claim(ResolveResult("invalid_option", to_dict(req)))
    if text is not None and len(text) > 100:
        return Claim(ResolveResult("invalid_option", to_dict(req)))

    now = clock_now()
    async with write_session() as s:
        res = await s.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == req.id, ApprovalRequest.status == "pending")
            .values(
                status="resolved",
                resolved_option=option_key or "text",
                resolved_edits=edits,
                resolved_by=actor.id,
                resolved_via=via,
                resolved_at=now,
            )
        )
        won = res.rowcount == 1  # type: ignore[attr-defined]
        if won:
            add_audit(s, "approval_resolved", business_id=req.business_id, user_id=actor.id, action_id=req.action_id,
                      inputs={"request_id": req.id, "option": option_key, "via": via, "edits": edits, "text": text})
    fresh = await get(str(req.id))
    assert fresh is not None
    data = to_dict(fresh)
    if not won:
        return Claim(ResolveResult("already_resolved", data))
    effect = option["effect"] if option else "text"
    graph_name, thread_id, user_id = fresh.graph_name, fresh.graph_thread_id, str(actor.id)

    async def resume() -> dict[str, Any] | None:
        _notify("resolved", data)
        if thread_id and thread_id.startswith("q:"):
            kind = thread_id[2:].split(":", 1)[0]
            handler = QUESTION_HANDLERS.get(kind)
            if handler is not None:
                await handler({**data, "effect": effect, "text": text, "edits": edits or {}})
        if graph_name and thread_id:
            from app.graphs import runtime

            return await runtime.resume(
                graph_name,
                thread_id,
                {
                    "option_key": option_key,
                    "effect": effect,
                    "edits": edits or {},
                    "text": text,
                    "user_id": user_id,
                    "via": via,
                    "timed_out": False,
                },
            )
        return None

    return Claim(ResolveResult("resolved", data), resume)


async def answer_for(key: str) -> dict[str, Any] | None:
    async with read_session() as s:
        row = (
            await s.execute(select(ApprovalRequest).where(ApprovalRequest.graph_thread_id == f"q:{key}"))
        ).scalar_one_or_none()
    return to_dict(row) if row else None


class _System:
    id = uuid.UUID(int=0)
    role = "owner"
    username = "system"


async def withdraw(ref: str, reason: str) -> bool:
    """Take back a pending request that is no longer needed; its paused graph resumes with effect `cancel`."""
    req = await get(ref)
    if req is None:
        return False
    now = clock_now()
    async with write_session() as s:
        res = await s.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == req.id, ApprovalRequest.status == "pending")
            .values(status="superseded", resolved_option="cancel", resolved_via="system", resolved_at=now)
        )
        if res.rowcount != 1:  # type: ignore[attr-defined]
            return False
        add_audit(s, "approval_withdrawn", business_id=req.business_id, action_id=req.action_id,
                  inputs={"request_id": req.id, "reason": reason})
    fresh = await get(str(req.id))
    assert fresh is not None
    _notify("resolved", to_dict(fresh))
    if req.graph_name and req.graph_thread_id:
        from app.graphs import runtime

        await runtime.resume(req.graph_name, req.graph_thread_id,
                             {"option_key": "cancel", "effect": "cancel", "edits": {}, "text": reason,
                              "user_id": str(_System.id), "via": "system", "timed_out": False})
    return True


async def expire_due(business_id: uuid.UUID) -> int:
    """Apply the safe default to overdue requests (FR-011). Returns how many expired.

    The request is marked `timed_out` and its graph resumes with the safe default, which is never
    irreversible. When that default is "ask again" the question is re-sent with urgency + 1 and
    reask_count + 1: a paused action's own graph re-asks at its gate; a standalone question is copied here.
    """
    now = clock_now()
    async with read_session() as s:
        due = (
            await s.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.business_id == business_id,
                    ApprovalRequest.status == "pending",
                    ApprovalRequest.kind != "alert",
                    ApprovalRequest.deadline.is_not(None),
                    ApprovalRequest.deadline <= now,
                )
            )
        ).scalars().all()
    count = 0
    for req in due:
        async with write_session() as s:
            res = await s.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == req.id, ApprovalRequest.status == "pending")
                .values(status="timed_out", resolved_option=req.safe_default, resolved_via="system", resolved_at=now)
            )
            if res.rowcount != 1:  # type: ignore[attr-defined]
                continue
            add_audit(s, "approval_timed_out", business_id=business_id, action_id=req.action_id,
                      inputs={"request_id": req.id, "safe_default": req.safe_default})
        count += 1
        fresh = await get(str(req.id))
        assert fresh is not None
        _notify("resolved", to_dict(fresh))
        effect = next((o["effect"] for o in req.options if o["key"] == req.safe_default), req.safe_default or "reask")
        if req.graph_name and req.graph_thread_id:
            from app.graphs import runtime

            await runtime.resume(
                req.graph_name,
                req.graph_thread_id,
                {"option_key": req.safe_default, "effect": effect, "edits": {}, "text": None,
                 "user_id": str(_System.id), "via": "system", "timed_out": True},
            )
        elif effect in REASK_EFFECTS:
            await _reask_standalone(req)
        elif req.graph_thread_id and req.graph_thread_id.startswith("q:"):
            kind = req.graph_thread_id[2:].split(":", 1)[0]
            handler = QUESTION_HANDLERS.get(kind)
            if handler is not None:
                await handler({**to_dict(fresh), "effect": effect, "text": None, "edits": {}, "timed_out": True})
    return count


async def _reask_standalone(req: ApprovalRequest) -> None:
    """Re-send a question that is not tied to a paused graph, one level more urgent."""
    ask = OwnerAsk(kind=req.kind, text_en=req.text_en, text_ar=req.text_ar, options=req.options,
                   required_role=req.required_role, safe_default=req.safe_default, urgency=min(3, req.urgency + 1),
                   context={k: v for k, v in (req.context or {}).items() if k != "allow_text"},
                   allow_text=bool((req.context or {}).get("allow_text")), reask=req.reask_count + 1)
    thread = f"{req.graph_thread_id}:r{req.reask_count + 1}" if req.graph_thread_id else None
    await _create(business_id=req.business_id, ask=ask, graph_name=None, thread_id=thread,
                  gate_key=req.gate_key, action_id=req.action_id, agent=req.agent)


async def user_actor(user_id: uuid.UUID) -> Actor | None:
    async with read_session() as s:
        u = await s.get(User, user_id)
    return u  # type: ignore[return-value]
