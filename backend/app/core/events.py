"""Transactional outbox with inline, in-process dispatch after commit."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.harness.audit import jsonable

log = logging.getLogger(__name__)

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "po.drafted": ("po_id", "supplier_id", "total", "expected_date", "is_critical"),
    "budget.check_result": ("po_id", "within_budget", "remaining_budget", "recommendation"),
    "po.approved_sent": ("po_id", "lines", "total", "sent_at"),
    "delivery.received": ("delivery_id", "po_id", "lines", "discrepancies"),
    "invoice.posted": ("invoice_id", "supplier_id", "due_date", "total", "unit_costs"),
    "invoice.held": ("invoice_id", "reason"),
    "customer_payment.received": ("receivable_invoice_id", "amount", "bank_txn_id"),
    "budget.updated": ("week_start", "amount", "tightened", "reason"),
    "shortfall.predicted": ("run_id", "gap_amount", "gap_date", "days_to_act", "plan_id"),
    "price.changed": ("supplier_id", "item_id", "old_price", "new_price", "pct"),
    "stock_valuation.mismatch": ("ledger_value", "stock_value", "difference"),
    "supplier.performance_updated": ("supplier_id", "reliability_score", "observed_lead_time"),
    "incident.opened": ("incident_id", "agent", "type", "summary"),
    "incident.resolved": ("incident_id", "agent", "type", "summary"),
    "rule.activated": ("rule_id", "agent", "kind", "trigger"),
    "rule.deactivated": ("rule_id", "agent", "kind", "trigger"),
    "receivable.created": ("receivable_invoice_id", "total", "due_date"),
    "budget.conflict_resolved": ("po_id", "decision", "recommendation", "decided_by"),
}

Handler = Callable[[dict[str, Any]], Awaitable[None]]
_subscribers: dict[str, list[tuple[str, Handler]]] = {}
_dispatching = False
# Lets the post-commit hook skip the outbox query when nothing was published. Starts True for leftovers.
_maybe_pending = True
_PUBLISHED = "events_published"
_BATCH = 500


class EventPayloadError(ValueError):
    pass


def subscribe(event_type: str, consumer: str, handler: Handler) -> None:
    subs = _subscribers.setdefault(event_type, [])
    if not any(name == consumer for name, _ in subs):
        subs.append((consumer, handler))


def clear_subscribers() -> None:
    _subscribers.clear()


def publish(
    session: AsyncSession,
    event_type: str,
    payload: dict[str, Any],
    *,
    producer: str,
    business_id: uuid.UUID,
    action_id: uuid.UUID | None = None,
    version: int = 1,
) -> None:
    from app.models.events import Event

    missing = [f for f in REQUIRED_FIELDS.get(event_type, ()) if f not in payload]
    if missing:
        raise EventPayloadError(f"{event_type} missing fields: {missing}")
    session.add(
        Event(
            business_id=business_id,
            type=event_type,
            version=version,
            producer=producer,
            payload=jsonable(payload),
            action_id=action_id,
        )
    )
    _mark_pending(session)


def _mark_pending(session: AsyncSession) -> None:
    session.info[_PUBLISHED] = True


def committed(session: AsyncSession) -> None:
    """Called by write_session after each commit."""
    global _maybe_pending
    if session.info.get(_PUBLISHED):
        _maybe_pending = True


async def dispatch_if_pending() -> int:
    if not _maybe_pending:
        return 0
    return await dispatch_pending()


async def dispatch_pending() -> int:
    """Deliver pending events inline, so handler effects are visible when the caller's write returns."""
    global _dispatching, _maybe_pending
    if _dispatching or not _subscribers:
        return 0
    from app.db.engine import read_session, write_session
    from app.models.events import Event, EventDelivery

    _dispatching = True
    delivered = 0
    try:
        while True:
            # clear before querying so a commit during the query sets it again
            _maybe_pending = False
            async with read_session() as s:
                events = list(
                    (await s.execute(select(Event).where(Event.dispatched.is_(False))
                                     .order_by(Event.occurred_at).limit(_BATCH)))
                    .scalars()
                    .all()
                )
                done = {
                    (eid, consumer)
                    for eid, consumer in (await s.execute(
                        select(EventDelivery.event_id, EventDelivery.consumer)
                        .where(EventDelivery.event_id.in_([e.id for e in events]))
                    )).all()
                } if events else set()
            if not events:
                if _maybe_pending:
                    continue
                break
            idle: list[uuid.UUID] = []
            for ev in events:
                todo = [(c, h) for c, h in list(_subscribers.get(ev.type, [])) if (ev.id, c) not in done]
                if not todo:
                    idle.append(ev.id)
                    continue
                envelope = {
                    "event_id": str(ev.id),
                    "type": ev.type,
                    "version": ev.version,
                    "business_id": str(ev.business_id),
                    "producer": ev.producer,
                    "action_id": str(ev.action_id) if ev.action_id else None,
                    "occurred_at": ev.occurred_at.isoformat(),
                    "payload": ev.payload,
                }
                results: list[EventDelivery] = []
                for consumer, handler in todo:
                    status, error = "handled", None
                    try:
                        from app.core.ownership import acting_as

                        with acting_as(consumer.split(":", 1)[0]):
                            await handler(dict(envelope))
                    except Exception as exc:  # don't let one consumer block the rest
                        log.exception("event handler %s failed for %s", consumer, ev.type)
                        status, error = "failed", repr(exc)
                    results.append(EventDelivery(business_id=ev.business_id, event_id=ev.id, consumer=consumer,
                                                 status=status, error=error))
                    delivered += 1
                # Deliveries and the flag commit together, so a crash re-runs the whole event.
                async with write_session() as s:
                    s.add_all(results)
                    await s.execute(update(Event).where(Event.id == ev.id).values(dispatched=True))
            if idle:
                async with write_session() as s:
                    await s.execute(update(Event).where(Event.id.in_(idle)).values(dispatched=True))
    finally:
        _dispatching = False
    return delivered
