"""Transactional outbox and in-process dispatch (research R11, contracts/events.md).

`publish()` writes an Event row in the caller's transaction. After the transaction commits,
`write_session()` calls `dispatch_pending()`, which delivers each event once per subscribed
consumer and records an EventDelivery. Handlers are idempotent on (event, consumer).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.harness.audit import jsonable

log = logging.getLogger(__name__)

# Required payload fields per event type (contracts/events.md).
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
}

Handler = Callable[[dict[str, Any]], Awaitable[None]]
_subscribers: dict[str, list[tuple[str, Handler]]] = {}
_dispatching = False


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


async def dispatch_pending() -> int:
    """Deliver undispatched events to their consumers. Returns the number of deliveries made.

    Re-entrant calls (a handler's own write_session) return immediately; the outer loop picks up
    any events those handlers publish.
    """
    global _dispatching
    if _dispatching or not _subscribers:
        return 0
    from app.db.engine import read_session, write_session
    from app.models.events import Event, EventDelivery

    _dispatching = True
    delivered = 0
    try:
        while True:
            async with read_session() as s:
                events = list(
                    (await s.execute(select(Event).where(Event.dispatched.is_(False)).order_by(Event.occurred_at)))
                    .scalars()
                    .all()
                )
            if not events:
                break
            for ev in events:
                for consumer, handler in list(_subscribers.get(ev.type, [])):
                    async with read_session() as s:
                        done = (
                            await s.execute(
                                select(EventDelivery.id).where(
                                    EventDelivery.event_id == ev.id, EventDelivery.consumer == consumer
                                )
                            )
                        ).first()
                    if done:
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
                    status, error = "handled", None
                    try:
                        await handler(envelope)
                    except Exception as exc:  # a failing consumer must not block the others
                        log.exception("event handler %s failed for %s", consumer, ev.type)
                        status, error = "failed", repr(exc)
                    async with write_session() as s:
                        s.add(
                            EventDelivery(
                                business_id=ev.business_id,
                                event_id=ev.id,
                                consumer=consumer,
                                status=status,
                                error=error,
                            )
                        )
                    delivered += 1
                async with write_session() as s:
                    row = await s.get(Event, ev.id)
                    if row is not None:
                        row.dispatched = True
    finally:
        _dispatching = False
    return delivered
