"""Stock endpoints (contracts/rest-api.md "Stock", T062)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, File, Form, Response, UploadFile
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import func, select

from app.agents.stock import checks, demand, reorder, waste
from app.agents.stock import graphs as stock_graphs
from app.agents.stock import supplier as supplier_perf
from app.agents.stock.action_specs import apply_po_edits
from app.api.common import J, with_freshness
from app.core import clock, settings_store
from app.core.auth import CurrentUser, RequireManager, RequireStaff, can
from app.core.errors import AppError, not_found
from app.core.files import save_upload
from app.db.engine import read_session, write_session
from app.harness.graph import run_action
from app.harness.incidents import open_incident
from app.models.master import Item, Supplier, SupplierPrice
from app.models.purchasing import OPEN_STATUSES, Delivery, PurchaseOrder, PurchaseOrderLine
from app.models.stock_ops import StockCount, StockLevel, StockMovement

router = APIRouter(tags=["stock"])


@router.get("/stock/items")
async def list_items(user: CurrentUser = RequireStaff) -> Response:
    """Purchased items with stock, days of cover, reorder status and expiry risk. Staff see no costs."""
    show_money = can(user.role, "manager")
    today = clock.today()
    async with read_session() as s:
        items = list((await s.execute(select(Item).where(Item.business_id == user.business_id,
                                                         Item.is_ingredient.is_(True)).order_by(Item.name_en))).scalars())
        levels = {r.item_id: r for r in (await s.execute(select(StockLevel).where(StockLevel.business_id == user.business_id))).scalars()}
        gen = await demand.latest_generation(s, user.business_id, today)
        fc = await demand.daily_demand(s, user.business_id, gen) if gen else {}
        open_lines: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for po in (await s.execute(select(PurchaseOrder).where(PurchaseOrder.business_id == user.business_id,
                                                               PurchaseOrder.status.in_(OPEN_STATUSES)))).scalars():
            for ln in (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars():
                open_lines.setdefault(ln.item_id, []).append({"po": po.number, "status": po.status, "qty": ln.qty,
                                                              "expected_date": po.expected_date})
        suppliers = {sp.id: sp for sp in (await s.execute(select(Supplier).where(Supplier.business_id == user.business_id))).scalars()}
        last_update = (await s.execute(select(func.max(StockLevel.last_updated_at)).where(
            StockLevel.business_id == user.business_id))).scalar_one()
    out = []
    for item in items:
        lvl = levels.get(item.id)
        qty = lvl.quantity if lvl else Decimal(0)
        demand_by_day = fc.get(item.id, {})
        cover = reorder.days_of_cover(qty, demand_by_day, today)
        avg = sum(demand_by_day.get(today + timedelta(days=i), Decimal(0)) for i in range(7)) / 7
        risk = waste.expiry_risk(item, qty, avg)
        stockout = next((d for d, v in reorder.projection(qty, demand_by_day, {}, today, 30) if v < 0), None)
        status = "ok"
        if item.id in open_lines:
            status = "on_order"
        elif cover < reorder.trigger_window(float(suppliers[item.preferred_supplier_id].stated_lead_time_days)
                                            if item.preferred_supplier_id in suppliers else 1):
            status = "reorder"
        row: dict[str, Any] = {
            "id": item.id, "name_en": item.name_en, "name_ar": item.name_ar, "unit": item.unit, "category": item.category,
            "quantity": qty, "days_of_cover": None if cover == float("inf") else round(cover, 1),
            "projected_stockout": stockout, "reorder_status": status, "is_critical": item.is_critical,
            "safety_stock": item.safety_stock, "open_orders": open_lines.get(item.id, []),
            "expiry_risk": {"days_to_use": round(risk.days_to_sell, 1), "shelf_life_days": risk.shelf_life_days,
                            "suggestion": risk.suggestion} if risk else None,
            "supplier": suppliers[item.preferred_supplier_id].name_en if item.preferred_supplier_id in suppliers else None,
            "last_counted_at": lvl.last_counted_at if lvl else None,
        }
        if show_money:
            row["unit_cost"] = item.unit_cost
        out.append(row)
    return J(with_freshness({"items": out, "forecast_generated_on": gen},
                            {"stock": last_update, "forecast": gen}))


@router.get("/stock/products")
async def list_products(user: CurrentUser = RequireStaff) -> Response:
    """Sold products (for manual sales entry). Staff see no prices."""
    async with read_session() as s:
        rows = (await s.execute(select(Item).where(Item.business_id == user.business_id, Item.is_sold.is_(True))
                                .order_by(Item.name_en))).scalars().all()
    show = can(user.role, "manager")
    return J(with_freshness({"products": [{"id": i.id, "name_en": i.name_en, "name_ar": i.name_ar,
                                           **({"sale_price": i.sale_price} if show else {})} for i in rows]},
                            {"catalog": max((i.updated_at for i in rows), default=None)}))


@router.get("/stock/items/{item_id}/forecast")
async def item_forecast(item_id: uuid.UUID, days: int = 14, user: CurrentUser = RequireManager) -> Response:
    today = clock.today()
    async with read_session() as s:
        item = await s.get(Item, item_id)
        if item is None or item.business_id != user.business_id:
            raise not_found("Item")
        gen = await demand.latest_generation(s, user.business_id, today)
        fc = (await demand.daily_demand(s, user.business_id, gen, "expected")) if gen else {}
        lo = (await demand.daily_demand(s, user.business_id, gen, "low")) if gen else {}
        hi = (await demand.daily_demand(s, user.business_id, gen, "high")) if gen else {}
        actual: dict[str, float] = {}
        start = today - timedelta(days=days)
        if item.is_sold:
            hist = await demand.sales_history(s, user.business_id, today, days)
            actual = {d.isoformat(): v for d, v in hist.get(item_id, {}).items()}
        else:
            moves = (await s.execute(select(StockMovement.date, func.sum(StockMovement.quantity)).where(
                StockMovement.item_id == item_id, StockMovement.type == "sale", StockMovement.date >= start)
                .group_by(StockMovement.date))).all()
            actual = {d.isoformat(): float(-q) for d, q in moves}
        past = {}
        from app.models.stock_ops import DemandForecast

        for r in (await s.execute(select(DemandForecast).where(DemandForecast.item_id == item_id,
                                                               DemandForecast.forecast_date >= start,
                                                               DemandForecast.forecast_date < today,
                                                               DemandForecast.forecast_date == DemandForecast.generated_on))).scalars():
            past[r.forecast_date.isoformat()] = float(r.expected)
    series = []
    for i in range(-days, days):
        d = today + timedelta(days=i)
        key = d.isoformat()
        series.append({"date": key, "actual": actual.get(key) if i < 0 else None,
                       "forecast": float(fc.get(item_id, {}).get(d)) if i >= 0 and d in fc.get(item_id, {}) else past.get(key),
                       "low": float(lo.get(item_id, {}).get(d)) if i >= 0 and d in lo.get(item_id, {}) else None,
                       "high": float(hi.get(item_id, {}).get(d)) if i >= 0 and d in hi.get(item_id, {}) else None})
    method = "derived" if item.is_ingredient and not item.is_sold else item.forecast_method
    return J(with_freshness({"item": {"id": item.id, "name_en": item.name_en, "name_ar": item.name_ar, "unit": item.unit},
                             "method": method, "series": series}, {"forecast": gen}))


class WasteIn(BaseModel):
    item_id: uuid.UUID
    qty: Decimal = Field(gt=0)
    type: Literal["waste", "spoilage"] = "waste"
    reason: str = Field(min_length=2, max_length=300)


@router.post("/stock/waste", status_code=201)
async def record_waste(body: WasteIn, user: CurrentUser = RequireStaff) -> Response:
    out = await run_action("adjust_stock", {"item_id": str(body.item_id), "qty_delta": str(-body.qty), "type": body.type,
                                            "reason": body.reason, "source": f"user:{user.username}"}, user.business_id)
    if out["outcome"] != "completed":
        raise AppError(422, "not_recorded", message_en="The waste could not be recorded.", message_ar="تعذر تسجيل الهالك.",
                       extra={"outcome": out["outcome"]})
    return J({"outcome": out["outcome"], "result": out["result"]}, 201)


class CountLine(BaseModel):
    item_id: uuid.UUID
    counted_qty: Decimal = Field(ge=0)


class CountIn(BaseModel):
    counts: list[CountLine]


@router.post("/stock/counts", status_code=201)
async def record_counts(body: CountIn, user: CurrentUser = RequireManager) -> Response:
    tolerance = float(await settings_store.get(user.business_id, "stock_variance_pct"))
    results = []
    for c in body.counts:
        item_id, counted = c.item_id, c.counted_qty
        async with read_session() as s:
            item = await s.get(Item, item_id)
            lvl = (await s.execute(select(StockLevel).where(StockLevel.item_id == item_id))).scalar_one_or_none()
        if item is None or item.business_id != user.business_id:
            raise not_found("Item")
        calculated = lvl.quantity if lvl else Decimal(0)
        check = checks.count_variance(item, counted, calculated, tolerance)
        resolution = "accepted" if check.passed else "investigating"
        async with write_session() as s:
            s.add(StockCount(business_id=user.business_id, item_id=item_id, counted_qty=counted, calculated_qty=calculated,
                             variance=counted - calculated, counted_by=user.id, date=clock.today(), resolution=resolution))
        if counted != calculated:
            reason = "stock count" if check.passed else "stock count: variance under investigation (missing waste record, recipe error or loss)"
            await run_action("adjust_stock", {"item_id": str(item_id), "qty_delta": str(counted - calculated),
                                              "type": "count_correction", "reason": reason}, user.business_id)
        if not check.passed:
            await open_incident(business_id=user.business_id, agent="stock", type="count_variance", detected_by="count_variance",
                                summary=check.reason_en, refs={"item_id": str(item_id), **{k: str(v) for k, v in check.details.items()}},
                                action_taken="adjusted to the count and opened an investigation")
        results.append({"item_id": item_id, "variance": counted - calculated, "resolution": resolution,
                        "variance_pct": check.details.get("variance_pct")})
    return J({"results": results}, 201)


async def _po_out(s: Any, po: PurchaseOrder) -> dict[str, Any]:
    sup = await s.get(Supplier, po.supplier_id)
    lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars().all()
    items = {i.id: i for i in (await s.execute(select(Item).where(Item.id.in_([ln.item_id for ln in lines])))).scalars()} if lines else {}
    deliveries = (await s.execute(select(Delivery).where(Delivery.po_id == po.id))).scalars().all()
    return {"id": po.id, "number": po.number, "status": po.status, "supplier": {"id": sup.id, "name_en": sup.name_en,
            "name_ar": sup.name_ar} if sup else None, "total": po.total, "expected_date": po.expected_date,
            "is_critical": po.is_critical_order, "sent_at": po.sent_at, "late": po.late_flagged, "notes": po.notes,
            "lines": [{"item_id": ln.item_id, "name_en": items[ln.item_id].name_en if ln.item_id in items else "",
                       "name_ar": items[ln.item_id].name_ar if ln.item_id in items else "", "qty": ln.qty, "unit": ln.unit,
                       "unit_price": ln.unit_price, "line_total": ln.line_total} for ln in lines],
            "deliveries": [{"id": d.id, "received_on": d.received_on, "discrepancies": d.discrepancies,
                            "has_photo": d.photo_file_id is not None} for d in deliveries]}


@router.get("/purchase-orders")
async def list_pos(status: str | None = None, user: CurrentUser = RequireStaff) -> Response:
    async with read_session() as s:
        q = select(PurchaseOrder).where(PurchaseOrder.business_id == user.business_id)
        if status == "open":
            q = q.where(PurchaseOrder.status.in_(OPEN_STATUSES))
        elif status:
            q = q.where(PurchaseOrder.status == status)
        rows = (await s.execute(q.order_by(PurchaseOrder.created_at.desc()).limit(100))).scalars().all()
        out = [await _po_out(s, po) for po in rows]
    if not can(user.role, "manager"):  # staff see what to expect, not prices
        for po in out:
            po.pop("total", None)
            for ln in po["lines"]:
                ln.pop("unit_price", None)
                ln.pop("line_total", None)
    return J(with_freshness({"purchase_orders": out}, {"orders": max((po.updated_at for po in rows), default=None)}))


class POPatch(BaseModel):
    lines: list[dict[str, Any]]  # [{item_id, qty}]


@router.patch("/purchase-orders/{po_id}")
async def patch_po(po_id: uuid.UUID, body: POPatch, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, po_id)
    if po is None or po.business_id != user.business_id:
        raise not_found("Purchase order")
    if po.status not in ("draft", "pending_approval", "on_hold"):
        raise AppError(409, "po_locked", message_en="This order can no longer be edited.", message_ar="لم يعد بالإمكان تعديل هذا الأمر.")
    await apply_po_edits(po_id, {"lines": body.lines})
    # Re-run the draft checks on the edited order.
    pct = float(await settings_store.get(user.business_id, "price_change_pct"))
    found = []
    async with read_session() as s:
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po_id))).scalars().all()
        found.append(await checks.duplicate_po(s, user.business_id, po.supplier_id, {str(ln.item_id) for ln in lines},
                                               exclude_po=po_id))
        for ln in lines:
            item = await s.get(Item, ln.item_id)
            price = (await s.execute(select(SupplierPrice).where(SupplierPrice.supplier_id == po.supplier_id,
                                                                 SupplierPrice.item_id == ln.item_id)
                                     .order_by(SupplierPrice.valid_from.desc()).limit(1))).scalar_one_or_none()
            hist = await checks.price_history(s, po.supplier_id, ln.item_id, clock.today() + timedelta(days=1))
            found.append(checks.price_sanity(item, ln.unit_price.amount_minor, hist[1:], pct))
            found.append(checks.unit_mismatch(item, ln.unit, ln.pack_size, price))
        out = await _po_out(s, await s.get(PurchaseOrder, po_id))
    out["checks"] = [{"name": c.name, "passed": c.passed, "reason_en": c.reason_en, "reason_ar": c.reason_ar} for c in found]
    return J(out)


class DeliveryLineIn(BaseModel):
    item_id: uuid.UUID
    qty_received: Decimal = Field(ge=0, le=Decimal("1000000"))
    unit_price_minor: int | None = Field(default=None, ge=0, le=10**15)


_DELIVERY_LINES = TypeAdapter(list[DeliveryLineIn])


@router.post("/purchase-orders/{po_id}/deliveries", status_code=201)
async def record_delivery(po_id: uuid.UUID, lines: str = Form(...), photo: UploadFile | None = File(None),
                          user: CurrentUser = RequireStaff) -> Response:
    """Staff delivery checklist; `lines` is JSON [{item_id, qty_received, unit_price_minor?}]."""
    async with read_session() as s:
        po = await s.get(PurchaseOrder, po_id)
    if po is None or po.business_id != user.business_id:
        raise not_found("Purchase order")
    try:
        parsed = [ln.model_dump(mode="json", exclude_none=True) for ln in _DELIVERY_LINES.validate_json(lines)]
    except ValidationError as exc:
        raise AppError(422, "invalid_lines", message_en="Lines must be a JSON list of {item_id, qty_received}.",
                       message_ar="يجب أن تكون البنود قائمة JSON من {item_id, qty_received}.") from exc
    photo_id = po.notes.get("pending_photo_file_id")
    if photo is not None:
        ref = await save_upload(user.business_id, await photo.read(), photo.content_type or "image/jpeg",
                                photo.filename or "delivery.jpg", user.id)
        photo_id = str(ref.id)
    result = await stock_graphs.record_delivery(user.business_id, {
        "po_id": str(po_id), "lines": parsed, "photo_file_id": photo_id, "received_by": str(user.id),
        "received_on": clock.today().isoformat()})
    if result.get("outcome") != "completed":
        raise AppError(422, "delivery_not_recorded", message_en="The delivery could not be recorded.",
                       message_ar="تعذر تسجيل التوريد.", extra={"result": result})
    if not can(user.role, "manager"):
        result.pop("discrepancies", None)
    return J(result, 201)


@router.get("/suppliers")
async def suppliers(user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        rows = (await s.execute(select(Supplier).where(Supplier.business_id == user.business_id).order_by(Supplier.name_en))).scalars()
        return J({"suppliers": [{"id": sp.id, "name_en": sp.name_en, "name_ar": sp.name_ar, "vat_number": sp.vat_number,
                                 "phone": sp.phone, "stated_lead_time_days": sp.stated_lead_time_days,
                                 "observed_lead_time_days": sp.observed_lead_time_days,
                                 "payment_terms_days": sp.payment_terms_days, "reliability_score": sp.reliability_score}
                                for sp in rows]})


@router.get("/suppliers/{supplier_id}/scorecard")
async def supplier_scorecard(supplier_id: uuid.UUID, user: CurrentUser = RequireManager) -> Response:
    """Stated vs observed lead time, delivery record, price changes and reliability (US8)."""
    async with read_session() as s:
        sp = await s.get(Supplier, supplier_id)
        if sp is None or sp.business_id != user.business_id:
            raise not_found("Supplier")
        pos = {po.id: po for po in (await s.execute(select(PurchaseOrder).where(
            PurchaseOrder.business_id == user.business_id, PurchaseOrder.supplier_id == supplier_id))).scalars()}
        deliveries = (await s.execute(select(Delivery).where(Delivery.po_id.in_(list(pos))).order_by(Delivery.received_on))
                      ).scalars().all() if pos else []
        prices = (await s.execute(select(SupplierPrice).where(SupplierPrice.supplier_id == supplier_id)
                                  .order_by(SupplierPrice.valid_from, SupplierPrice.created_at))).scalars().all()
        items = {i.id: i for i in (await s.execute(select(Item).where(Item.business_id == user.business_id))).scalars()}
    record: list[dict[str, Any]] = []
    for dl in deliveries:
        po = pos[dl.po_id]
        record.append({"po": po.number, "sent_on": po.sent_at.date() if po.sent_at else None, "expected": po.expected_date,
                       "received_on": dl.received_on,
                       "lead_days": (dl.received_on - po.sent_at.date()).days if po.sent_at else None,
                       "on_time": dl.received_on <= po.expected_date,
                       "complete": not any(d.get("kind") in ("quantity", "missing") for d in dl.discrepancies or []),
                       "price_ok": not any(d.get("kind") == "price" for d in dl.discrepancies or [])})
    history: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for p in prices:
        rows = history.setdefault(p.item_id, [])
        prev = rows[-1]["price"].amount_minor if rows else None
        change = round((p.price.amount_minor - prev) / prev * 100, 1) if prev else None
        rows.append({"valid_from": p.valid_from, "price": p.price, "unit": p.unit, "change_pct": change})
    leads: list[int] = [r["lead_days"] for r in record if r["lead_days"] is not None]
    return J(with_freshness({
        "supplier": {"id": sp.id, "name_en": sp.name_en, "name_ar": sp.name_ar, "payment_terms_days": sp.payment_terms_days},
        "lead_time": {"stated_days": sp.stated_lead_time_days, "observed_days": sp.observed_lead_time_days,
                      "average_actual_days": round(sum(leads) / len(leads), 1) if leads else None,
                      "planning_days": supplier_perf.lead_time(sp)},
        "reliability_score": sp.reliability_score,
        "deliveries": {"count": len(record), "on_time": sum(1 for r in record if r["on_time"]),
                       "complete": sum(1 for r in record if r["complete"]), "recent": record[-10:][::-1]},
        "orders": {"count": len(pos), "open": sum(1 for po in pos.values() if po.status in OPEN_STATUSES)},
        "prices": [{"item_id": iid, "name_en": items[iid].name_en if iid in items else "",
                    "name_ar": items[iid].name_ar if iid in items else "", "history": rows,
                    "latest_change_pct": next((r["change_pct"] for r in reversed(rows) if r["change_pct"] is not None), None)}
                   for iid, rows in history.items()],
    }, {"orders": max((dl.created_at for dl in deliveries), default=None),
        "prices": max((p.created_at for p in prices), default=None)}))
