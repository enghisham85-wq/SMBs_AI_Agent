"""3-month replay of the sample cafe vs. the no-assistant baseline. Slow, run with -m slow (~10 min)."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app import wiring
from app.agents.cashflow import position
from app.agents.stock import tracking
from app.agents.stock.graphs import record_delivery
from app.approvals import service as approvals
from app.config import get_settings
from app.core import clock, scheduler, settings_store
from app.db.engine import read_session
from app.harness import rules
from app.harness.graph import run_action
from app.models.books import ReceivableInvoice
from app.models.cash import ShortfallPlan
from app.models.finance_master import BankTransaction, Sale
from app.models.harness import ApprovalRequest, LearnedRule
from app.models.master import Item, RecipeLine, Supplier
from app.models.purchasing import PurchaseOrder, PurchaseOrderLine
from app.models.stock_ops import StockMovement
from app.models.tenancy import Business, User
from app.seed.sample_cafe import seed
from tests.integration.baseline_policy import Event, Shelf, baseline_orders, simulate_stock, usage_by_day

pytestmark = pytest.mark.slow

START = date(2026, 10, 4)
DAYS = int(os.environ.get("SAMPLE_METRICS_DAYS", "91"))  # shorter only for a quick local check
SECONDS_PER_ANSWER = 30  # read the request and tap
SECONDS_PER_ALERT = 10  # read a notice
REPORT = Path(__file__).resolve().parents[2] / "var" / "reports" / "sample_metrics.json"
# Ground truth for bank matching, straight from the demo feed.
EXPECTED_MATCH = {"cash_sales": "sales_cash", "card_settlement": "settlement", "transfer_sales": "settlement",
                  "card_fee": "card_fee", "cash_deposit_out": "transfer", "cash_deposit_in": "transfer",
                  "obligation": "obligation", "supplier_payment": "supplier_invoice", "customer_payment": "customer_payment"}


@pytest.fixture
async def cafe(db: None, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    wiring.register_all()
    return await seed(start_date=START, history_days=90)


class Owner:
    def __init__(self, u: User) -> None:
        self.id, self.role, self.username = u.id, u.role, u.username


def _choice(req: ApprovalRequest) -> str:
    """The simulated owner always takes the advice."""
    keys = [o["key"] for o in req.options]
    recommended = (req.context or {}).get("recommendation")
    if recommended in keys:
        return str(recommended)
    plan_steps = [k for k in keys if k.startswith("do:")]
    if plan_steps:
        return plan_steps[0]
    if req.kind == "approval" and "approve" in keys:
        return "approve"
    if "continue" in keys:
        return "continue"
    if req.safe_default and req.safe_default in keys:
        return req.safe_default
    return keys[0]


async def _owner_round(bid: uuid.UUID, owner: Owner, seen_alerts: set[uuid.UUID]) -> tuple[int, int]:
    """One pass over open requests; new ones wait till tomorrow. Returns (answers, alerts read)."""
    async with read_session() as s:
        pending = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.business_id == bid,
                                                                 ApprovalRequest.status == "pending")
                                   .order_by(ApprovalRequest.created_at))).scalars().all()
    answers = alerts = 0
    # Accept every routine-order offer.
    async with read_session() as s:
        offers = [r for r in (await s.execute(select(LearnedRule).where(
            LearnedRule.business_id == bid, LearnedRule.kind == "policy", LearnedRule.status == "proposed"))).scalars()
            if r.trigger.get("auto_approve_up_to_minor") is not None]
    for rule in offers:
        await rules.approve(rule.id, bid, owner.id)
        answers += 1
    for req in pending:
        if req.kind == "alert":
            if req.id not in seen_alerts:
                seen_alerts.add(req.id)
                alerts += 1
            continue
        res = await approvals.resolve(str(req.id), _choice(req), owner, "dashboard")  # type: ignore[arg-type]
        if res.status == "resolved":
            answers += 1
    return answers, alerts


async def _staff_deliveries(bid: uuid.UUID, d: date) -> int:
    async with read_session() as s:
        due = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.business_id == bid, PurchaseOrder.status == "sent",
                                                           PurchaseOrder.expected_date <= d))).scalars().all()
        lines = {po.id: (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars().all()
                 for po in due}
    for po in due:
        await record_delivery(bid, {"po_id": str(po.id), "received_on": d.isoformat(),
                                    "lines": [{"item_id": str(ln.item_id), "qty_received": str(ln.qty)} for ln in lines[po.id]]})
    return len(due)


def _by_item(episodes: list[tuple[uuid.UUID, date]], items: dict[uuid.UUID, Item]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for iid, _ in episodes:
        out[items[iid].name_en if iid in items else str(iid)] += 1
    return dict(sorted(out.items()))


def _names(counts: dict[uuid.UUID, int], items: dict[uuid.UUID, Item]) -> dict[str, int]:
    return dict(sorted((items[i].name_en if i in items else str(i), n) for i, n in counts.items()))


async def _staff_spoilage(bid: uuid.UUID, d: date, shelf: Shelf, items: dict[uuid.UUID, Item],
                          recipes: list[RecipeLine]) -> None:
    """Record this morning's spoilage and count sold-out items as zero."""
    async with read_session() as s:
        sales = list((await s.execute(select(Sale).where(Sale.business_id == bid, Sale.date == d))).scalars())
        arrived = list((await s.execute(select(StockMovement).where(
            StockMovement.business_id == bid, StockMovement.type == "purchase", StockMovement.date == d))).scalars())
        levels = await tracking.current_levels(s, bid)
    expired = shelf.step(d, [Event(m.date, m.item_id, m.quantity) for m in arrived],
                         usage_by_day(sales, items, recipes).get(d, {}))
    for iid, qty in expired.items():
        on_hand = levels.get(iid, Decimal(0))
        if on_hand > 0:
            await run_action("adjust_stock", {"item_id": str(iid), "qty_delta": str(-min(qty, on_hand)), "type": "spoilage",
                                              "reason": "past its shelf life, thrown away", "source": "user:staff"}, bid)
    # Sold out is zero, never negative.
    for iid, on_hand in levels.items():
        if on_hand < 0:
            await run_action("adjust_stock", {"item_id": str(iid), "qty_delta": str(-on_hand), "type": "count_correction",
                                              "reason": "counted after selling out", "source": "user:staff"}, bid)


async def _total_balance(bid: uuid.UUID, d: date) -> int:
    async with read_session() as s:
        return sum(a.balance_minor for a in await position.account_balances(s, bid, d))


async def test_three_month_replay_meets_the_success_criteria(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    async with read_session() as s:
        owner_user = (await s.execute(select(User).where(User.business_id == bid, User.role == "owner"))).scalar_one()
        opening = dict(await tracking.current_levels(s, bid))
        business = await s.get(Business, bid)
        manual_hours = float(await settings_store.get(bid, "manual_bookkeeping_hours_per_week", s))
    assert business is not None
    owner = Owner(owner_user)
    first = clock.today() + timedelta(days=1)
    buffer = business.min_cash_buffer.amount_minor

    weekly_answers: dict[int, int] = defaultdict(int)
    weekly_alerts: dict[int, int] = defaultdict(int)
    seen_alerts: set[uuid.UUID] = set()
    balances: dict[date, int] = {}
    async with read_session() as s:
        items = {i.id: i for i in (await s.execute(select(Item).where(Item.business_id == bid))).scalars()}
        recipes = list((await s.execute(select(RecipeLine).where(RecipeLine.business_id == bid))).scalars())
    shelf = Shelf(opening, items, first)
    for i in range(DAYS):
        await scheduler.advance(bid, days=1)
        d = clock.today()
        await _staff_deliveries(bid, d)
        await _staff_spoilage(bid, d, shelf, items, recipes)
        a, n = await _owner_round(bid, owner, seen_alerts)
        weekly_answers[i // 7] += a
        weekly_alerts[i // 7] += n
        balances[d] = await _total_balance(bid, d)
    last = first + timedelta(days=DAYS - 1)

    async with read_session() as s:
        sales = list((await s.execute(select(Sale).where(Sale.business_id == bid, Sale.date >= first - timedelta(days=28),
                                                         Sale.date <= last))).scalars())
        pos = list((await s.execute(select(PurchaseOrder).where(PurchaseOrder.business_id == bid,
                                                                PurchaseOrder.created_at >= first))).scalars())
        plans = list((await s.execute(select(ShortfallPlan).where(ShortfallPlan.business_id == bid))).scalars())
        txns = list((await s.execute(select(BankTransaction).where(BankTransaction.business_id == bid,
                                                                   BankTransaction.date >= first,
                                                                   BankTransaction.date <= last))).scalars())
        receivables = {r.id: r.number for r in (await s.execute(select(ReceivableInvoice).where(
            ReceivableInvoice.business_id == bid))).scalars()}
        suppliers = {sp.id: sp for sp in (await s.execute(select(Supplier).where(Supplier.business_id == bid))).scalars()}

    # Stock: assistants vs baseline
    usage = usage_by_day(sales, items, recipes)
    ingredients = [i for i in items.values() if i.is_ingredient]
    assistant = shelf.result  # scored live during the replay, same rules as the baseline
    lead = {sid: sp.stated_lead_time_days for sid, sp in suppliers.items()}
    base_events = baseline_orders(usage, ingredients, lead, first, DAYS)
    baseline = simulate_stock(opening, usage, base_events, items, first, DAYS)

    # Warnings must come 3+ days ahead in 90% of cases; an unwarned stockout is a miss.
    # Items that keep 3 days or less are re-ordered daily, so they're reported but not scored.
    def keeps(iid: uuid.UUID) -> bool:
        life = items[iid].shelf_life_days if iid in items else None
        return life is None or life > 3

    by_name = {i.name_en: i for i in items.values()}
    warnings = []
    for po in pos:
        drafted = po.created_at.date()
        for part in str((po.notes or {}).get("reason") or "").split("; "):
            m = re.match(r"(.+) runs out on (\d{4}-\d{2}-\d{2})$", part.strip())
            if not m or m.group(1) not in by_name:
                continue
            item = by_name[m.group(1)]
            warnings.append({"po": po.number, "item": item.name_en, "item_id": str(item.id), "drafted": drafted.isoformat(),
                             "stockout": m.group(2), "scored": keeps(item.id),
                             "shelf_life_days": item.shelf_life_days,
                             "lead_days": (date.fromisoformat(m.group(2)) - drafted).days})
    warned = defaultdict(list)
    for w in warnings:
        warned[w["item_id"]].append(date.fromisoformat(w["drafted"]))
    unwarned = [(items[iid].name_en, d.isoformat()) for iid, d in assistant.stockout_episodes
                if keeps(iid) and not any(d - timedelta(days=30) <= wd <= d - timedelta(days=3) for wd in warned[str(iid)])]
    scored = [w for w in warnings if w["scored"]]
    warning_cases = len(scored) + len(unwarned)
    warned_3_days_ahead = sum(1 for w in scored if w["lead_days"] >= 3)

    # Shortfalls flagged 14+ days ahead in 90% of cases. While cash sits below the buffer every
    # forecast flags it again, so plans within 3 days of each other are one episode.
    first_flag: dict[date, date] = {}
    for p in plans:
        flagged_on = p.created_at.date()
        first_flag[p.gap_date] = min(first_flag.get(p.gap_date, flagged_on), flagged_on)
    episodes: list[dict[str, Any]] = []
    for g in sorted(first_flag):
        if episodes and (g - episodes[-1]["last_gap"]).days <= 3:
            ep = episodes[-1]
            ep["last_gap"] = g
            ep["flagged_on"] = min(ep["flagged_on"], first_flag[g])
        else:
            episodes.append({"gap_date": g, "last_gap": g, "flagged_on": first_flag[g]})
    flagged = [{"gap_date": e["gap_date"].isoformat(), "until": e["last_gap"].isoformat(),
                "flagged_on": e["flagged_on"].isoformat(), "lead_days": (e["gap_date"] - e["flagged_on"]).days}
               for e in episodes]
    dips = [d for d in sorted(balances) if balances[d] < buffer and balances.get(d - timedelta(days=1), buffer) >= buffer]
    missed_dips = [d.isoformat() for d in dips
                   if not any(e["gap_date"] - timedelta(days=3) <= d <= e["last_gap"] + timedelta(days=3)
                              and (e["gap_date"] - e["flagged_on"]).days >= 14 for e in episodes)]
    shortfall_cases = len(flagged) + len(missed_dips)
    flagged_14_days_ahead = sum(1 for f in flagged if f["lead_days"] >= 14)

    # Owner effort
    weeks = [{"week": w + 1, "answers": weekly_answers[w], "alerts": weekly_alerts[w],
              "minutes": round((weekly_answers[w] * SECONDS_PER_ANSWER + weekly_alerts[w] * SECONDS_PER_ALERT) / 60, 1)}
             for w in sorted(weekly_answers)]

    # Bank matching
    fed = [t for t in txns if (t.meta or {}).get("feed")]
    auto = [t for t in fed if t.match_status == "auto_matched"]
    wrong = []
    for t in auto:
        kind = t.meta.get("kind")
        expected = EXPECTED_MATCH.get(kind)
        bad = expected is None or t.matched_type != expected
        if not bad and kind == "obligation":
            bad = str(t.matched_id) != t.meta.get("obligation_id")
        if not bad and kind == "customer_payment":
            bad = receivables.get(t.matched_id) != t.meta.get("receivable")  # type: ignore[arg-type]
        if bad:
            wrong.append({"description": t.description, "kind": kind, "matched_type": t.matched_type})
    auto_rate = len(auto) / len(fed) if fed else 0.0
    not_auto: dict[str, int] = defaultdict(int)
    for t in fed:
        if t.match_status != "auto_matched":
            not_auto[f"{t.meta.get('kind')}:{t.match_status}"] += 1

    report = {
        "replay": {"from": first.isoformat(), "to": last.isoformat(), "days": DAYS},
        "stock": {
            "assistants": {"stockout_days": assistant.stockout_days, "stockout_episodes": len(assistant.stockout_episodes),
                           "episodes_by_item": _by_item(assistant.stockout_episodes, items),
                           "days_by_item": _names(assistant.stockout_days_by_item, items),
                           "waste": {"amount_minor": assistant.waste_minor, "currency": business.currency}},
            "baseline": {"stockout_days": baseline.stockout_days, "stockout_episodes": len(baseline.stockout_episodes),
                         "episodes_by_item": _by_item(baseline.stockout_episodes, items),
                         "days_by_item": _names(baseline.stockout_days_by_item, items),
                         "waste": {"amount_minor": baseline.waste_minor, "currency": business.currency},
                         "policy": "every Sunday order the average weekly use of the previous 4 weeks"},
        },
        "stockout_warnings": {"cases": warning_cases, "at_least_3_days": warned_3_days_ahead, "unwarned_stockouts": unwarned,
                                    "all_lines_including_daily_perishables": {
                                        "lines": len(warnings), "at_least_3_days": sum(1 for w in warnings if w["lead_days"] >= 3)},
                                    "warnings": warnings},
        "shortfall_warnings": {"cases": shortfall_cases, "at_least_14_days": flagged_14_days_ahead, "flagged": flagged,
                                     "unflagged_dips": missed_dips},
        "owner_effort": {"weeks": weeks, "max_minutes": max(w["minutes"] for w in weeks),
                               "max_answers": max(w["answers"] for w in weeks),
                               "manual_bookkeeping_hours_per_week": manual_hours,
                               "assumptions": {"seconds_per_answer": SECONDS_PER_ANSWER, "seconds_per_alert": SECONDS_PER_ALERT}},
        "bank_matching": {"bank_lines": len(fed), "auto_matched": len(auto), "rate": round(auto_rate, 3),
                                "incorrect": wrong, "not_auto_matched_by_kind": dict(not_auto)},
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    assert warning_cases > 0 and warned_3_days_ahead / warning_cases >= 0.9, report["stockout_warnings"]
    assert assistant.stockout_days < baseline.stockout_days, report["stock"]
    assert assistant.waste_minor < baseline.waste_minor, report["stock"]
    assert shortfall_cases > 0 and flagged_14_days_ahead / shortfall_cases >= 0.9, report["shortfall_warnings"]
    assert report["owner_effort"]["max_minutes"] <= 15 and report["owner_effort"]["max_answers"] <= 25, weeks
    assert auto_rate >= 0.85 and wrong == [], report["bank_matching"]
