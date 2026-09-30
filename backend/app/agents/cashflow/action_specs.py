"""Cash-Flow Agent actions run through harness_graph (T093)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.cashflow import budget as budget_mod
from app.approvals import service as approvals
from app.core import clock
from app.core.events import publish
from app.core.i18n import normalize_arabic_name, option
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.action_spec import ActionContext, ActionSpec, Check, OwnerAsk, VerifyOutcome, register
from app.harness.verifier import build_packet
from app.llm.messages import compose
from app.models.books import ReceivableInvoice
from app.models.cash import CashForecast, CashForecastRun, PaymentPromise, PaymentReminder, PurchasingBudget
from app.models.finance_master import BankAccount, BankTransaction, Obligation
from app.models.tenancy import Business


def precise_now() -> datetime:
    """Business-clock time with microseconds, so two runs on the same day keep their order."""
    return clock.clock_now().replace(microsecond=datetime.now().microsecond)


LEVEL_NAMES = {1: ("polite", "ودّي"), 2: ("firm", "حازم"), 3: ("final", "نهائي")}


async def _business(bid: uuid.UUID) -> Business:
    async with read_session() as s:
        b = await s.get(Business, bid)
    assert b is not None
    return b


# =========================================================================== paid check
async def paid_status(s: AsyncSession, inv: ReceivableInvoice) -> dict[str, Any] | None:
    """Is the invoice paid? Checks the Accountant's records, then bank lines not yet reconciled."""
    owed = inv.total.amount_minor - inv.amount_paid_minor
    if inv.status == "paid" or owed <= 0:
        return {"via": "books", "paid_on": inv.paid_on or clock.today()}
    name = normalize_arabic_name(inv.customer_name).split(" ")[0] if inv.customer_name else ""
    number = inv.number.lower()
    rows = (await s.execute(select(BankTransaction).join(BankAccount, BankAccount.id == BankTransaction.account_id).where(
        BankTransaction.business_id == inv.business_id, BankAccount.is_cash_on_hand.is_(False),
        BankTransaction.date >= inv.invoice_date, BankTransaction.match_status.in_(("unmatched", "suggested"))))).scalars().all()
    for t in rows:
        if t.amount.amount_minor != owed:
            continue
        desc = normalize_arabic_name(t.description)
        suggested = (t.meta or {}).get("suggestion", {}).get("ref") == str(inv.id)
        if suggested or number in t.description.lower() or (name and name in desc):
            return {"via": "bank", "paid_on": t.date, "bank_txn_id": str(t.id)}
    return None


async def _reminder(reminder_id: str) -> tuple[PaymentReminder, ReceivableInvoice]:
    async with read_session() as s:
        rem = await s.get(PaymentReminder, uuid.UUID(reminder_id))
        assert rem is not None
        inv = await s.get(ReceivableInvoice, rem.receivable_invoice_id)
        assert inv is not None
    return rem, inv


async def inform_paid(business_id: uuid.UUID, inv: ReceivableInvoice, paid_on: date, reminder_id: uuid.UUID) -> None:
    when_en = "today" if paid_on == clock.today() else f"on {paid_on.strftime('%d %b')}"
    when_ar = "اليوم" if paid_on == clock.today() else f"في {paid_on.strftime('%d/%m')}"
    await approvals.post_alert(
        business_id=business_id, agent="cashflow", dedupe_key=f"reminder_paid:{reminder_id}",
        text_en=f"Customer {inv.customer_name} paid invoice {inv.number} {when_en}, so I cancelled the reminder.",
        text_ar=f"دفع العميل {inv.customer_name} الفاتورة {inv.number} {when_ar}، لذا ألغيت التذكير.",
        context={"receivable_invoice_id": str(inv.id), "reminder_id": str(reminder_id)})


# =========================================================================== send_reminder
async def _rem_plan(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    rem, inv = await _reminder(inputs["reminder_id"])
    return {"intent": f"send a level-{rem.level} payment reminder", "summary": f"{inv.customer_name} {inv.number}",
            "data_refs": {"receivable_invoice_id": str(inv.id), "due_date": inv.due_date}}


async def _rem_checks(ctx: ActionContext, inputs: dict[str, Any]) -> list[Check]:
    rem, inv = await _reminder(inputs["reminder_id"])
    if rem.status not in ("scheduled", "pending_approval"):
        return [Check("reminder_still_scheduled", False, {"status": rem.status}, cancel=True,
                      reason_en=f"the reminder is already {rem.status}")]
    async with read_session() as s:
        paid = await paid_status(s, inv)
    if paid is not None:
        # Caught just before sending: logged as an incident so a rule can be learned about this customer.
        return [Check("invoice_still_unpaid", False, {"invoice": inv.number, "customer": inv.customer_name, **paid},
                      cancel=True, incident=True,
                      reason_en=f"{inv.customer_name} already paid {inv.number}",
                      reason_ar=f"دفع {inv.customer_name} الفاتورة {inv.number} بالفعل")]
    return [Check("invoice_still_unpaid", True, {"invoice": inv.number, "outstanding_minor": inv.total.amount_minor - inv.amount_paid_minor})]


async def _rem_packet(ctx: ActionContext, inputs: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    rem, inv = await _reminder(inputs["reminder_id"])
    owed = inv.total.amount_minor - inv.amount_paid_minor
    async with read_session() as s:
        paid = await paid_status(s, inv)
        earlier = (await s.execute(select(PaymentReminder).where(PaymentReminder.receivable_invoice_id == inv.id,
                                                                 PaymentReminder.status == "sent"))).scalars().all()
    known = []
    if paid is not None:
        known.append({"field": "invoice", "problem": "the invoice is already paid", "severity": "high"})
    if Money(owed, inv.total.currency).to_display() not in rem.text_en:
        known.append({"field": "amount", "problem": "the amount in the message differs from the amount owed", "severity": "high"})
    source = {"today": clock.today(), "invoice": inv.number, "customer": inv.customer_name, "due_date": inv.due_date,
              "total": inv.total, "amount_paid_minor": inv.amount_paid_minor, "status": inv.status,
              "reminders_already_sent": [r.level for r in earlier]}
    proposed = {"level": rem.level, "message_en": rem.text_en, "amount_owed": Money(owed, inv.total.currency)}
    return build_packet("send_reminder", source, proposed, known_issues=known, irreversible=True)


async def _rem_auto(ctx: ActionContext, inputs: dict[str, Any], settings: dict[str, Any]) -> bool:
    """Only first (polite) reminders, and only when the owner turned that on (FR-027)."""
    if settings.get("reminder_auto_approve") != "polite_only":
        return False
    rem, _ = await _reminder(inputs["reminder_id"])
    return rem.level == 1


async def _rem_request(ctx: ActionContext, inputs: dict[str, Any], plan: dict[str, Any]) -> OwnerAsk:
    rem, inv = await _reminder(inputs["reminder_id"])
    async with write_session() as s:
        row = await s.get(PaymentReminder, rem.id)
        if row is not None and row.status == "scheduled":
            row.status = "pending_approval"
    owed = Money(inv.total.amount_minor - inv.amount_paid_minor, inv.total.currency)
    days = (clock.today() - inv.due_date).days
    late_en = f"{days} days overdue" if days > 0 else ("due today" if days == 0 else f"due in {-days} days")
    late_ar = f"متأخرة {days} يوماً" if days > 0 else ("تستحق اليوم" if days == 0 else f"تستحق خلال {-days} أيام")
    lvl_en, lvl_ar = LEVEL_NAMES[rem.level]
    fb_en = f"Send a {lvl_en} reminder to {inv.customer_name} for {inv.number} ({owed.to_display()}, {late_en})?"
    fb_ar = f"هل أرسل تذكيراً {lvl_ar} إلى {inv.customer_name} بخصوص {inv.number} ({owed.to_display('ar')}، {late_ar})؟"
    msg = await compose({"customer": inv.customer_name, "invoice": inv.number, "amount": owed.to_display(),
                         "level": lvl_en, "days_overdue": days}, fb_en, fb_ar)
    return OwnerAsk(kind="approval", text_en=msg.text_en, text_ar=msg.text_ar,
                    options=[option("approve", "opt_approve", "approve"),
                             {"key": "promised", "label_en": "They promised to pay", "label_ar": "وعد بالدفع",
                              "effect": "promised"},
                             {"key": "reject", "label_en": "Don't send", "label_ar": "لا ترسل", "effect": "reject"}],
                    required_role="manager", safe_default="reask",
                    context={"reminder_id": str(rem.id), "receivable_invoice_id": str(inv.id), "level": rem.level,
                             "message_en": rem.text_en, "message_ar": rem.text_ar})


async def _rem_option(ctx: ActionContext, inputs: dict[str, Any], option_key: str, edits: dict[str, Any]) -> dict[str, Any]:
    rem, inv = await _reminder(inputs["reminder_id"])
    if option_key == "promised":
        promised = date.fromisoformat(edits["promised_date"]) if edits.get("promised_date") else clock.today() + timedelta(days=7)
        async with write_session() as s:
            s.add(PaymentPromise(business_id=ctx.business_id, receivable_invoice_id=inv.id, promised_date=promised,
                                 amount=Money(inv.total.amount_minor - inv.amount_paid_minor, inv.total.currency),
                                 recorded_by=None))
            row = await s.get(PaymentReminder, rem.id)
            if row is not None:
                row.status, row.cancel_reason = "cancelled_owner", f"customer promised to pay by {promised.isoformat()}"
    return {"next": "finalize", "outcome": "cancelled"}


async def _rem_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    async with write_session() as s:
        rem = await s.get(PaymentReminder, uuid.UUID(inputs["reminder_id"]))
        assert rem is not None
        inv = await s.get(ReceivableInvoice, rem.receivable_invoice_id)
        assert inv is not None
        paid = await paid_status(s, inv)  # the invoice may have been paid while waiting for approval
        if paid is not None:
            rem.status, rem.cancel_reason = "cancelled_paid", f"paid ({paid['via']}) on {paid['paid_on']}"
            return {"cancelled": "paid", "paid_on": paid["paid_on"]}
        rem.auto_approved = rem.status == "scheduled"  # no approval request was raised
        rem.status = "sent"
        rem.sent_at = clock.clock_now()
        rem.action_id = ctx.action_id
        return {"sent": True, "level": rem.level, "channel": "dashboard", "sent_at": rem.sent_at}


async def _rem_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        rem = await s.get(PaymentReminder, uuid.UUID(inputs["reminder_id"]))
    if rem is None:
        return VerifyOutcome(False, {"problem": "reminder missing"})
    if result.get("cancelled"):
        return VerifyOutcome(rem.status == "cancelled_paid", {"status": rem.status})
    return VerifyOutcome(rem.status == "sent" and rem.sent_at is not None, {"status": rem.status})


async def _rem_finalize(ctx: ActionContext, inputs: dict[str, Any], outcome: str, result: dict[str, Any]) -> None:
    async with write_session() as s:
        rem = await s.get(PaymentReminder, uuid.UUID(inputs["reminder_id"]))
        if rem is None:
            return
        inv = await s.get(ReceivableInvoice, rem.receivable_invoice_id)
        assert inv is not None
        paid = await paid_status(s, inv) if rem.status in ("scheduled", "pending_approval", "cancelled_paid") else None
        if paid is not None and rem.status != "cancelled_paid":
            rem.status, rem.cancel_reason = "cancelled_paid", f"paid ({paid['via']}) on {paid['paid_on']}"
        elif outcome in ("rejected", "cancelled") and rem.status in ("scheduled", "pending_approval"):
            rem.status, rem.cancel_reason = "cancelled_owner", rem.cancel_reason or "the owner chose not to send it"
        elif outcome == "escalated" and rem.status == "pending_approval":
            rem.status = "scheduled"
        status, rid = rem.status, rem.id
    if status == "cancelled_paid" and paid is not None:
        await inform_paid(ctx.business_id, inv, paid["paid_on"], rid)


# =========================================================================== publish_budget
async def _budget_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    business = await _business(ctx.business_id)
    ws = date.fromisoformat(inputs["week_start"])
    async with write_session() as s:
        row = (await s.execute(select(PurchasingBudget).where(PurchasingBudget.business_id == ctx.business_id,
                                                              PurchasingBudget.week_start == ws))).scalar_one_or_none()
        prev = None
        if row is None:
            row = PurchasingBudget(business_id=ctx.business_id, week_start=ws, amount=Money(0, business.currency))
            s.add(row)
        else:
            prev = {"amount_minor": row.amount.amount_minor, "reason": row.reason, "tightened": row.tightened,
                    "forecast_run_id": str(row.forecast_run_id) if row.forecast_run_id else None}
        changed = prev is None or prev["amount_minor"] != int(inputs["amount_minor"]) or prev["tightened"] != bool(inputs["tightened"])
        row.amount = Money(int(inputs["amount_minor"]), business.currency)
        row.reason = inputs["reason_en"][:300]
        row.tightened = bool(inputs["tightened"])
        row.forecast_run_id = uuid.UUID(inputs["forecast_run_id"]) if inputs.get("forecast_run_id") else None
        if changed:
            publish(s, "budget.updated", {"week_start": ws, "amount": row.amount, "tightened": row.tightened,
                                          "reason": inputs["reason_en"], "reason_ar": inputs.get("reason_ar", "")},
                    producer="cashflow", business_id=ctx.business_id, action_id=ctx.action_id)
        await s.flush()
        return {"budget_id": str(row.id), "previous": prev, "published": changed}


async def _budget_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        row = await s.get(PurchasingBudget, uuid.UUID(result["budget_id"]))
    ok = row is not None and row.amount.amount_minor == int(inputs["amount_minor"]) and row.tightened == bool(inputs["tightened"])
    return VerifyOutcome(ok, {"amount_minor": row.amount.amount_minor if row else None})


async def _budget_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    if not result.get("budget_id"):
        return
    async with write_session() as s:
        row = await s.get(PurchasingBudget, uuid.UUID(result["budget_id"]))
        if row is None:
            return
        prev = result.get("previous")
        if prev is None:
            await s.delete(row)
        else:
            row.amount = Money(prev["amount_minor"], row.amount.currency)
            row.reason, row.tightened = prev["reason"], prev["tightened"]


async def budget_remaining(business_id: uuid.UUID, d: date) -> int | None:
    """Stock Agent budget provider (FR-029)."""
    async with read_session() as s:
        return await budget_mod.remaining(s, business_id, d)


# =========================================================================== save_forecast_run
async def _save_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    business = await _business(ctx.business_id)
    cur = business.currency
    primary = inputs["scenarios"][inputs["primary_scenario"]]
    low = min(primary, key=lambda d: (d["closing"], d["date"]))
    async with write_session() as s:
        run = CashForecastRun(
            business_id=ctx.business_id, generated_on=date.fromisoformat(inputs["generated_on"]),
            bank_data_as_of=datetime.fromisoformat(inputs["bank_data_as_of"]) if inputs.get("bank_data_as_of") else None,
            low_confidence_reason=inputs.get("low_confidence_reason"), primary_scenario=inputs["primary_scenario"],
            opening_balance=Money(int(inputs["opening_minor"]), cur), lowest_balance=Money(int(low["closing"]), cur),
            lowest_date=date.fromisoformat(low["date"]), buffer=Money(int(inputs["buffer_minor"]), cur),
            horizon_days=int(inputs.get("horizon", 30)), flows=inputs.get("flows", []), checks=inputs.get("checks", []),
            action_id=ctx.action_id, created_at=precise_now())
        s.add(run)
        await s.flush()
        n = 0
        for sc, days in inputs["scenarios"].items():
            for d in days:
                s.add(CashForecast(business_id=ctx.business_id, run_id=run.id, scenario=sc, date=date.fromisoformat(d["date"]),
                                   opening=Money(int(d["opening"]), cur), inflows=Money(int(d["inflows"]), cur),
                                   outflows=Money(int(d["outflows"]), cur), closing=Money(int(d["closing"]), cur),
                                   confidence=float(d["confidence"]), below_buffer=bool(d["below_buffer"])))
                n += 1
        return {"run_id": str(run.id), "rows": n, "lowest_minor": int(low["closing"]), "lowest_date": low["date"]}


async def _save_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    rid = uuid.UUID(result["run_id"])
    async with read_session() as s:
        run = await s.get(CashForecastRun, rid)
        rows = (await s.execute(select(CashForecast).where(CashForecast.run_id == rid))).scalars().all()
    n = len(rows)
    low = min((r.closing.amount_minor for r in rows if r.scenario == inputs["primary_scenario"]), default=None)
    expected = sum(len(v) for v in inputs["scenarios"].values())
    ok = run is not None and n == expected and low == run.lowest_balance.amount_minor
    return VerifyOutcome(ok, {"rows": n, "expected_rows": expected, "lowest_minor": low})


async def _save_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    if not result.get("run_id"):
        return
    rid = uuid.UUID(result["run_id"])
    async with write_session() as s:
        await s.execute(delete(CashForecast).where(CashForecast.run_id == rid))
        await s.execute(delete(CashForecastRun).where(CashForecastRun.id == rid))


# =========================================================================== save_obligation
async def _ob_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    business = await _business(ctx.business_id)
    async with write_session() as s:
        prev = None
        if inputs.get("obligation_id"):
            ob = await s.get(Obligation, uuid.UUID(inputs["obligation_id"]))
            assert ob is not None and ob.business_id == ctx.business_id
            prev = {"type": ob.type, "description": ob.description, "amount_minor": ob.amount.amount_minor,
                    "next_due_date": ob.next_due_date, "recurrence": ob.recurrence, "is_confirmed": ob.is_confirmed}
        else:
            ob = Obligation(business_id=ctx.business_id, type="other", description="", amount=Money(0, business.currency),
                            next_due_date=clock.today(), recurrence="monthly", is_confirmed=True)
            s.add(ob)
        f = inputs["fields"]
        if "type" in f:
            ob.type = f["type"]
        if "description" in f:
            ob.description = f["description"]
        if "amount_minor" in f:
            ob.amount = Money(int(f["amount_minor"]), business.currency)
        if "next_due_date" in f:
            ob.next_due_date = date.fromisoformat(str(f["next_due_date"]))
        if "recurrence" in f:
            ob.recurrence = f["recurrence"]
        if "is_confirmed" in f:
            ob.is_confirmed = bool(f["is_confirmed"])
        if ob.bank_account_id is None:
            ob.bank_account_id = (await s.execute(select(BankAccount.id).where(
                BankAccount.business_id == ctx.business_id, BankAccount.is_cash_on_hand.is_(False)).limit(1))).scalar_one_or_none()
        await s.flush()
        return {"obligation_id": str(ob.id), "previous": prev}


async def _ob_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        ob = await s.get(Obligation, uuid.UUID(result["obligation_id"]))
    f = inputs["fields"]
    ok = ob is not None and ("amount_minor" not in f or ob.amount.amount_minor == int(f["amount_minor"]))
    return VerifyOutcome(ok, {"saved": ob is not None})


async def _ob_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    if not result.get("obligation_id"):
        return
    async with write_session() as s:
        ob = await s.get(Obligation, uuid.UUID(result["obligation_id"]))
        if ob is None:
            return
        prev = result.get("previous")
        if prev is None:
            await s.delete(ob)
            return
        ob.type, ob.description, ob.recurrence, ob.is_confirmed = prev["type"], prev["description"], prev["recurrence"], prev["is_confirmed"]
        ob.amount = Money(prev["amount_minor"], ob.amount.currency)
        ob.next_due_date = date.fromisoformat(str(prev["next_due_date"]))


# =========================================================================== save_shortfall_plan
async def _plan_execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    from app.models.cash import PlanAction, ShortfallPlan

    cur = (await _business(ctx.business_id)).currency
    sf = inputs["shortfall"]
    async with write_session() as s:
        # Older plans stop being current; their accepted actions keep applying to forecasts.
        superseded = []
        for old in (await s.execute(select(ShortfallPlan).where(ShortfallPlan.business_id == ctx.business_id,
                                                                ShortfallPlan.status.in_(("proposed", "presented"))))).scalars():
            superseded.append({"id": str(old.id), "status": old.status})
            old.status = "superseded"
        plan = ShortfallPlan(business_id=ctx.business_id, forecast_run_id=uuid.UUID(inputs["run_id"]),
                             gap_amount=Money(int(sf["gap_minor"]), cur), gap_date=date.fromisoformat(sf["first_below"]),
                             lowest_balance=Money(int(sf["lowest_minor"]), cur), lowest_date=date.fromisoformat(sf["lowest_date"]),
                             days_to_act=int(sf["days_to_act"]),
                             combined_lowest_balance=Money(int(inputs["combined_lowest_minor"]), cur), status="proposed",
                             created_at=precise_now())
        s.add(plan)
        await s.flush()
        for a in inputs["actions"]:
            s.add(PlanAction(business_id=ctx.business_id, plan_id=plan.id, type=a["type"], target_type=a["target_type"],
                             target_ref=a.get("target_ref"), description_en=a["description_en"][:300],
                             description_ar=a["description_ar"][:300], impact=Money(int(a["impact_minor"]), cur),
                             risk=a["risk"], rank=int(a["rank"]),
                             simulated_lowest_balance=Money(int(a["simulated_lowest_minor"]), cur),
                             simulated_lowest_date=date.fromisoformat(a["simulated_lowest_date"]),
                             adjustments=a["adjustments"], simulated_series=a["series"]))
        return {"plan_id": str(plan.id), "actions": len(inputs["actions"]), "superseded": superseded}


async def _plan_verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    from app.models.cash import PlanAction

    async with read_session() as s:
        rows = (await s.execute(select(PlanAction).where(PlanAction.plan_id == uuid.UUID(result["plan_id"])))).scalars().all()
    ranks = sorted(r.rank for r in rows)
    fin_last = not rows or max(rows, key=lambda r: r.rank).type == "financing"
    ok = len(rows) == len(inputs["actions"]) and ranks == list(range(1, len(rows) + 1)) and fin_last
    return VerifyOutcome(ok, {"actions": len(rows), "financing_last": fin_last})


async def _plan_compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    from app.models.cash import PlanAction, ShortfallPlan

    if not result.get("plan_id"):
        return
    pid = uuid.UUID(result["plan_id"])
    async with write_session() as s:
        await s.execute(delete(PlanAction).where(PlanAction.plan_id == pid))
        await s.execute(delete(ShortfallPlan).where(ShortfallPlan.id == pid))
        for old in result.get("superseded", []):
            row = await s.get(ShortfallPlan, uuid.UUID(old["id"]))
            if row is not None:
                row.status = old["status"]


def register_specs() -> None:
    register(ActionSpec(name="save_shortfall_plan", agent="cashflow", risk_class="reversible", execute=_plan_execute,
                        title_en="Save shortfall plan", title_ar="حفظ خطة العجز النقدي",
                        verify=_plan_verify, compensate=_plan_compensate))
    register(ActionSpec(name="send_reminder", agent="cashflow", risk_class="irreversible_external", execute=_rem_execute,
                        title_en="Send payment reminder", title_ar="إرسال تذكير بالدفع", plan=_rem_plan,
                        preconditions=_rem_checks, verify=_rem_verify, verifier_packet=_rem_packet,
                        auto_approve=_rem_auto, approval_request=_rem_request, on_option=_rem_option,
                        on_finalize=_rem_finalize))
    register(ActionSpec(name="publish_budget", agent="cashflow", risk_class="reversible", execute=_budget_execute,
                        title_en="Publish purchasing budget", title_ar="نشر ميزانية المشتريات",
                        verify=_budget_verify, compensate=_budget_compensate))
    register(ActionSpec(name="save_forecast_run", agent="cashflow", risk_class="reversible", execute=_save_execute,
                        title_en="Save cash forecast", title_ar="حفظ التوقع النقدي",
                        verify=_save_verify, compensate=_save_compensate))
    register(ActionSpec(name="save_obligation", agent="cashflow", risk_class="reversible", execute=_ob_execute,
                        title_en="Save recurring payment", title_ar="حفظ دفعة متكررة",
                        verify=_ob_verify, compensate=_ob_compensate))
