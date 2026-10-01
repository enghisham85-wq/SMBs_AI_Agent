"""Stock Agent actions run through harness_graph (T060)."""

from __future__ import annotations

import math
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select

from app.agents.stock import checks, demand, tracking
from app.agents.stock import supplier as supplier_perf
from app.core import clock
from app.core.events import publish
from app.core.i18n import option
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.action_spec import ActionContext, ActionSpec, Check, OwnerAsk, VerifyOutcome, register
from app.harness.calibration import auto_approve_factor
from app.harness.verifier import build_packet
from app.llm.messages import compose
from app.models.finance_master import Sale
from app.models.master import Item, Supplier, SupplierPrice
from app.models.purchasing import Delivery, DeliveryLine, PurchaseOrder, PurchaseOrderLine, transition
from app.models.stock_ops import DemandForecast, StockLevel, StockMovement
from app.models.tenancy import Business

AR_DAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


async def _business(bid: uuid.UUID) -> Business:
    async with read_session() as s:
        b = await s.get(Business, bid)
    assert b is not None
    return b


def _weekday(d: date, lang: str) -> str:
    return d.strftime("%A") if lang == "en" else AR_DAYS[d.weekday()]


# =========================================================================== draft_po
async def _draft_plan(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    return {"intent": "draft a purchase order", "reason": inputs.get("reason", ""),
            "data_refs": {"forecast_generated_on": inputs.get("forecast_generated_on"),
                          "projected_stockout": inputs.get("projected_stockout")}}


async def _draft_checks(ctx: ActionContext, inputs: dict[str, Any]) -> list[Check]:
    from app.core import settings_store

    out: list[Check] = []
    supplier_id = uuid.UUID(inputs["supplier_id"])
    today = clock.today()
    pct = float(await settings_store.get(ctx.business_id, "price_change_pct"))
    async with read_session() as s:
        items = {str(i.id): i for i in (await s.execute(select(Item).where(Item.business_id == ctx.business_id))).scalars()}
        own = (await s.execute(select(PurchaseOrder.id).where(PurchaseOrder.created_by_action_id == ctx.action_id))).scalar_one_or_none()
        out.append(await checks.duplicate_po(s, ctx.business_id, supplier_id, {ln["item_id"] for ln in inputs["lines"]},
                                             exclude_po=own))
        for ln in inputs["lines"]:
            item = items[ln["item_id"]]
            price = (await s.execute(select(SupplierPrice).where(SupplierPrice.supplier_id == supplier_id,
                                                                 SupplierPrice.item_id == item.id,
                                                                 SupplierPrice.valid_from <= today)
                                     .order_by(SupplierPrice.valid_from.desc()).limit(1))).scalar_one_or_none()
            history = await checks.price_history(s, supplier_id, item.id, today)
            # The current list price is part of the history; compare the proposed price against earlier ones.
            prior = history[1:] if history and price is not None and history[0] == int(ln["unit_price_minor"]) else history
            price_check = checks.price_sanity(item, int(ln["unit_price_minor"]), prior, pct)
            if not price_check.passed:  # a supplier price jump is a fault to learn from, not only a question
                sup = await s.get(Supplier, supplier_id)
                price_check.incident = True
                price_check.details.update({"supplier_id": str(supplier_id), "supplier_en": sup.name_en if sup else "",
                                            "supplier_ar": sup.name_ar if sup else "", "item_en": item.name_en})
            out.append(price_check)
            out.append(checks.unit_mismatch(item, ln["unit"], _dec(ln.get("pack_size", 1)), price))
    return out


async def _draft_on_hold(ctx: ActionContext, inputs: dict[str, Any], failed: list[dict[str, Any]]) -> OwnerAsk:
    po_id = await _upsert_po(ctx, inputs, status="on_hold", hold=failed)
    names = {f["name"] for f in failed}
    opts = [option("continue", "opt_continue", "override")]
    if "duplicate_po" in names:
        opts.append({"key": "merge", "label_en": "Merge into open order", "label_ar": "دمج مع الأمر المفتوح", "effect": "merge"})
    if "price_sanity" in names:
        opts.append({"key": "alternative", "label_en": "Find another supplier", "label_ar": "ابحث عن مورد آخر",
                     "effect": "alternative"})
    opts.append(option("cancel", "opt_cancel", "cancel"))
    reason_en = "; ".join(f["reason_en"] for f in failed)
    reason_ar = "؛ ".join(f["reason_ar"] or f["reason_en"] for f in failed)
    async with read_session() as s:
        sup = await s.get(Supplier, uuid.UUID(inputs["supplier_id"]))
    sup_en = sup.name_en if sup else "supplier"
    sup_ar = sup.name_ar if sup else "المورد"
    return OwnerAsk(kind="question",
                    text_en=f"I held the order to {sup_en}: {reason_en}. Continue anyway?",
                    text_ar=f"أوقفت الطلب إلى {sup_ar}: {reason_ar}. هل أتابع؟",
                    options=opts[:4], context={"po_id": str(po_id), "checks": [f["name"] for f in failed]})


async def _next_po_number(s: Any, business_id: uuid.UUID) -> str:
    n = (await s.execute(select(func.count()).select_from(PurchaseOrder).where(PurchaseOrder.business_id == business_id))).scalar_one()
    return f"PO-{n + 1:04d}"


async def _upsert_po(ctx: ActionContext, inputs: dict[str, Any], *, status: str,
                     hold: list[dict[str, Any]] | None = None) -> uuid.UUID:
    business = await _business(ctx.business_id)
    cur = business.currency
    async with write_session() as s:
        po = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.created_by_action_id == ctx.action_id))).scalar_one_or_none()
        if po is None:
            po = PurchaseOrder(business_id=ctx.business_id, number=await _next_po_number(s, ctx.business_id),
                               supplier_id=uuid.UUID(inputs["supplier_id"]), status=status,
                               total=Money(0, cur), expected_date=date.fromisoformat(inputs["expected_date"]),
                               is_critical_order=bool(inputs.get("is_critical")), created_by_action_id=ctx.action_id,
                               notes={})
            s.add(po)
            await s.flush()
        else:
            if po.status != status:
                if po.status == "on_hold" and status == "draft":
                    transition(po, "draft")
                else:
                    po.status = status
            po.supplier_id = uuid.UUID(inputs["supplier_id"])
            po.expected_date = date.fromisoformat(inputs["expected_date"])
        await s.execute(delete(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))
        total = 0
        for ln in inputs["lines"]:
            qty = _dec(ln["qty"])
            unit_price = Money(int(ln["unit_price_minor"]), cur)
            line_total = unit_price.times(qty)
            total += line_total.amount_minor
            s.add(PurchaseOrderLine(business_id=ctx.business_id, po_id=po.id, item_id=uuid.UUID(ln["item_id"]),
                                    qty=qty, unit=ln["unit"], pack_size=_dec(ln.get("pack_size", 1)),
                                    unit_price=unit_price, line_total=line_total))
        po.total = Money(total, cur)
        po.notes = {**(po.notes or {}), "reason": inputs.get("reason"), "projected_stockout": inputs.get("projected_stockout"),
                    **({"hold": [f["name"] for f in hold]} if hold else {})}
        return po.id


async def _draft_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    po_id = await _upsert_po(ctx, inputs, status="draft")
    async with write_session() as s:
        po = await s.get(PurchaseOrder, po_id)
        assert po is not None
        publish(s, "po.drafted", {"po_id": po.id, "supplier_id": po.supplier_id, "total": po.total,
                                  "expected_date": po.expected_date, "is_critical": po.is_critical_order},
                producer="stock", business_id=ctx.business_id, action_id=ctx.action_id)
        return {"po_id": str(po.id), "number": po.number, "total_minor": po.total.amount_minor}


async def _draft_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(result["po_id"]))
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == uuid.UUID(result["po_id"])))).scalars().all()
    if po is None:
        return VerifyOutcome(False, {"problem": "purchase order was not saved"})
    total = sum(ln.line_total.amount_minor for ln in lines)
    ok = po.status == "draft" and total == po.total.amount_minor and len(lines) == len(inputs["lines"])
    return VerifyOutcome(ok, {"status": po.status, "total": po.total.amount_minor, "lines_total": total,
                              "lines": len(lines)})


async def _draft_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    async with write_session() as s:
        po = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.created_by_action_id == ctx.action_id))).scalar_one_or_none()
        if po is not None:
            await s.execute(delete(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))
            await s.delete(po)


async def cheapest_alternative(business_id: uuid.UUID, supplier_id: uuid.UUID, item_ids: list[str]) -> dict[str, Any] | None:
    async with read_session() as s:
        prices = (await s.execute(select(SupplierPrice).where(SupplierPrice.business_id == business_id,
                                                              SupplierPrice.supplier_id != supplier_id,
                                                              SupplierPrice.item_id.in_([uuid.UUID(i) for i in item_ids]))
                                  .order_by(SupplierPrice.valid_from.desc()))).scalars().all()
    by_supplier: dict[uuid.UUID, dict[str, SupplierPrice]] = {}
    for p in prices:
        by_supplier.setdefault(p.supplier_id, {}).setdefault(str(p.item_id), p)
    complete = {sid: m for sid, m in by_supplier.items() if set(item_ids) <= set(m)}
    if not complete:
        return None
    sid, m = min(complete.items(), key=lambda kv: sum(p.price.amount_minor for p in kv[1].values()))
    return {"supplier_id": str(sid), "prices": {k: v.price.amount_minor for k, v in m.items()}}


async def _draft_on_option(ctx: ActionContext, inputs: dict[str, Any], option_key: str, edits: dict[str, Any]) -> dict[str, Any]:
    if option_key == "merge":
        async with write_session() as s:
            own = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.created_by_action_id == ctx.action_id))).scalar_one_or_none()
            target = None
            for po in (await s.execute(select(PurchaseOrder).where(
                    PurchaseOrder.business_id == ctx.business_id, PurchaseOrder.supplier_id == uuid.UUID(inputs["supplier_id"]),
                    PurchaseOrder.status.in_(("draft", "pending_approval", "approved"))))).scalars():
                if own is None or po.id != own.id:
                    target = po
                    break
            if target is not None:
                existing = {str(ln.item_id): ln for ln in (await s.execute(select(PurchaseOrderLine).where(
                    PurchaseOrderLine.po_id == target.id))).scalars()}
                for ln in inputs["lines"]:
                    cur = existing.get(ln["item_id"])
                    if cur is not None:
                        cur.qty = max(cur.qty, _dec(ln["qty"]))
                        cur.line_total = cur.unit_price.times(cur.qty)
                target.total = Money(sum(ln.line_total.amount_minor for ln in existing.values()), target.total.currency)
            if own is not None:
                own.status = "cancelled"
                own.merged_into_id = target.id if target else None
        return {"next": "finalize", "outcome": "cancelled"}
    if option_key == "alternative":
        alt = await cheapest_alternative(ctx.business_id, uuid.UUID(inputs["supplier_id"]), [ln["item_id"] for ln in inputs["lines"]])
        if alt is None:
            return {"next": "finalize", "outcome": "cancelled"}
        new_inputs = {**inputs, "supplier_id": alt["supplier_id"],
                      "lines": [{**ln, "unit_price_minor": alt["prices"][ln["item_id"]]} for ln in inputs["lines"]]}
        return {"next": "precheck", "inputs": new_inputs}
    return {"next": "finalize", "outcome": "cancelled"}


async def _draft_on_finalize(ctx: ActionContext, inputs: dict[str, Any], outcome: str, result: dict[str, Any]) -> None:
    if outcome in ("cancelled", "rejected", "escalated"):
        async with write_session() as s:
            po = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.created_by_action_id == ctx.action_id))).scalar_one_or_none()
            if po is not None and po.status in ("on_hold", "draft"):
                po.status = "cancelled"


# =========================================================================== send_po
async def po_details(po_id: uuid.UUID) -> dict[str, Any]:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, po_id)
        assert po is not None
        sup = await s.get(Supplier, po.supplier_id)
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po_id))).scalars().all()
        items = {i.id: i for i in (await s.execute(select(Item).where(Item.id.in_([ln.item_id for ln in lines])))).scalars()}
    return {"po": po, "supplier": sup, "lines": lines, "items": items}


def _qty_text(q: Decimal) -> str:
    return f"{q.normalize():f}"


async def _send_checks(ctx: ActionContext, inputs: dict[str, Any]) -> list[Check]:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
    ok = po is not None and po.status in ("draft", "pending_approval", "approved")
    return [Check("po_ready_to_send", ok, {"status": po.status if po else None},
                  reason_en="the purchase order is not in a sendable state")]


async def _send_packet(ctx: ActionContext, inputs: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    d = await po_details(uuid.UUID(inputs["po_id"]))
    po, lines, items = d["po"], d["lines"], d["items"]
    today = clock.today()
    known: list[dict[str, Any]] = []
    async with read_session() as s:
        levels = {r.item_id: r.quantity for r in (await s.execute(select(StockLevel).where(
            StockLevel.item_id.in_([ln.item_id for ln in lines])))).scalars()}
        gen = await demand.latest_generation(s, ctx.business_id, today)
        fc = await demand.daily_demand(s, ctx.business_id, gen) if gen else {}
    source = {"today": today, "stock": {items[ln.item_id].name_en: levels.get(ln.item_id) for ln in lines},
              "forecast_next_7_days": {items[ln.item_id].name_en: [fc.get(ln.item_id, {}).get(today + timedelta(days=i))
                                                                   for i in range(7)] for ln in lines},
              "storage_capacity": {items[ln.item_id].name_en: items[ln.item_id].storage_capacity for ln in lines}}
    for ln in lines:
        item = items[ln.item_id]
        if ln.unit != item.unit:
            known.append({"field": item.name_en, "problem": f"unit {ln.unit} differs from stock unit {item.unit}", "severity": "high"})
        cap = item.storage_capacity
        if cap is not None and ln.qty + max(levels.get(ln.item_id, Decimal(0)), Decimal(0)) > cap * Decimal("1.2"):
            known.append({"field": item.name_en, "problem": "quantity exceeds storage capacity", "severity": "high"})
    proposed = {"supplier": d["supplier"].name_en if d["supplier"] else None, "expected_date": po.expected_date,
                "total": po.total,
                "lines": [{"item": items[ln.item_id].name_en, "qty": ln.qty, "unit": ln.unit, "unit_price": ln.unit_price}
                          for ln in lines]}
    return build_packet("send_po", source, proposed, known_issues=known, irreversible=True)


async def _send_auto(ctx: ActionContext, inputs: dict[str, Any], settings: dict[str, Any]) -> bool:
    """Auto-approve within the owner's limit from Settings, or within a routine-order rule the owner approved."""
    from app.agents.stock.routine_orders import within_owner_rule

    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
    if po is None:
        return False
    limit = int(settings.get("po_auto_approve_limit") or 0)
    if limit > 0 and 0 < po.total.amount_minor <= int(limit * await auto_approve_factor(ctx.business_id, "stock")):
        return True
    return await within_owner_rule(ctx.business_id, po) is not None


async def _send_request(ctx: ActionContext, inputs: dict[str, Any], plan: dict[str, Any]) -> OwnerAsk:
    d = await po_details(uuid.UUID(inputs["po_id"]))
    po, sup, lines, items = d["po"], d["supplier"], d["lines"], d["items"]
    async with write_session() as s:
        row = await s.get(PurchaseOrder, po.id)
        if row is not None and row.status == "draft":
            transition(row, "pending_approval")
    stockout = po.notes.get("projected_stockout")
    first = lines[0]
    item = items[first.item_id]
    total = po.total.to_display()
    arrival_en, arrival_ar = _weekday(po.expected_date, "en"), _weekday(po.expected_date, "ar")
    if stockout:
        so = date.fromisoformat(stockout)
        lead_en = f"{item.name_en} will run out {_weekday(so, 'en')} evening. "
        lead_ar = f"سينفد {item.name_ar} مساء {_weekday(so, 'ar')}. "
    else:
        lead_en, lead_ar = f"{item.name_en} is running low. ", f"{item.name_ar} على وشك النفاد. "
    what_en = ", ".join(f"{_qty_text(ln.qty)} {ln.unit} {items[ln.item_id].name_en}" for ln in lines)
    what_ar = "، ".join(f"{_qty_text(ln.qty)} {ln.unit} {items[ln.item_id].name_ar}" for ln in lines)
    fb_en = f"{lead_en}Order {what_en} from {sup.name_en} (arrives {arrival_en}), {total}?"
    fb_ar = f"{lead_ar}هل أطلب {what_ar} من {sup.name_ar} (يصل {arrival_ar})، بقيمة {po.total.to_display('ar')}؟"
    facts = {"item": item.name_en, "stockout_day": _weekday(date.fromisoformat(stockout), "en") if stockout else None,
             "lines": [{"item": items[ln.item_id].name_en, "qty": _qty_text(ln.qty), "unit": ln.unit} for ln in lines],
             "supplier": sup.name_en, "arrives": arrival_en, "total": total}
    msg = await compose(facts, fb_en, fb_ar)
    edit_lines = [{"item_id": str(ln.item_id), "name_en": items[ln.item_id].name_en, "name_ar": items[ln.item_id].name_ar,
                   "qty": str(ln.qty), "unit": ln.unit} for ln in lines]
    return OwnerAsk(kind="approval", text_en=msg.text_en, text_ar=msg.text_ar,
                    options=[option("approve", "opt_approve", "approve"), option("edit", "opt_edit", "edit"),
                             option("reject", "opt_reject", "reject")],
                    required_role="manager", safe_default="reask",
                    context={"po_id": str(po.id), "po_number": po.number, "edit": {"lines": edit_lines}})


async def apply_po_edits(po_id: uuid.UUID, edits: dict[str, Any]) -> bool:
    changed = False
    async with write_session() as s:
        po = await s.get(PurchaseOrder, po_id)
        assert po is not None
        lines = {str(ln.item_id): ln for ln in (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po_id))).scalars()}
        for e in edits.get("lines", []):
            ln = lines.get(str(e.get("item_id")))
            if ln is None:
                continue
            qty = _dec(e["qty"])
            if qty <= 0:
                await s.delete(ln)
                lines.pop(str(ln.item_id))
            else:
                ln.qty = qty
                ln.line_total = ln.unit_price.times(qty)
            changed = True
        po.total = Money(sum(ln.line_total.amount_minor for ln in lines.values()), po.total.currency)
    return changed


async def _send_on_option(ctx: ActionContext, inputs: dict[str, Any], option_key: str, edits: dict[str, Any]) -> dict[str, Any]:
    if option_key == "cancel":  # the request was withdrawn (e.g. the order was deferred for the budget)
        return {"next": "finalize", "outcome": "cancelled"}
    if option_key == "edit":
        if edits and await apply_po_edits(uuid.UUID(inputs["po_id"]), edits):
            return {"next": "execute"}  # the edited order counts as approved
        return {"next": "approval_gate"}
    return {"next": "finalize", "outcome": "rejected"}


async def _send_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    async with write_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
        assert po is not None
        if po.status in ("draft", "pending_approval"):
            if po.status == "draft":
                transition(po, "pending_approval")
            transition(po, "approved")
        transition(po, "sent")
        po.sent_at = clock.clock_now()
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars().all()
        publish(s, "po.approved_sent", {"po_id": po.id, "supplier_id": po.supplier_id, "total": po.total,
                                        "sent_at": po.sent_at, "expected_date": po.expected_date,
                                        "lines": [{"item_id": ln.item_id, "qty": ln.qty, "unit_price": ln.unit_price}
                                                  for ln in lines]},
                producer="stock", business_id=ctx.business_id, action_id=ctx.action_id)
        return {"po_id": str(po.id), "sent_at": po.sent_at.isoformat()}


async def _send_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
    return VerifyOutcome(po is not None and po.status == "sent" and po.sent_at is not None,
                         {"status": po.status if po else None})


async def _send_on_finalize(ctx: ActionContext, inputs: dict[str, Any], outcome: str, result: dict[str, Any]) -> None:
    if outcome == "rejected":
        async with write_session() as s:
            po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
            if po is not None and po.status in ("draft", "pending_approval"):
                po.status = "rejected"


# =========================================================================== record_delivery
async def _delivery_checks(ctx: ActionContext, inputs: dict[str, Any]) -> list[Check]:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
    ok = po is not None and po.status in ("sent", "partially_received")
    return [Check("po_awaiting_delivery", ok, {"status": po.status if po else None},
                  reason_en="this purchase order is not waiting for a delivery")]


async def _delivery_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    received_on = date.fromisoformat(inputs.get("received_on") or clock.today().isoformat())
    business = await _business(ctx.business_id)
    async with write_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
        assert po is not None
        prev_status = po.status
        ordered = {str(ln.item_id): ln for ln in (await s.execute(select(PurchaseOrderLine).where(
            PurchaseOrderLine.po_id == po.id))).scalars()}
        delivery = Delivery(business_id=ctx.business_id, po_id=po.id, received_on=received_on,
                            received_by=uuid.UUID(inputs["received_by"]) if inputs.get("received_by") else None,
                            photo_file_id=uuid.UUID(inputs["photo_file_id"]) if inputs.get("photo_file_id") else None,
                            action_id=ctx.action_id, discrepancies=[])
        s.add(delivery)
        await s.flush()
        discrepancies: list[dict[str, Any]] = []
        received_total: dict[str, Decimal] = {}
        for ln in inputs["lines"]:
            qty = _dec(ln["qty_received"])
            ol = ordered.get(ln["item_id"])
            price_minor = int(ln.get("unit_price_minor") or (ol.unit_price.amount_minor if ol else 0))
            s.add(DeliveryLine(business_id=ctx.business_id, delivery_id=delivery.id, item_id=uuid.UUID(ln["item_id"]),
                               qty_received=qty, unit_price_on_note=Money(price_minor, business.currency)))
            if qty > 0:
                await tracking.move(s, ctx.business_id, uuid.UUID(ln["item_id"]), qty, "purchase", received_on,
                                    reference_type="delivery", reference_id=delivery.id, action_id=ctx.action_id)
            received_total[ln["item_id"]] = qty
            if ol is not None and qty != ol.qty:
                discrepancies.append({"item_id": ln["item_id"], "kind": "quantity", "ordered": str(ol.qty), "received": str(qty)})
            if ol is not None and price_minor != ol.unit_price.amount_minor:
                discrepancies.append({"item_id": ln["item_id"], "kind": "price", "ordered_price_minor": ol.unit_price.amount_minor,
                                      "note_price_minor": price_minor})
        for item_id, ol in ordered.items():
            if item_id not in received_total:
                discrepancies.append({"item_id": item_id, "kind": "missing", "ordered": str(ol.qty), "received": "0"})
        delivery.discrepancies = discrepancies
        complete = all(received_total.get(i, Decimal(0)) >= ol.qty for i, ol in ordered.items())
        transition(po, "received" if complete else "partially_received")
        sup = await s.get(Supplier, po.supplier_id)
        if sup is not None:
            supplier_perf.update_on_delivery(s, sup, po, received_on, complete,
                                             price_ok=not any(d["kind"] == "price" for d in discrepancies))
        publish(s, "delivery.received", {"delivery_id": delivery.id, "po_id": po.id,
                                         "lines": [{"item_id": k, "qty_received": v} for k, v in received_total.items()],
                                         "discrepancies": discrepancies},
                producer="stock", business_id=ctx.business_id, action_id=ctx.action_id)
        return {"delivery_id": str(delivery.id), "discrepancies": discrepancies, "prev_status": prev_status,
                "received": {k: str(v) for k, v in received_total.items()}}


async def _delivery_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        moves = (await s.execute(select(StockMovement).where(StockMovement.action_id == ctx.action_id))).scalars().all()
    got = sum((m.quantity for m in moves), Decimal(0))
    expected = sum((_dec(v) for v in result.get("received", {}).values()), Decimal(0))
    return VerifyOutcome(got == expected, {"moved": got, "expected": expected})


async def _delivery_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    async with write_session() as s:
        await tracking.undo_action_movements(s, ctx.action_id)
        if result.get("delivery_id"):
            did = uuid.UUID(result["delivery_id"])
            await s.execute(delete(DeliveryLine).where(DeliveryLine.delivery_id == did))
            await s.execute(delete(Delivery).where(Delivery.id == did))
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
        if po is not None and result.get("prev_status"):
            po.status = result["prev_status"]


# =========================================================================== adjust_stock
async def _adjust_checks(ctx: ActionContext, inputs: dict[str, Any]) -> list[Check]:
    from app.models.stock_ops import REASON_REQUIRED

    needs = inputs.get("type") in REASON_REQUIRED
    ok = not needs or bool((inputs.get("reason") or "").strip())
    return [Check("reason_required", ok, {"type": inputs.get("type")}, reason_en="a reason is required")]


async def _adjust_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    async with write_session() as s:
        await tracking.move(s, ctx.business_id, uuid.UUID(inputs["item_id"]), _dec(inputs["qty_delta"]), inputs["type"],
                            clock.today(), reason=inputs.get("reason"), source=inputs.get("source", "manual"),
                            action_id=ctx.action_id)
        lvl = await tracking.level_row(s, ctx.business_id, uuid.UUID(inputs["item_id"]))
        if inputs["type"] == "count_correction":
            lvl.last_counted_at = clock.clock_now()
        return {"new_level": str(lvl.quantity)}


async def _adjust_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        n = (await s.execute(select(func.count()).select_from(StockMovement).where(StockMovement.action_id == ctx.action_id))).scalar_one()
    return VerifyOutcome(n == 1, {"movements": n})


async def _undo_moves(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    async with write_session() as s:
        await tracking.undo_action_movements(s, ctx.action_id)


# =========================================================================== apply_sales
async def _apply_sales_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    d = date.fromisoformat(inputs["date"])
    async with write_session() as s:
        res = await tracking.apply_sales(s, ctx.business_id, d, action_id=ctx.action_id)
    return {"deducted": {k: str(v) for k, v in res.deducted.items()}, "missing_mapping": res.missing_mapping,
            "unit_errors": res.unit_errors, "sale_ids": res.sale_ids}


async def _apply_sales_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        moves = (await s.execute(select(StockMovement).where(StockMovement.action_id == ctx.action_id))).scalars().all()
        unapplied = (await s.execute(select(func.count()).select_from(Sale).where(
            Sale.business_id == ctx.business_id, Sale.date == date.fromisoformat(inputs["date"]),
            Sale.stock_applied.is_(False)))).scalar_one()
    ok = len(moves) == len(result.get("deducted", {})) and unapplied == 0
    return VerifyOutcome(ok, {"movements": len(moves), "unapplied_sales": unapplied})


async def _apply_sales_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    async with write_session() as s:
        await tracking.undo_action_movements(s, ctx.action_id)
        for sid in result.get("sale_ids", []):
            sale = await s.get(Sale, uuid.UUID(sid))
            if sale is not None:
                sale.stock_applied = False


# =========================================================================== generate_forecasts
async def _forecast_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    d = date.fromisoformat(inputs["date"])
    async with write_session() as s:
        methods = await demand.generate(s, ctx.business_id, d)
    return {"methods": methods, "generated_on": d.isoformat()}


async def _forecast_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        n = (await s.execute(select(func.count()).select_from(DemandForecast).where(
            DemandForecast.business_id == ctx.business_id,
            DemandForecast.generated_on == date.fromisoformat(inputs["date"])))).scalar_one()
    expected_items = len(result.get("methods", {}))
    return VerifyOutcome(n >= expected_items, {"rows": n, "items": expected_items})


async def _forecast_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    async with write_session() as s:
        await s.execute(delete(DemandForecast).where(DemandForecast.business_id == ctx.business_id,
                                                     DemandForecast.generated_on == date.fromisoformat(inputs["date"])))


# =========================================================================== defer / resume / resize (budget)
async def _open_requests_for(po_id: str) -> list[str]:
    from app.models.harness import ApprovalRequest

    async with read_session() as s:
        reqs = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.status == "pending",
                                                              ApprovalRequest.agent == "stock"))).scalars().all()
    return [str(r.id) for r in reqs if (r.context or {}).get("po_id") == po_id]


async def _defer_checks(ctx: ActionContext, inputs: dict[str, Any]) -> list[Check]:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
    ok = po is not None and po.status in ("draft", "pending_approval", "approved", "on_hold")
    return [Check("po_not_sent", ok, {"status": po.status if po else None}, cancel=not ok,
                  reason_en="the order was already sent or closed")]


async def _defer_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    from app.approvals import service as approvals

    for rid in await _open_requests_for(inputs["po_id"]):  # take back the waiting approval
        await approvals.withdraw(rid, "order deferred to stay within the purchasing budget")
    until = clock.today() + timedelta(days=int(inputs.get("days", 5)))
    async with write_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
        assert po is not None
        prev = po.status
        po.status = "on_hold"
        po.notes = {**(po.notes or {}), "deferred_until": until.isoformat(), "deferred_reason": inputs.get("reason", ""),
                    "budget": {**(po.notes or {}).get("budget", {}), "status": "deferred"}}
        return {"po_id": str(po.id), "previous_status": prev, "deferred_until": until.isoformat()}


async def _defer_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
    ok = po is not None and po.status == "on_hold" and po.notes.get("deferred_until") == result["deferred_until"]
    return VerifyOutcome(ok, {"status": po.status if po else None})


async def _defer_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    async with write_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
        if po is not None and result.get("previous_status"):
            po.status = "draft" if result["previous_status"] in ("pending_approval", "approved") else result["previous_status"]
            po.notes = {k: v for k, v in (po.notes or {}).items() if k not in ("deferred_until", "deferred_reason")}


async def _resume_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    async with write_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
        assert po is not None
        prev = po.status
        if po.status == "on_hold":
            po.status = "draft"
        # A deferred order goes out now: it can arrive no sooner than the supplier's lead time from today,
        # otherwise it would count as late the moment it is sent.
        sup = await s.get(Supplier, po.supplier_id)
        earliest = clock.today() + timedelta(days=math.ceil(supplier_perf.lead_time(sup)) if sup else 1)
        prev_expected = po.expected_date
        po.expected_date = max(po.expected_date, earliest)
        notes = {k: v for k, v in (po.notes or {}).items() if k not in ("deferred_until", "deferred_reason")}
        notes["budget"] = {**notes.get("budget", {}), "status": inputs.get("decision", "proceed")}
        po.notes = notes
        return {"po_id": str(po.id), "previous_status": prev, "previous_expected": prev_expected.isoformat()}


async def _resume_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
    return VerifyOutcome(po is not None and po.status in ("draft", "pending_approval", "approved"),
                         {"status": po.status if po else None})


async def _resize_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    """Scale an order down to fit `max_total_minor` (the budget left), keeping every line."""
    cap = int(inputs["max_total_minor"])
    async with write_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
        assert po is not None
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars().all()
        before = {str(ln.id): str(ln.qty) for ln in lines}
        factor = Decimal(cap) / Decimal(po.total.amount_minor) if po.total.amount_minor else Decimal(1)
        factor = min(Decimal(1), factor)
        for ln in lines:
            qty = (ln.qty * factor).quantize(Decimal("0.01"), rounding="ROUND_FLOOR")
            if ln.pack_size and ln.pack_size > 0:
                qty = (qty // ln.pack_size) * ln.pack_size
            ln.qty = max(qty, Decimal(0))
            ln.line_total = ln.unit_price.times(ln.qty)
        po.total = Money(sum(ln.line_total.amount_minor for ln in lines), po.total.currency)
        return {"po_id": str(po.id), "before": before, "total_minor": po.total.amount_minor}


async def _resize_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
    ok = po is not None and po.total.amount_minor <= int(inputs["max_total_minor"]) and po.total.amount_minor > 0
    return VerifyOutcome(ok, {"total_minor": po.total.amount_minor if po else None})


async def _resize_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    async with write_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(inputs["po_id"]))
        if po is None:
            return
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars().all()
        for ln in lines:
            if str(ln.id) in result.get("before", {}):
                ln.qty = Decimal(result["before"][str(ln.id)])
                ln.line_total = ln.unit_price.times(ln.qty)
        po.total = Money(sum(ln.line_total.amount_minor for ln in lines), po.total.currency)


def register_specs() -> None:
    register(ActionSpec(name="defer_po", agent="stock", risk_class="reversible", execute=_defer_execute,
                        title_en="Defer purchase order", title_ar="تأجيل أمر الشراء", preconditions=_defer_checks,
                        verify=_defer_verify, compensate=_defer_compensate))
    register(ActionSpec(name="resume_po", agent="stock", risk_class="reversible", execute=_resume_execute,
                        title_en="Resume purchase order", title_ar="استئناف أمر الشراء", verify=_resume_verify))
    register(ActionSpec(name="resize_po", agent="stock", risk_class="reversible", execute=_resize_execute,
                        title_en="Reduce purchase order to budget", title_ar="تخفيض أمر الشراء حسب الميزانية",
                        verify=_resize_verify, compensate=_resize_compensate))
    register(ActionSpec(name="draft_po", agent="stock", risk_class="reversible", execute=_draft_execute,
                        title_en="Draft purchase order", title_ar="مسودة أمر شراء", plan=_draft_plan,
                        preconditions=_draft_checks, verify=_draft_verify, compensate=_draft_compensate,
                        on_hold=_draft_on_hold, on_option=_draft_on_option, on_finalize=_draft_on_finalize))
    register(ActionSpec(name="send_po", agent="stock", risk_class="irreversible_external", execute=_send_execute,
                        title_en="Send purchase order", title_ar="إرسال أمر الشراء", preconditions=_send_checks,
                        verify=_send_verify, verifier_packet=_send_packet, auto_approve=_send_auto,
                        approval_request=_send_request, on_option=_send_on_option, on_finalize=_send_on_finalize))
    register(ActionSpec(name="record_delivery", agent="stock", risk_class="reversible", execute=_delivery_execute,
                        title_en="Record delivery", title_ar="تسجيل توريد", preconditions=_delivery_checks,
                        verify=_delivery_verify, compensate=_delivery_compensate))
    register(ActionSpec(name="adjust_stock", agent="stock", risk_class="reversible", execute=_adjust_execute,
                        title_en="Adjust stock", title_ar="تعديل المخزون", preconditions=_adjust_checks,
                        verify=_adjust_verify, compensate=_undo_moves))
    register(ActionSpec(name="apply_sales", agent="stock", risk_class="reversible", execute=_apply_sales_execute,
                        title_en="Deduct stock for sales", title_ar="خصم المخزون للمبيعات",
                        verify=_apply_sales_verify, compensate=_apply_sales_compensate))
    register(ActionSpec(name="generate_forecasts", agent="stock", risk_class="reversible", execute=_forecast_execute,
                        title_en="Update demand forecast", title_ar="تحديث توقعات الطلب",
                        verify=_forecast_verify, compensate=_forecast_compensate))
