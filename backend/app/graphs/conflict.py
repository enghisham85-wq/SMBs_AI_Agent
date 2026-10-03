"""conflict_graph: Stock wants to order, Cash-Flow says the budget does not cover it.

gather both positions with figures -> recommend -> ask the owner (interrupt) -> apply -> publish.

A critical item's stockout outranks the budget: its order goes ahead and the owner is told, and the
question becomes whether to delay other, non-critical orders that can wait instead.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from app.approvals import service as approvals
from app.core import clock
from app.core.events import publish
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.action_spec import OwnerAsk
from app.harness.graph import run_action
from app.models.master import Item, Supplier
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine

GRAPH = "conflict"
DEFER_DAYS = 5


class ConflictState(TypedDict, total=False):
    business_id: str
    po_id: str
    thread_id: str
    check: dict[str, Any]
    positions: dict[str, Any]
    recommendation: str
    others: list[dict[str, Any]]
    answer: dict[str, Any] | None
    decision: str


def _m(v: dict[str, Any] | None, cur: str) -> Money | None:
    return Money(int(v["amount_minor"]), cur) if v else None


async def gather(state: ConflictState) -> dict[str, Any]:
    po_id = uuid.UUID(state["po_id"])
    bid = uuid.UUID(state["business_id"])
    chk = state["check"]
    async with read_session() as s:
        po = await s.get(PurchaseOrder, po_id)
        assert po is not None
        sup = await s.get(Supplier, po.supplier_id)
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po_id))).scalars().all()
        items = {i.id: i for i in (await s.execute(select(Item).where(Item.id.in_([ln.item_id for ln in lines])))).scalars()}
        # Other unsent, non-critical orders that could wait instead.
        from app.agents.stock.handlers import can_wait

        others = []
        for o in (await s.execute(select(PurchaseOrder).where(
                PurchaseOrder.business_id == bid, PurchaseOrder.id != po_id,
                PurchaseOrder.status.in_(("draft", "pending_approval", "approved"))))).scalars():
            if await can_wait(o, clock.today(), DEFER_DAYS):
                others.append({"po_id": str(o.id), "number": o.number, "total": o.total.to_json()})
    cur = po.total.currency
    names_en = ", ".join(items[ln.item_id].name_en for ln in lines if ln.item_id in items)
    names_ar = "، ".join(items[ln.item_id].name_ar for ln in lines if ln.item_id in items)
    stockout = (po.notes or {}).get("projected_stockout")
    left = _m(chk.get("remaining_budget"), cur)
    positions = {
        "stock": {"po_number": po.number, "items_en": names_en, "items_ar": names_ar, "total": po.total.to_json(),
                  "supplier_en": sup.name_en if sup else "", "supplier_ar": sup.name_ar if sup else "",
                  "needed_by": stockout, "critical": po.is_critical_order},
        "cashflow": {"remaining_budget": left.to_json() if left else None,
                     "weekly_budget": chk.get("weekly_budget"), "tightened": chk.get("tightened"),
                     "reason": chk.get("budget_reason")},
    }
    return {"positions": positions, "others": others}


async def recommend(state: ConflictState) -> dict[str, Any]:
    """A critical item outranks the budget; so does an item that would run out before a deferred order could
    arrive (deferring it only guarantees the stockout). Otherwise the Cash-Flow recommendation stands."""
    from datetime import timedelta

    st = state["positions"]["stock"]
    needed_by = date.fromisoformat(st["needed_by"]) if st.get("needed_by") else None
    runs_out_first = needed_by is not None and needed_by <= clock.today() + timedelta(days=DEFER_DAYS)
    rec = "proceed" if st["critical"] or runs_out_first else state["check"]["recommendation"]
    return {"recommendation": rec}


def _texts(state: ConflictState) -> tuple[str, str]:
    st, cf = state["positions"]["stock"], state["positions"]["cashflow"]
    total = Money(int(st["total"]["amount_minor"]), st["total"]["currency"])
    left = Money(int(cf["remaining_budget"]["amount_minor"]), cf["remaining_budget"]["currency"]) if cf["remaining_budget"] else None
    need_en = f" (runs out {date.fromisoformat(st['needed_by']).strftime('%d %b')})" if st.get("needed_by") else ""
    need_ar = f" (سينفد {st['needed_by']})" if st.get("needed_by") else ""
    left_en = left.to_display() if left else "nothing"
    left_ar = left.to_display("ar") if left else "لا شيء"
    en = (f"Stock Agent: order {st['items_en']}{need_en} from {st['supplier_en']}, {total.to_display()}. "
          f"Cash-Flow Agent: only {left_en} is left in this week's purchasing budget.")
    ar = (f"وكيل المخزون: طلب {st['items_ar']}{need_ar} من {st['supplier_ar']} بقيمة {total.to_display('ar')}. "
          f"وكيل التدفق النقدي: المتبقي من ميزانية المشتريات هذا الأسبوع {left_ar} فقط.")
    return en, ar


async def ask(state: ConflictState) -> dict[str, Any]:
    bid = uuid.UUID(state["business_id"])
    en, ar = _texts(state)
    st = state["positions"]["stock"]
    others = state.get("others") or []
    if st["critical"]:
        await approvals.post_alert(
            business_id=bid, agent="harness", urgency=2, dedupe_key=f"conflict:{state['po_id']}",
            context={"po_id": state["po_id"], "kind": "conflict"},
            text_en=en + f" {st['items_en']} is critical, so running out outranks the budget: I am going ahead with {st['po_number']}.",
            text_ar=ar + f" {st['items_ar']} صنف حرج، لذا تجنب نفاده أهم من الميزانية: سأمضي في {st['po_number']}.")
        if not others:
            return {"answer": {"effect": "proceed", "via": "system"}}
        numbers = ", ".join(o["number"] for o in others)
        ask_ = OwnerAsk(kind="question", text_en=f"To stay within budget, delay {numbers} (not critical) by {DEFER_DAYS} days?",
                        text_ar=f"للبقاء ضمن الميزانية، هل أؤجل {numbers} (غير حرجة) {DEFER_DAYS} أيام؟",
                        options=[{"key": "defer_others", "label_en": f"Delay {DEFER_DAYS} days", "label_ar": f"أجّل {DEFER_DAYS} أيام",
                                  "effect": "defer_others"},
                                 {"key": "keep", "label_en": "Keep them", "label_ar": "أبقها", "effect": "keep"}],
                        required_role="manager", safe_default="keep",
                        context={"po_id": state["po_id"], "others": others, "kind": "conflict"})
    else:
        rec = state["recommendation"]
        rec_en = {"defer": f"delay it {DEFER_DAYS} days", "reduce": "order only what fits the budget"}.get(rec, "order it")
        rec_ar = {"defer": f"تأجيله {DEFER_DAYS} أيام", "reduce": "طلب ما تسمح به الميزانية فقط"}.get(rec, "طلبه")
        options = [{"key": "defer", "label_en": f"Delay {DEFER_DAYS} days", "label_ar": f"أجّل {DEFER_DAYS} أيام", "effect": "defer"},
                   {"key": "proceed", "label_en": "Order anyway", "label_ar": "اطلب على أي حال", "effect": "proceed"}]
        if rec == "reduce":
            options.insert(0, {"key": "reduce", "label_en": "Order what fits", "label_ar": "اطلب ما يناسب الميزانية",
                               "effect": "reduce"})
        ask_ = OwnerAsk(kind="question", text_en=f"{en} Recommended: {rec_en}.", text_ar=f"{ar} المقترح: {rec_ar}.",
                        options=options, required_role="manager", safe_default="defer",
                        context={"po_id": state["po_id"], "positions": state["positions"], "recommendation": rec,
                                 "kind": "conflict"})
    answer = await approvals.ask_owner(business_id=bid, graph_name=GRAPH, thread_id=state["thread_id"], gate_key="decision",
                                       ask=ask_, agent="harness")
    return {"answer": answer}


async def apply(state: ConflictState) -> dict[str, Any]:
    bid = uuid.UUID(state["business_id"])
    ans = state.get("answer") or {}
    effect = ans.get("effect") or "proceed"
    po_id = state["po_id"]
    if effect == "reask":  # timed out without a safe default we can act on: keep the recommendation
        effect = "defer" if state["recommendation"] == "defer" else "proceed"
    if effect in ("defer", "reduce", "proceed") and not state["positions"]["stock"]["critical"]:
        if effect == "defer":
            await run_action("defer_po", {"po_id": po_id, "days": DEFER_DAYS, "reason": "over the weekly purchasing budget"}, bid)
        else:
            if effect == "reduce":
                left = state["positions"]["cashflow"]["remaining_budget"]
                if left and int(left["amount_minor"]) > 0:
                    await run_action("resize_po", {"po_id": po_id, "max_total_minor": int(left["amount_minor"])}, bid)
            resumed = await run_action("resume_po", {"po_id": po_id, "decision": effect}, bid)
            await run_action("send_po", {"po_id": po_id}, bid, parent_action_id=resumed["action_id"])
    elif effect == "defer_others":
        for o in state.get("others") or []:
            await run_action("defer_po", {"po_id": o["po_id"], "days": DEFER_DAYS,
                                          "reason": f"making room for critical order {state['positions']['stock']['po_number']}"}, bid)
    async with write_session() as s:
        publish(s, "budget.conflict_resolved", {"po_id": po_id, "decision": effect, "recommendation": state["recommendation"],
                                                "decided_by": ans.get("via") or "owner",
                                                "timed_out": bool(ans.get("timed_out"))},
                producer="harness", business_id=bid)
    return {"decision": effect}


def build() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(ConflictState)
    for name, fn in (("gather", gather), ("recommend", recommend), ("ask", ask), ("apply", apply)):
        g.add_node(name, fn)
    g.add_edge(START, "gather")
    g.add_edge("gather", "recommend")
    g.add_edge("recommend", "ask")
    g.add_edge("ask", "apply")
    g.add_edge("apply", END)
    return g


async def start_conflict(business_id: uuid.UUID, po_id: uuid.UUID, check: dict[str, Any]) -> dict[str, Any]:
    from app.graphs import runtime

    thread = f"{GRAPH}:{po_id}:{uuid.uuid4().hex[:6]}"
    return await runtime.start(GRAPH, {"business_id": str(business_id), "po_id": str(po_id), "thread_id": thread,
                                       "check": check}, thread)


def register() -> None:
    from app.graphs import runtime

    runtime.register(GRAPH, build)
