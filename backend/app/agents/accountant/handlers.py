"""Accountant Agent event handlers (T110, contracts/events.md). Each runs as the Accountant Agent.

Orders and deliveries stay in the Stock Agent's tables; the Accountant reads them for the three-way
match. These handlers note that they are available and keep a price context used when checking
supplier invoices.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core import clock, settings_store
from app.db.engine import write_session
from app.harness.audit import add_audit

PRICE_CONTEXT = "accountant_price_context"


def _bid(env: dict[str, Any]) -> uuid.UUID:
    return uuid.UUID(env["business_id"])


async def on_po_approved_sent(env: dict[str, Any]) -> None:
    """An order was sent: it is now open for matching against the supplier's invoice."""
    async with write_session() as s:
        add_audit(s, "po_open_for_matching", business_id=_bid(env), agent="accountant",
                  inputs={"po_id": env["payload"]["po_id"], "total": env["payload"]["total"]})


async def on_delivery_received(env: dict[str, Any]) -> None:
    """Delivered quantities and note prices are the third leg of the three-way match."""
    p = env["payload"]
    async with write_session() as s:
        add_audit(s, "delivery_ready_for_match", business_id=_bid(env), agent="accountant",
                  inputs={"po_id": p["po_id"], "delivery_id": p["delivery_id"], "discrepancies": p.get("discrepancies", [])})


async def on_price_changed(env: dict[str, Any]) -> None:
    """Remember the latest agreed price per supplier and item (context for invoice price checks)."""
    bid = _bid(env)
    p = env["payload"]
    async with write_session() as s:
        ctx = dict(await settings_store.get_all(bid, s)).get(PRICE_CONTEXT) or {}
        ctx[f"{p.get('supplier_id')}:{p['item_id']}"] = {"price": p["new_price"], "previous": p["old_price"], "pct": p["pct"],
                                                         "on": clock.today().isoformat()}
        await settings_store.put(s, bid, PRICE_CONTEXT, ctx)


def register() -> None:
    from app.core.events import subscribe

    subscribe("po.approved_sent", "accountant", on_po_approved_sent)
    subscribe("delivery.received", "accountant", on_delivery_received)
    subscribe("price.changed", "accountant", on_price_changed)
