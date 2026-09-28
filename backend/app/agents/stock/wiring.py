"""Registers the Stock Agent: action specs, graphs, daily steps, seed data and Telegram handlers."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update

from app.agents.stock import action_specs, graphs
from app.db.engine import read_session, write_session
from app.models.finance_master import Sale

# Opening stock in days of average use; milk is low so the demo shows a stockout warning within ~3 days.
OPENING_COVER_DAYS = {
    "Milk": Decimal("5.5"),
    "Coffee beans": Decimal("9"),
    "Tea bags": Decimal("25"),
    "Croissant (baked)": Decimal("2"),
    "Chocolate muffin (baked)": Decimal("2.5"),
    "Bread roll": Decimal("2"),
    "White cheese": Decimal("8"),
    "Oranges": Decimal("5"),
    "Water bottle 600 ml": Decimal("20"),
    "Paper cup": Decimal("15"),
}


async def seed_opening_stock(business_id: uuid.UUID, start: date, ids: dict[str, Any]) -> None:
    """Opening stock from recent average usage; history sales are marked as already applied."""
    from app.agents.stock import tracking
    from app.models.master import Item

    async with write_session() as s:
        usage: dict[uuid.UUID, Decimal] = {}
        window = 14
        for i in range(1, window + 1):
            d = start - timedelta(days=i)
            res = await tracking.apply_sales(s, business_id, d)  # computes usage (and marks the sales applied)
            for item_id, qty in res.deducted.items():
                usage[uuid.UUID(item_id)] = usage.get(uuid.UUID(item_id), Decimal(0)) + qty
        # Undo those deductions: history is before the opening count.
        from sqlalchemy import delete

        from app.models.stock_ops import StockLevel, StockMovement

        await s.execute(delete(StockMovement).where(StockMovement.business_id == business_id))
        await s.execute(delete(StockLevel).where(StockLevel.business_id == business_id))
        await s.execute(update(Sale).where(Sale.business_id == business_id, Sale.date < start).values(stock_applied=True))
        items = (await s.execute(select(Item).where(Item.business_id == business_id, Item.is_ingredient.is_(True)))).scalars().all()
        for item in items:
            avg = usage.get(item.id, Decimal(0)) / window
            cover = OPENING_COVER_DAYS.get(item.name_en, Decimal(10))
            qty = (avg * cover).quantize(Decimal("0.01")) if avg > 0 else Decimal(10)
            await tracking.move(s, business_id, item.id, qty, "count_correction", start - timedelta(days=1),
                                reason="opening stock count", source="seed")


async def _late_answer(req: dict[str, Any]) -> None:
    """Late delivery question: 'Reorder from another supplier' drafts an order with the cheapest alternative."""
    if req.get("effect") != "reorder_other":
        return
    from app.agents.stock.action_specs import cheapest_alternative, po_details
    from app.core import clock
    from app.harness.graph import run_action

    po_id = uuid.UUID(req["context"]["po_id"])
    d = await po_details(po_id)
    alt = await cheapest_alternative(uuid.UUID(req["business_id"]), d["po"].supplier_id, [str(ln.item_id) for ln in d["lines"]])
    if alt is None:
        return
    async with read_session() as s:
        from app.models.master import Supplier

        sup = await s.get(Supplier, uuid.UUID(alt["supplier_id"]))
    lead = int(max(sup.stated_lead_time_days, sup.observed_lead_time_days or 0)) if sup else 1
    inputs = {"supplier_id": alt["supplier_id"], "expected_date": (clock.today() + timedelta(days=lead)).isoformat(),
              "lines": [{"item_id": str(ln.item_id), "qty": str(ln.qty), "unit": ln.unit, "pack_size": str(ln.pack_size),
                         "unit_price_minor": alt["prices"][str(ln.item_id)]} for ln in d["lines"]],
              "reason": f"replacement for late order {d['po'].number}", "is_critical": d["po"].is_critical_order,
              "projected_stockout": d["po"].notes.get("projected_stockout")}
    drafted = await run_action("draft_po", inputs, uuid.UUID(req["business_id"]))
    if drafted["outcome"] == "completed" and drafted["result"]:
        await run_action("send_po", {"po_id": drafted["result"]["po_id"]}, uuid.UUID(req["business_id"]))


def register() -> None:
    from app.agents.stock import sales_import
    from app.approvals.service import register_question_handler
    from app.seed.sample_cafe import register_extension

    action_specs.register_specs()
    sales_import.register_spec()
    graphs.register_graphs()
    register_extension(seed_opening_stock)
    register_question_handler("late", _late_answer)
    from app.agents.stock import telegram_handlers

    telegram_handlers.register()
