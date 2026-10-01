"""Stock Agent workflows as LangGraph graphs (T061). State-changing steps run through harness_graph."""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from app.agents.stock import checks, demand, reorder, tracking, waste
from app.agents.stock import supplier as supplier_perf
from app.approvals import service as approvals
from app.core import settings_store
from app.core.country_profiles import calendar_for
from app.db.engine import read_session, write_session
from app.harness import calibration
from app.harness.graph import run_action
from app.harness.incidents import open_incident
from app.models.harness import CheckResult
from app.models.master import Item, RecipeLine, Supplier, SupplierPrice
from app.models.purchasing import OPEN_STATUSES, PurchaseOrder, PurchaseOrderLine
from app.models.tenancy import Business

AR_DAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]

# Weekly purchasing budget provider (the Cash-Flow Agent registers one in US3). None = unlimited.
BudgetProvider = Callable[[uuid.UUID, date], Awaitable[int | None]]
BUDGET_PROVIDER: list[BudgetProvider] = []


class DayState(TypedDict, total=False):
    business_id: str
    date: str
    notes: dict[str, Any]


def _ids(state: DayState) -> tuple[uuid.UUID, date]:
    return uuid.UUID(state["business_id"]), date.fromisoformat(state["date"])


async def _store_findings(bid: uuid.UUID, found: list[Any]) -> None:
    if not found:
        return
    from app.harness.audit import jsonable

    async with write_session() as s:
        for c in found:
            s.add(CheckResult(business_id=bid, check_name=c.name, passed=c.passed, details=jsonable(c.details)))


# ============================================================= stock_update_graph
async def su_apply(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    out = await run_action("apply_sales", {"date": d.isoformat()}, bid)
    return {"notes": {"apply": out.get("result") or {}}}


async def su_checks(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    applied = state.get("notes", {}).get("apply", {})
    async with read_session() as s:
        levels = await tracking.current_levels(s, bid)
        items = {i.id: i for i in (await s.execute(select(Item).where(Item.business_id == bid))).scalars()}
        gap = await checks.sales_data_gap(s, bid, d)
    found = checks.negative_or_impossible_stock(levels, items)
    if not gap.passed:
        found.append(gap)
    await _store_findings(bid, found)
    for c in found:
        key = f"{c.name}:{c.details.get('item_id') or d.isoformat()}"
        await open_incident(business_id=bid, agent="stock", type=c.name, detected_by=c.name, summary=c.reason_en,
                            refs={k: str(v) for k, v in c.details.items()}, dedupe_key=key,
                            action_taken="paused forecast updates for the day" if c.name == "sales_data_gap"
                            else "requested a stock count")
        await approvals.post_alert(business_id=bid, agent="stock", text_en=c.reason_en + ".",
                                   text_ar=(c.reason_ar or c.reason_en) + ".", dedupe_key=key, urgency=2)
    for name in applied.get("missing_mapping", []):
        await approvals.post_alert(business_id=bid, agent="stock",
                                   text_en=f"{name} was sold but has no recipe, so its ingredients were not deducted.",
                                   text_ar=f"تم بيع {name} بدون وصفة، لذا لم تُخصم مكوناته.",
                                   dedupe_key=f"missing_mapping:{name}")
    return {"notes": {**state.get("notes", {}), "findings": [c.name for c in found]}}


def build_stock_update() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DayState)
    g.add_node("apply_sales", su_apply)
    g.add_node("checks", su_checks)
    g.add_edge(START, "apply_sales")
    g.add_edge("apply_sales", "checks")
    g.add_edge("checks", END)
    return g


# ============================================================= forecast_check_graph
async def fc_evaluate(state: DayState) -> dict[str, Any]:
    """Compare yesterday's forecast with actual sales; switch unreliable items to the safer method."""
    bid, d = _ids(state)
    threshold = float(await settings_store.get(bid, "forecast_mape_threshold"))
    switched: list[dict[str, Any]] = []
    errors: list[float] = []
    async with read_session() as s:
        products = list((await s.execute(select(Item).where(Item.business_id == bid, Item.is_sold.is_(True)))).scalars())
        gap = await checks.sales_data_gap(s, bid, d - timedelta(days=1))
        per_item = await demand.forecast_errors(s, bid, [p.id for p in products], d) if gap.passed else {}
    if not gap.passed:
        return {"notes": {"paused": "sales data gap yesterday"}}
    found = []
    for p in products:
        y, m7 = per_item.get(p.id, (None, None))
        if y is not None:
            errors.append(y)
        worst = max(v for v in (y, m7) if v is not None) if (y is not None or m7 is not None) else None
        c = checks.forecast_accuracy(p, worst, threshold)
        found.append(c)
        if not c.passed and p.forecast_method != "same_weekday_avg":
            switched.append({"item_id": str(p.id), "name_en": p.name_en, "name_ar": p.name_ar, "error": worst})
    await _store_findings(bid, [c for c in found if not c.passed])
    if switched:
        async with write_session() as s:
            ing_ids: set[uuid.UUID] = set()
            for sw in switched:
                item = await s.get(Item, uuid.UUID(sw["item_id"]))
                if item is not None:
                    item.forecast_method = "same_weekday_avg"
                for r in (await s.execute(select(RecipeLine).where(RecipeLine.sold_item_id == uuid.UUID(sw["item_id"])))).scalars():
                    ing_ids.add(r.ingredient_item_id)
            for iid in ing_ids:  # raise safety stock while demand is unusual
                ing = await s.get(Item, iid)
                if ing is not None:
                    ing.safety_stock = (ing.safety_stock * Decimal("1.5")).quantize(Decimal("0.01"))
    agent_error = sum(errors) / len(errors) if errors else 0.0
    cal = await calibration.record(bid, "stock", "forecast_mape", agent_error, d, threshold, safe_method="same_weekday_avg",
                                   open_incident=False)
    if switched or cal["state"] == "degraded":
        names_en = ", ".join(f"{sw['name_en']} ({sw['error'] * 100:.0f}%)" for sw in switched)
        names_ar = "، ".join(f"{sw['name_ar']} ({sw['error'] * 100:.0f}%)" for sw in switched)
        inc = await open_incident(
            business_id=bid, agent="stock", type="forecast_accuracy", detected_by="forecast_accuracy",
            summary=f"Forecast error jumped for {names_en or 'several items'}",
            refs={"items": switched, "agent_error": agent_error}, dedupe_key=f"forecast_accuracy:{d.isoformat()}",
            action_taken="lowered confidence, switched to the 4-same-weekday average, raised safety stock")
        from app.harness.analysis import analyse_and_propose

        await analyse_and_propose(inc)
        await approvals.post_alert(
            business_id=bid, agent="stock", urgency=2, dedupe_key=f"forecast_accuracy:{d.isoformat()}",
            context={"incident_id": str(inc)},
            text_en=f"Sales were far from my forecast yesterday for {names_en}. I switched to a safer method "
                    f"(average of the last 4 same weekdays) and raised safety stock. Possible cause: an event nearby.",
            text_ar=f"جاءت المبيعات بعيدة عن توقعاتي أمس لـ {names_ar}. انتقلت إلى طريقة أكثر أماناً "
                    f"(متوسط آخر 4 أيام مماثلة) ورفعت مخزون الأمان. سبب محتمل: حدث قريب.")
    return {"notes": {"switched": switched, "calibration": cal, "agent_error": agent_error}}


async def fc_generate(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    await run_action("generate_forecasts", {"date": d.isoformat()}, bid)
    return {}


def build_forecast_check() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DayState)
    g.add_node("evaluate", fc_evaluate)
    g.add_node("generate", fc_generate)
    g.add_edge(START, "evaluate")
    g.add_edge("evaluate", "generate")
    g.add_edge("generate", END)
    return g


# ============================================================= reorder_graph
async def plan_proposals(bid: uuid.UUID, d: date, assume_missing: set[uuid.UUID] | None = None) -> list[reorder.Proposal]:
    """Reorder proposals for every purchased item. `assume_missing` = POs treated as not arriving."""
    assume_missing = assume_missing or set()
    async with read_session() as s:
        business = await s.get(Business, bid)
        assert business is not None
        cal = calendar_for(business)
        gen = await demand.latest_generation(s, bid, d)
        if gen is None:
            return []
        fc = await demand.daily_demand(s, bid, gen)
        levels = await tracking.current_levels(s, bid)
        items = [i for i in (await s.execute(select(Item).where(Item.business_id == bid, Item.is_ingredient.is_(True)))).scalars()]
        suppliers = {sp.id: sp for sp in (await s.execute(select(Supplier).where(Supplier.business_id == bid))).scalars()}
        prices: dict[tuple[uuid.UUID, uuid.UUID], SupplierPrice] = {}
        for p in (await s.execute(select(SupplierPrice).where(SupplierPrice.business_id == bid, SupplierPrice.valid_from <= d)
                                  .order_by(SupplierPrice.valid_from))).scalars():
            prices[(p.supplier_id, p.item_id)] = p
        incoming: dict[uuid.UUID, dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
        # Items already on an unsent order (draft / awaiting approval / approved / on hold) wait for the
        # owner's answer; proposing them again would only create duplicate orders.
        awaiting_owner: set[uuid.UUID] = set()
        open_pos = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.business_id == bid,
                                                                PurchaseOrder.status.in_(OPEN_STATUSES)))).scalars().all()
        for po in open_pos:
            lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars().all()
            if po.status not in ("sent", "partially_received"):
                awaiting_owner.update(ln.item_id for ln in lines)
                continue
            if po.id in assume_missing:
                continue
            arrive = max(po.expected_date, d)
            for ln in lines:
                incoming[ln.item_id][arrive] += ln.qty
    items = [i for i in items if i.id not in awaiting_owner]
    proposals: list[reorder.Proposal] = []
    for item in items:
        sup = suppliers.get(item.preferred_supplier_id) if item.preferred_supplier_id else None
        price = prices.get((sup.id, item.id)) if sup else None
        if sup is None or price is None:
            continue
        p = reorder.plan_item(reorder.ItemPlanInput(
            item_id=item.id, name=item.name_en, unit=item.unit, stock=levels.get(item.id, Decimal(0)),
            daily_demand=fc.get(item.id, {}), incoming=dict(incoming.get(item.id, {})),
            lead_time_days=supplier_perf.lead_time(sup), safety_stock=item.safety_stock,
            min_order_qty=price.min_order_qty, pack_size=price.pack_size, shelf_life_days=item.shelf_life_days,
            storage_capacity=item.storage_capacity, unit_price_minor=price.price.amount_minor, supplier_id=sup.id,
            is_critical=item.is_critical, margin_class=item.margin_class), d, cal)
        if p is not None:
            proposals.append(p)
    return proposals


async def ro_late(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    handled = []
    async with write_session() as s:
        late = await checks.late_deliveries(s, bid, d)
        flagged = []
        for entry in late:
            po = entry["po"]
            sup = await s.get(Supplier, po.supplier_id)
            if sup is not None and supplier_perf.record_late(s, sup, po, entry["days_late"]):
                flagged.append({"po_id": po.id, "number": po.number, "supplier_en": sup.name_en, "supplier_ar": sup.name_ar,
                                "days_late": entry["days_late"], "critical": entry["critical"],
                                "items": [(entry["items"][ln.item_id].name_en, entry["items"][ln.item_id].name_ar, ln.item_id)
                                          for ln in entry["lines"] if ln.item_id in entry["items"]]})
    for f in flagged:
        # Recompute cover assuming this delivery has not arrived.
        props = await plan_proposals(bid, d, assume_missing={f["po_id"]})
        by_item = {p.item_id: p for p in props}
        runs_out = [(en, ar, by_item[iid].projected_stockout) for en, ar, iid in f["items"]
                    if iid in by_item and by_item[iid].projected_stockout]
        when_en = when_ar = ""
        if runs_out:
            en, ar, so = min(runs_out, key=lambda r: r[2])
            when_en = f"; {en.lower()} runs out {so.strftime('%A')}"
            when_ar = f"؛ سينفد {ar} يوم {AR_DAYS[so.weekday()]}"
        from app.harness.action_spec import OwnerAsk

        ask = OwnerAsk(
            kind="question",
            text_en=f"{f['supplier_en']} delivery ({f['number']}) is {f['days_late']} day(s) late{when_en}.",
            text_ar=f"توريد {f['supplier_ar']} ({f['number']}) متأخر {f['days_late']} يوم{when_ar}.",
            options=[{"key": "wait", "label_en": "Wait", "label_ar": "انتظر", "effect": "ack"},
                     {"key": "reorder_other", "label_en": "Reorder from another supplier", "label_ar": "اطلب من مورد آخر",
                      "effect": "reorder_other"},
                     {"key": "call", "label_en": "I'll call the supplier", "label_ar": "سأتصل بالمورد", "effect": "ack"}],
            safe_default="wait", context={"po_id": str(f["po_id"])})
        await approvals.post_question(business_id=bid, agent="stock", ask=ask, key=f"late:{f['po_id']}")
        if f["critical"] and runs_out and (runs_out[0][2] - d).days <= f["days_late"] + 1:
            await open_incident(business_id=bid, agent="stock", type="late_delivery", detected_by="late_delivery",
                                summary=f"Critical delivery {f['number']} from {f['supplier_en']} is {f['days_late']} day(s) late",
                                refs={"po_id": str(f["po_id"])}, dedupe_key=f"late:{f['po_id']}",
                                action_taken="alerted the owner; lowered supplier reliability once")
        handled.append(str(f["po_id"]))
    return {"notes": {"late": handled}}


async def ro_order(state: DayState) -> dict[str, Any]:
    bid, d = _ids(state)
    proposals = await plan_proposals(bid, d)
    budget = None
    for provider in BUDGET_PROVIDER:
        budget = await provider(bid, d)
    orders = reorder.group_by_supplier(proposals, budget)
    async with read_session() as s:
        gen = await demand.latest_generation(s, bid, d)
    results = []
    for order in orders:
        if not order.lines:
            continue
        stockout = order.earliest_stockout
        inputs = {
            "supplier_id": str(order.supplier_id),
            "lines": [{"item_id": str(p.item_id), "qty": str(p.qty), "unit": p.unit, "pack_size": str(p.pack_size),
                       "unit_price_minor": p.unit_price_minor} for p in order.lines],
            "expected_date": order.expected_date.isoformat(),
            "projected_stockout": stockout.isoformat() if stockout else None,
            "reason": "; ".join(f"{p.name} {p.reason}" for p in order.lines),
            "is_critical": order.is_critical,
            "forecast_generated_on": gen.isoformat() if gen else None,
            "deferred": [str(p.item_id) for p in order.deferred],
        }
        drafted = await run_action("draft_po", inputs, bid)
        entry = {"supplier_id": str(order.supplier_id), "draft": drafted["outcome"], "interrupted": drafted["interrupted"]}
        if drafted["outcome"] == "completed" and drafted["result"]:
            if await budget_decision_pending(uuid.UUID(drafted["result"]["po_id"])):
                entry["send"] = "waiting_for_budget_decision"  # conflict_graph asks the owner first
            else:
                sent = await run_action("send_po", {"po_id": drafted["result"]["po_id"]}, bid,
                                        parent_action_id=drafted["action_id"])
                entry["send"] = "awaiting_approval" if sent["interrupted"] else sent["outcome"]
        results.append(entry)
    return {"notes": {**state.get("notes", {}), "orders": results}}


async def budget_decision_pending(po_id: uuid.UUID) -> bool:
    async with read_session() as s:
        po = await s.get(PurchaseOrder, po_id)
    return po is not None and (po.notes or {}).get("budget", {}).get("status") == "conflict"


async def ro_deferred(state: DayState) -> dict[str, Any]:
    """Orders deferred for the budget go ahead when their date comes (through the normal approval)."""
    bid, d = _ids(state)
    async with read_session() as s:
        held = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.business_id == bid,
                                                            PurchaseOrder.status == "on_hold"))).scalars().all()
    resumed = []
    for po in held:
        until = (po.notes or {}).get("deferred_until")
        if not until or date.fromisoformat(until) > d:
            continue
        out = await run_action("resume_po", {"po_id": str(po.id), "decision": "resumed"}, bid)
        if out["outcome"] == "completed":
            await run_action("send_po", {"po_id": str(po.id)}, bid, parent_action_id=out["action_id"])
            resumed.append(po.number)
    return {"notes": {**state.get("notes", {}), "resumed": resumed}}


async def ro_expiry(state: DayState) -> dict[str, Any]:
    """Flag stock that will expire before it is used, and dead stock (FR-022)."""
    bid, d = _ids(state)
    async with read_session() as s:
        gen = await demand.latest_generation(s, bid, d)
        fc = await demand.daily_demand(s, bid, gen) if gen else {}
        levels = await tracking.current_levels(s, bid)
        items = [i for i in (await s.execute(select(Item).where(Item.business_id == bid, Item.is_ingredient.is_(True)))).scalars()]
        dead_days = int(await settings_store.get(bid, "dead_stock_days", s))
        dead = await waste.dead_stock(s, bid, d, dead_days)
    risks = []
    for item in items:
        week = [fc.get(item.id, {}).get(d + timedelta(days=i), Decimal(0)) for i in range(7)]
        avg = sum(week) / 7
        r = waste.expiry_risk(item, levels.get(item.id, Decimal(0)), avg)
        if r:
            risks.append({"item": r.name, "stock": str(r.stock), "days_to_sell": round(r.days_to_sell, 1),
                          "shelf_life_days": r.shelf_life_days, "suggestion": r.suggestion})
    return {"notes": {**state.get("notes", {}), "expiry_risk": risks, "dead_stock": [str(i) for i in dead]}}


def build_reorder() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DayState)
    g.add_node("late_deliveries", ro_late)
    g.add_node("deferred", ro_deferred)
    g.add_node("order", ro_order)
    g.add_node("expiry", ro_expiry)
    g.add_edge(START, "late_deliveries")
    g.add_edge("late_deliveries", "deferred")
    g.add_edge("deferred", "order")
    g.add_edge("order", "expiry")
    g.add_edge("expiry", END)
    return g


# ============================================================= delivery_graph
class DeliveryState(TypedDict, total=False):
    business_id: str
    inputs: dict[str, Any]
    result: dict[str, Any]


async def dg_record(state: DeliveryState) -> dict[str, Any]:
    out = await run_action("record_delivery", state["inputs"], uuid.UUID(state["business_id"]))
    return {"result": {"outcome": out["outcome"], **(out["result"] or {})}}


async def dg_flag(state: DeliveryState) -> dict[str, Any]:
    res = state.get("result", {})
    disc = res.get("discrepancies") or []
    if res.get("outcome") != "completed" or not disc:
        return {}
    bid = uuid.UUID(state["business_id"])
    async with read_session() as s:
        po = await s.get(PurchaseOrder, uuid.UUID(state["inputs"]["po_id"]))
        sup = await s.get(Supplier, po.supplier_id) if po else None
        items = {str(i.id): i for i in (await s.execute(select(Item).where(Item.business_id == bid))).scalars()}
    parts_en, parts_ar = [], []
    for dsc in disc:
        it = items.get(dsc["item_id"])
        if dsc["kind"] in ("quantity", "missing"):
            parts_en.append(f"{dsc['received']} {it.unit if it else ''} {it.name_en if it else ''} instead of {dsc['ordered']}")
            parts_ar.append(f"{dsc['received']} {it.unit if it else ''} {it.name_ar if it else ''} بدلاً من {dsc['ordered']}")
        else:
            parts_en.append(f"{it.name_en if it else ''} price differs from the order")
            parts_ar.append(f"سعر {it.name_ar if it else ''} يختلف عن أمر الشراء")
    await approvals.post_alert(
        business_id=bid, agent="stock", urgency=2, dedupe_key=f"delivery:{res.get('delivery_id')}",
        text_en=f"Delivery from {sup.name_en if sup else 'supplier'} ({po.number if po else ''}): " + "; ".join(parts_en)
                + ". I recorded what arrived and noted it on the supplier.",
        text_ar=f"توريد من {sup.name_ar if sup else 'المورد'} ({po.number if po else ''}): " + "؛ ".join(parts_ar)
                + ". سجلت ما وصل ودوّنت ذلك على المورد.")
    return {}


def build_delivery() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DeliveryState)
    g.add_node("record", dg_record)
    g.add_node("flag", dg_flag)
    g.add_edge(START, "record")
    g.add_edge("record", "flag")
    g.add_edge("flag", END)
    return g


# ============================================================= daily steps
async def _run(graph: str, bid: uuid.UUID, d: date) -> Any:
    from app.graphs import runtime

    return await runtime.start(graph, {"business_id": str(bid), "date": d.isoformat(), "notes": {}},
                               f"{graph}:{bid}:{d.isoformat()}:{uuid.uuid4().hex[:6]}")


async def step_stock_update(bid: uuid.UUID, d: date) -> Any:
    return await _run("stock_update", bid, d)


async def step_forecast_check(bid: uuid.UUID, d: date) -> Any:
    return await _run("forecast_check", bid, d)


async def step_reorder(bid: uuid.UUID, d: date) -> Any:
    return await _run("reorder", bid, d)


async def record_delivery(business_id: uuid.UUID, inputs: dict[str, Any]) -> dict[str, Any]:
    from app.graphs import runtime

    out = await runtime.start("delivery", {"business_id": str(business_id), "inputs": inputs, "result": {}},
                              f"delivery:{uuid.uuid4().hex}")
    return dict(out["values"].get("result") or {})


def register_graphs() -> None:
    from app.graphs import runtime
    from app.graphs.daily_run import register_step

    runtime.register("stock_update", build_stock_update)
    runtime.register("forecast_check", build_forecast_check)
    runtime.register("reorder", build_reorder)
    runtime.register("delivery", build_delivery)
    register_step("stock_update", "stock_update_graph", step_stock_update)
    register_step("forecast_check", "forecast_check_graph", step_forecast_check)
    register_step("reorder", "reorder_graph", step_reorder)
