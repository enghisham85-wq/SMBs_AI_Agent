"""Accountant Agent workflows as LangGraph graphs (T079).

document_graph: extract -> validate -> [re-extract once] -> ask the owner one question at a time
(interrupt) -> validate again -> post_invoice through the harness. The invoice stays `held` until every
issue is answered.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import delete, select

from app.agents.accountant import checks, classification, extraction, matching, supplier_match
from app.approvals import service as approvals
from app.core import clock, settings_store
from app.core.events import publish
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness import rules
from app.harness.action_spec import OwnerAsk
from app.harness.confidence import route as confidence_route
from app.harness.graph import run_action
from app.harness.incidents import open_incident
from app.models.books import Document, Extraction, JournalEntry, JournalLine, PayableInvoice
from app.models.finance_master import Account, BankAccount, BankBalanceSnapshot, BankTransaction
from app.models.harness import CheckResult, Incident
from app.models.master import Supplier
from app.models.tenancy import Business, FileRef

GRAPH = "document"
ALT_ACCOUNTS = ("5120", "5600", "5900", "5500", "5400")


class DocState(TypedDict, total=False):
    business_id: str
    document_id: str
    invoice_id: str | None
    attempt: int
    norm: dict[str, Any]
    overrides: dict[str, Any]
    issues: list[dict[str, Any]]
    asked: int
    route: str
    outcome: str | None
    held_published: bool
    warnings: list[str]


def _norm(d: dict[str, Any]) -> extraction.Normalised:
    data = dict(d)
    for k in ("invoice_date", "due_date"):
        data[k] = date.fromisoformat(data[k]) if data.get(k) else None
    return extraction.Normalised(**data)


def _money(minor: int | None, cur: str, lang: str = "en") -> str:
    return Money(minor or 0, cur).to_display(lang)


async def _doc(state: DocState) -> tuple[Document, Business]:
    async with read_session() as s:
        doc = await s.get(Document, uuid.UUID(state["document_id"]))
        b = await s.get(Business, uuid.UUID(state["business_id"]))
    assert doc is not None and b is not None
    return doc, b


# ------------------------------------------------------------------ extract
async def extract_node(state: DocState) -> dict[str, Any]:
    doc, business = await _doc(state)
    attempt = state.get("attempt", 1)
    async with read_session() as s:
        ref = await s.get(FileRef, doc.file_id)
        sups = (await s.execute(select(Supplier).where(Supplier.business_id == business.id))).scalars().all()
    assert ref is not None
    data = await asyncio.to_thread(Path(ref.path).read_bytes)
    hints = [f"{sp.name_en} / {sp.name_ar} (tax no. {sp.vat_number})" for sp in sups]
    hints += [r.rule_text_en for r in await rules.active_rules(business.id, "accountant", "parsing_hint")]
    ext, source = await extraction.extract(data, doc.mime, doc.sha256, hints, attempt)
    n = extraction.normalise(ext, business.currency)
    async with write_session() as s:
        s.add(Extraction(business_id=business.id, document_id=doc.id, attempt=attempt, fields=n.to_json(),
                         document_confidence=n.document_confidence, source=source))
        d = await s.get(Document, doc.id)
        assert d is not None
        d.status, d.language_detected, d.document_confidence = "extracted", n.language, n.document_confidence
    return {"norm": n.to_json(), "attempt": attempt}


async def reextract_node(state: DocState) -> dict[str, Any]:
    return {"attempt": 2}


# ------------------------------------------------------------------ validate
def _issue(code: str, en: str, ar: str, options: list[dict[str, str]], **data: Any) -> dict[str, Any]:
    return {"code": code, "text_en": en, "text_ar": ar, "options": options, "data": data}


async def validate_node(state: DocState) -> dict[str, Any]:
    doc, business = await _doc(state)
    cur = business.currency
    ov = dict(state.get("overrides") or {})
    n = _norm(state["norm"])
    today = clock.today()
    issues: list[dict[str, Any]] = []
    warnings: list[str] = list(state.get("warnings") or [])
    found: list[Any] = []
    detected_tw: dict[str, Any] | None = None

    # Apply the owner's earlier answers.
    for k in ("total_minor", "subtotal_minor", "vat_minor"):
        if k in ov:
            setattr(n, k, ov[k])
    if ov.get("invoice_date"):
        n.invoice_date = date.fromisoformat(ov["invoice_date"])
        n.date_format_observed = "DMY"

    if n.total_minor is None or not n.lines or n.document_confidence < 0.2:
        issues.append(_issue("unreadable", f"I could not read {doc.original_name} reliably.",
                             f"لم أتمكن من قراءة {doc.original_name} بشكل موثوق.",
                             [{"key": "retake", "label_en": "I'll retake the photo", "label_ar": "سأعيد التصوير", "effect": "retake"},
                              {"key": "discard", "label_en": "Discard", "label_ar": "تجاهل", "effect": "discard"}]))
        return await _save(state, business, n, None, issues, warnings, found, None, [])

    # Supplier
    async with read_session() as s:
        if ov.get("supplier_id"):
            supplier = await s.get(Supplier, uuid.UUID(ov["supplier_id"]))
            sm = None
        else:
            sm = await supplier_match.match(s, business.id, n.supplier_names, n.vat_number)
            supplier = await s.get(Supplier, sm.supplier_id) if sm.supplier_id else None
    if supplier is None:
        opts = [{"key": f"supplier:{sid}", "label_en": name, "label_ar": name, "effect": "pick"} for sid, name, _ in (sm.candidates if sm else [])[:2]]
        opts.append({"key": "new_supplier", "label_en": "A new supplier", "label_ar": "مورد جديد", "effect": "new"})
        name = n.supplier_names[0] if n.supplier_names else "?"
        issues.append(_issue("unknown_supplier", f"Who is the supplier “{name}”?", f"من هو المورد «{name}»؟", opts, name=name))
    else:
        extraction.penalise(n, "supplier", 1.0)

    # Arithmetic (re-read once before asking)
    if not ov.get("arithmetic_ok"):
        arith = checks.extraction_arithmetic(n, business.vat_rate_percent)
        found.append(arith)
        if not arith.passed:
            extraction.penalise(n, "total", 0.5)
            if state.get("attempt", 1) == 1:
                return {"route": "reextract", "norm": n.to_json()}
            calc = arith.details["calc_total_minor"]
            printed = arith.details["printed_total_minor"] or 0
            issues.append(_issue(
                "arithmetic",
                f"This receipt's total ({_money(printed, cur)}) does not equal its lines plus VAT ({_money(calc, cur)}).",
                f"إجمالي هذا الإيصال ({_money(printed, cur, 'ar')}) لا يساوي البنود مع الضريبة ({_money(calc, cur, 'ar')}).",
                [{"key": "use_printed", "label_en": f"Use {Money(printed, cur).to_decimal():,.2f}", "label_ar": f"استخدم {Money(printed, cur).to_decimal():,.2f}", "effect": "use_printed"},
                 {"key": "use_calculated", "label_en": f"Use {Money(calc, cur).to_decimal():,.2f}", "label_ar": f"استخدم {Money(calc, cur).to_decimal():,.2f}", "effect": "use_calculated"},
                 {"key": "retake", "label_en": "Retake photo", "label_ar": "إعادة التصوير", "effect": "retake"}],
                printed=printed, calc=calc, lines_sum=arith.details["lines_sum_minor"], vat_calc=arith.details["vat_calc_minor"]))

    # Bilingual disagreement
    if n.conflicts and "total_choice" not in ov:
        c = n.conflicts[0]
        issues.append(_issue(
            "bilingual_conflict",
            f"The Arabic total ({_money(c['alt'], cur)}) and the English total ({_money(c['primary'], cur)}) differ.",
            f"الإجمالي العربي ({_money(c['alt'], cur, 'ar')}) يختلف عن الإجمالي الإنجليزي ({_money(c['primary'], cur, 'ar')}).",
            [{"key": "use_primary", "label_en": f"Use {Money(c['primary'], cur).to_decimal():,.2f}", "label_ar": f"استخدم {Money(c['primary'], cur).to_decimal():,.2f}", "effect": "use_primary"},
             {"key": "use_alt", "label_en": f"Use {Money(c['alt'], cur).to_decimal():,.2f}", "label_ar": f"استخدم {Money(c['alt'], cur).to_decimal():,.2f}", "effect": "use_alt"}],
            primary=c["primary"], alt=c["alt"]))

    # Dates
    hint = supplier.date_format_hint if supplier else None
    dcheck, corrected, fmt_used = checks.date_sanity(n, today, hint, business.default_date_format)
    found.append(dcheck)
    if corrected is not None:
        n.invoice_date = corrected
        if dcheck.details.get("corrected"):
            warnings.append(dcheck.reason_en)
            inc_id = await open_incident(business_id=business.id, agent="accountant", type="date_format", detected_by="date_sanity",
                                summary=f"{supplier.name_en if supplier else 'Supplier'}: {dcheck.reason_en}",
                                refs={"supplier_id": str(supplier.id) if supplier else None, "format": fmt_used,
                                      "document_id": str(doc.id)},
                                dedupe_key=f"date_format:{supplier.id if supplier else doc.id}",
                                action_taken=f"read the date as {corrected.isoformat()}")
            from app.harness.analysis import analyse_and_propose

            await analyse_and_propose(inc_id)  # e.g. "Supplier X invoices use DD/MM format"
    elif not dcheck.passed:
        read_as = dcheck.details.get("read_as")
        opts = []
        if isinstance(read_as, date):
            for cand in {read_as, checks._swap(read_as) or read_as}:
                opts.append({"key": f"date:{cand.isoformat()}", "label_en": cand.strftime("%d %b %Y"),
                             "label_ar": cand.isoformat(), "effect": "date"})
        opts.append({"key": "retake", "label_en": "Retake photo", "label_ar": "إعادة التصوير", "effect": "retake"})
        issues.append(_issue("date", f"Please confirm the invoice date: {dcheck.reason_en}.",
                             f"يرجى تأكيد تاريخ الفاتورة: {dcheck.reason_ar}.", opts[:4]))

    # Duplicates
    if not ov.get("dup_ok"):
        async with read_session() as s:
            dup = await checks.duplicate_invoice(s, business.id, supplier.id if supplier else None, n.invoice_number,
                                                 n.total_minor, n.invoice_date, doc.sha256,
                                                 exclude_id=uuid.UUID(state["invoice_id"]) if state.get("invoice_id") else None)
        found.append(dup)
        if not dup.passed:
            sup_en = supplier.name_en if supplier else ""
            issues.append(_issue(
                "duplicate",
                f"I received two invoices from {sup_en} with number {n.invoice_number} (or the same amount and date). "
                f"Is this one a duplicate?",
                f"وصلتني فاتورتان من {supplier.name_ar if supplier else ''} برقم {n.invoice_number} (أو بنفس المبلغ والتاريخ). هل هذه مكررة؟",
                [{"key": "discard", "label_en": "Yes, discard", "label_ar": "نعم، تجاهلها", "effect": "discard_duplicate"},
                 {"key": "both_valid", "label_en": "No, both valid", "label_ar": "لا، كلتاهما صحيحة", "effect": "both_valid"}],
                existing=str(dup.details.get("existing_invoice_id"))))
            inc_id = await open_incident(
                business_id=business.id, agent="accountant", type="duplicate_invoice", detected_by="duplicate_invoice",
                summary=f"{sup_en or 'A supplier'} sent invoice {n.invoice_number} again ({doc.original_name})",
                refs={"supplier_id": str(supplier.id) if supplier else None, "supplier_en": sup_en,
                      "supplier_ar": supplier.name_ar if supplier else "", "invoice_number": n.invoice_number,
                      "document_id": str(doc.id), "existing_invoice_id": str(dup.details.get("existing_invoice_id"))},
                dedupe_key=f"duplicate_invoice:{doc.id}",
                action_taken="blocked the second posting and asked the owner; the payment is counted once")
            from app.harness.analysis import analyse_and_propose

            await analyse_and_propose(inc_id, resolve=False)

    # Stock lines and three-way match
    async with read_session() as s:
        item_ids = await checks.match_items(s, business.id, n.lines)
        po_id = None
        if supplier is not None and any(item_ids) and not ov.get("three_way"):
            tw, po_id = await checks.three_way_match(s, business.id, supplier.id, n.lines, item_ids)
            found.append(tw)
            if tw.name == "missing_po_or_delivery":
                warnings.append(tw.reason_en)
            elif not tw.passed:
                diff_en = "; ".join(
                    f"{d['invoiced']} invoiced vs {d['delivered']} delivered" if d["kind"] == "quantity" else "price differs from the order"
                    for d in tw.details["differences"])
                issues.append(_issue(
                    "three_way",
                    f"This invoice does not match order {tw.details['po_number']}: {diff_en}.",
                    f"هذه الفاتورة لا تطابق أمر الشراء {tw.details['po_number']}.",
                    [{"key": "post_delivered", "label_en": "Post what was delivered", "label_ar": "رحّل ما تم استلامه", "effect": "post_delivered"},
                     {"key": "post_invoiced", "label_en": "Post as invoiced", "label_ar": "رحّل كما في الفاتورة", "effect": "post_invoiced"},
                     {"key": "hold", "label_en": "Keep on hold", "label_ar": "أبقِها معلقة", "effect": "hold"}],
                    po_id=str(po_id), differences=[{**d, "item_id": str(d["item_id"])} for d in tw.details["differences"]]))
                detected_tw = {"po_id": str(po_id), "po_number": tw.details["po_number"],
                               "differences": [{**d, "item_id": str(d["item_id"])} for d in tw.details["differences"]]}
        elif ov.get("three_way"):
            po_id = uuid.UUID(ov["po_id"]) if ov.get("po_id") else None
    if ov.get("three_way") == "post_delivered":
        for d in ov.get("three_way_differences", []):
            for i, iid in enumerate(item_ids):
                if iid and str(iid) == d["item_id"] and d["kind"] == "quantity":
                    ln = n.lines[i]
                    ln["qty"] = str(d["delivered"])
                    ln["line_total_minor"] = int((Decimal(str(d["delivered"])) * ln["unit_price_minor"]).to_integral_value())
        n.subtotal_minor = sum(ln["line_total_minor"] for ln in n.lines)
        vat = 0
        for ln in n.lines:
            rate = Decimal(ln["vat_rate_percent"]) if ln["vat_rate_percent"] is not None else business.vat_rate_percent
            vat += int((Decimal(ln["line_total_minor"]) * rate / 100).to_integral_value())
        n.vat_minor, n.total_minor = vat, n.subtotal_minor + vat

    # VAT number validity (warning; flagged before the VAT period closes)
    # The number printed on the invoice is what the tax authority requires, not the one on file.
    vat_check = checks.supplier_vat_validity(n.vat_number, n.vat_minor, business.tax_id_pattern)
    found.append(vat_check)
    if not vat_check.passed:
        warnings.append(vat_check.reason_en)

    # Classification of non-stock lines
    accounts: list[dict[str, Any]] = []
    for i, ln in enumerate(n.lines):
        if item_ids[i] is not None:
            accounts.append({"item_id": str(item_ids[i]), "account_code": "1200", "account_source": "stock", "account_confidence": 1.0})
            continue
        if str(i) in (ov.get("accounts") or {}):
            code = ov["accounts"][str(i)]
            accounts.append({"account_code": code, "account_source": "owner", "account_confidence": 1.0})
            continue
        cls = await classification.classify(business.id, supplier.id if supplier else None, ln["description"])
        accounts.append({"account_code": cls.account_code, "account_source": cls.source, "account_confidence": cls.confidence})
        if await confidence_route(
                business.id, "accountant", cls.confidence,
                f"Recorded “{ln['description']}” as account {cls.account_code} ({cls.confidence:.0%} sure)",
                f"سُجّل «{ln['description']}» على الحساب {cls.account_code} (بثقة {cls.confidence:.0%})",
                {"document_id": str(doc.id), "line": i}) == "ask":
            async with read_session() as s:
                accts = {a.code: a for a in (await s.execute(select(Account).where(Account.business_id == business.id))).scalars()}
            choices = [cls.account_code] + [c for c in ALT_ACCOUNTS if c != cls.account_code][:2]
            issues.append(_issue(
                f"classify:{i}", f"How should I record “{ln['description']}” from {supplier.name_en if supplier else 'this supplier'}?",
                f"كيف أسجل «{ln['description']}» من {supplier.name_ar if supplier else 'هذا المورد'}؟",
                [{"key": f"account:{c}", "label_en": f"{accts[c].name_en} ({c})", "label_ar": f"{accts[c].name_ar} ({c})",
                  "effect": "account"} for c in choices if c in accts],
                line=i, suggested=cls.account_code))
    saved = await _save(state, business, n, supplier, issues, warnings, found, po_id, accounts)
    if detected_tw is not None and saved.get("invoice_id"):
        await _three_way_incident({**state, "invoice_id": saved["invoice_id"]}, detected_tw, None)
    return saved


async def _save(state: DocState, business: Business, n: extraction.Normalised, supplier: Supplier | None,
                issues: list[dict[str, Any]], warnings: list[str], found: list[Any], po_id: uuid.UUID | None,
                accounts: list[dict[str, Any]]) -> dict[str, Any]:
    cur = business.currency
    lines = [{**ln, **(accounts[i] if i < len(accounts) else {})} for i, ln in enumerate(n.lines)]
    subtotal = n.subtotal_minor if n.subtotal_minor is not None else sum(ln["line_total_minor"] for ln in lines)
    vat = n.vat_minor or 0
    total = n.total_minor if n.total_minor is not None else subtotal + vat
    held_published = bool(state.get("held_published"))
    async with write_session() as s:
        inv = await s.get(PayableInvoice, uuid.UUID(state["invoice_id"])) if state.get("invoice_id") else None
        if inv is None:
            inv = PayableInvoice(business_id=business.id, invoice_number=n.invoice_number or "?",
                                 normalised_number=checks.normalise_number(n.invoice_number),
                                 invoice_date=n.invoice_date or clock.today(), subtotal=Money(0, cur),
                                 vat_amount=Money(0, cur), total=Money(0, cur), document_id=uuid.UUID(state["document_id"]))
            s.add(inv)
        inv.supplier_id = supplier.id if supplier else None
        inv.invoice_number = n.invoice_number or "?"
        inv.normalised_number = checks.normalise_number(n.invoice_number)
        inv.invoice_date = n.invoice_date or clock.today()
        inv.due_date = n.due_date or ((n.invoice_date or clock.today()) + timedelta(days=supplier.payment_terms_days if supplier else 30))
        inv.lines = lines
        inv.subtotal, inv.vat_amount, inv.total = Money(subtotal, cur), Money(vat, cur), Money(total, cur)
        inv.supplier_vat_number = n.vat_number
        inv.purchase_order_id = po_id
        inv.status = "held" if issues else "draft"
        inv.hold_reason = [i["code"] for i in issues] or None
        inv.match_result = {"checks": [{"name": c.name, "passed": c.passed} for c in found], "warnings": warnings}
        await s.flush()
        # The checks belong to the latest reading; a re-validation (after an owner answer) replaces them.
        latest = (await s.execute(select(Extraction.id).where(Extraction.document_id == uuid.UUID(state["document_id"]))
                                  .order_by(Extraction.attempt.desc()).limit(1))).scalar_one_or_none()
        if latest is not None:
            await s.execute(delete(CheckResult).where(CheckResult.extraction_id == latest))
        for c in found:
            s.add(CheckResult(business_id=business.id, check_name=c.name, passed=c.passed,
                              details={k: str(v) for k, v in c.details.items()},
                              extraction_id=latest))
        doc = await s.get(Document, uuid.UUID(state["document_id"]))
        if doc is not None:
            doc.status = "needs_review" if issues else "extracted"
            doc.payable_invoice_id = inv.id
            doc.document_confidence = n.document_confidence
        if issues and not held_published:
            publish(s, "invoice.held", {"invoice_id": inv.id, "reason": [i["code"] for i in issues],
                                        "po_id": po_id, "supplier_id": inv.supplier_id, "total": inv.total},
                    producer="accountant", business_id=business.id)
            held_published = True
        inv_id = str(inv.id)
    return {"norm": n.to_json(), "issues": issues, "invoice_id": inv_id, "route": "ask" if issues else "post",
            "warnings": warnings, "held_published": held_published}


# ------------------------------------------------------------------ ask
async def ask_node(state: DocState) -> dict[str, Any]:
    issue = state["issues"][0]
    asked = state.get("asked", 0)
    bid = uuid.UUID(state["business_id"])
    ans = await approvals.ask_owner(
        business_id=bid, graph_name=GRAPH, thread_id=f"document:{state['document_id']}", gate_key=f"{issue['code']}:{asked}",
        ask=OwnerAsk(kind="question", text_en=issue["text_en"], text_ar=issue["text_ar"], options=issue["options"],
                     required_role="manager", safe_default=None, deadline_hours=24 * 7,
                     context={"document_id": state["document_id"], "invoice_id": state.get("invoice_id"), "issue": issue["code"]}),
        agent="accountant")
    ov = dict(state.get("overrides") or {})
    effect, key = ans.get("effect"), ans.get("option_key") or ""
    code, data = issue["code"], issue.get("data", {})
    if effect in ("retake", "discard", "discard_duplicate"):
        outcome = "duplicate" if effect == "discard_duplicate" else "rejected"
        await _close(state, outcome)
        return {"route": "end", "outcome": outcome, "asked": asked + 1}
    if effect == "hold":
        return {"route": "end", "outcome": "held", "asked": asked + 1}
    if code == "arithmetic":
        ov["arithmetic_ok"] = True
        if effect == "use_printed":
            ov["total_minor"] = data["printed"]
            ov["subtotal_minor"] = data["lines_sum"]
            ov["vat_minor"] = data["printed"] - data["lines_sum"]
        else:
            ov["total_minor"] = data["calc"]
            ov["subtotal_minor"] = data["lines_sum"]
            ov["vat_minor"] = data["vat_calc"]
    elif code == "bilingual_conflict":
        ov["total_choice"] = effect
        ov["total_minor"] = data["primary"] if effect == "use_primary" else data["alt"]
    elif code == "date":
        ov["invoice_date"] = key.split(":", 1)[1]
    elif code == "duplicate":
        ov["dup_ok"] = True
    elif code == "unknown_supplier":
        async with write_session() as s:
            if key.startswith("supplier:"):
                sid = uuid.UUID(key.split(":", 1)[1])
            else:
                sup = Supplier(business_id=bid, name_en=data["name"], name_ar=data["name"], stated_lead_time_days=1,
                               payment_terms_days=30)
                s.add(sup)
                await s.flush()
                sid = sup.id
            supplier_match.add_alias(s, bid, sid, data["name"])
        ov["supplier_id"] = str(sid)
    elif code == "three_way":
        ov["three_way"] = effect
        ov["po_id"] = data.get("po_id")
        ov["three_way_differences"] = data.get("differences", [])
        await _three_way_incident(state, data, effect)
    elif code.startswith("classify:"):
        chosen = key.split(":", 1)[1]
        ov.setdefault("accounts", {})[str(data["line"])] = chosen
        if chosen != data.get("suggested"):
            async with read_session() as s:
                inv = await s.get(PayableInvoice, uuid.UUID(state["invoice_id"])) if state.get("invoice_id") else None
            await classification.record_correction(bid, inv.supplier_id if inv else None, data.get("suggested"), chosen,
                                                   uuid.UUID(ans["user_id"]) if ans.get("user_id") else None,
                                                   f"document:{state['document_id']}:line:{data['line']}")
    elif code == "unreadable":
        await _close(state, "rejected")
        return {"route": "end", "outcome": "rejected", "asked": asked + 1}
    return {"overrides": ov, "route": "validate", "asked": asked + 1}


async def _three_way_incident(state: DocState, data: dict[str, Any], decision: str | None) -> None:
    """An invoice did not match its delivery: log it when held (decision None) and propose a rule; the
    owner's decision later completes the same incident."""
    from app.harness.analysis import analyse_and_propose

    bid = uuid.UUID(state["business_id"])
    async with read_session() as s:
        inv = await s.get(PayableInvoice, uuid.UUID(state["invoice_id"])) if state.get("invoice_id") else None
        sup = await s.get(Supplier, inv.supplier_id) if inv and inv.supplier_id else None
    if inv is None or sup is None:
        return
    diffs = data.get("differences", [])
    qty = [f"{d.get('invoiced')} invoiced vs {d.get('delivered')} delivered" for d in diffs if d.get("kind") == "quantity"]
    summary = f"{sup.name_en} invoice {inv.invoice_number} did not match what was delivered ({'; '.join(qty) or 'price'})"
    inc = await open_incident(business_id=bid, agent="accountant", type="three_way_mismatch", detected_by="three_way_match",
                              summary=summary, refs={"supplier_id": str(sup.id), "supplier_en": sup.name_en,
                                                     "supplier_ar": sup.name_ar, "invoice_id": str(inv.id),
                                                     "po_id": data.get("po_id"), "differences": diffs, "decision": decision},
                              dedupe_key=f"three_way:{inv.id}",
                              action_taken="held the invoice and asked the owner" if decision is None
                              else f"held the invoice; owner chose '{decision}'")
    if decision is not None:
        async with write_session() as s:
            row = await s.get(Incident, inc)
            if row is not None:
                row.action_taken = f"held the invoice; owner chose '{decision}'"
                row.refs = {**row.refs, "decision": decision}
    await analyse_and_propose(inc, resolve=decision is not None)


async def _delivery_confirmed(invoice_id: str) -> tuple[str | None, bool]:
    """(supplier id, whether the invoice's order has a recorded delivery)."""
    from app.models.purchasing import Delivery

    async with read_session() as s:
        inv = await s.get(PayableInvoice, uuid.UUID(invoice_id))
        if inv is None:
            return None, False
        delivered = False
        if inv.purchase_order_id:
            delivered = (await s.execute(select(Delivery.id).where(Delivery.po_id == inv.purchase_order_id).limit(1))).first() is not None
    return (str(inv.supplier_id) if inv.supplier_id else None), delivered


async def _close(state: DocState, outcome: str) -> None:
    async with write_session() as s:
        doc = await s.get(Document, uuid.UUID(state["document_id"]))
        if doc is not None:
            doc.status = "duplicate" if outcome == "duplicate" else "rejected"
        if state.get("invoice_id"):
            inv = await s.get(PayableInvoice, uuid.UUID(state["invoice_id"]))
            if inv is not None and inv.status in ("draft", "held"):
                inv.status = "void"


# ------------------------------------------------------------------ post
async def post_node(state: DocState) -> dict[str, Any]:
    bid = uuid.UUID(state["business_id"])
    supplier_id, delivered = await _delivery_confirmed(state["invoice_id"])
    # supplier_id and delivery_confirmed let learned rules such as "wait for delivery confirmation" apply.
    out = await run_action("post_invoice", {"invoice_id": state["invoice_id"], "supplier_id": supplier_id,
                                            "delivery_confirmed": delivered,
                                            "dup_ok": bool((state.get("overrides") or {}).get("dup_ok"))}, bid)
    async with write_session() as s:
        doc = await s.get(Document, uuid.UUID(state["document_id"]))
        if doc is not None:
            doc.status = "posted" if out["outcome"] == "completed" else "needs_review"
    if state.get("warnings"):
        inv_id = state["invoice_id"]
        await approvals.post_alert(business_id=bid, agent="accountant", text_en="Note on a posted invoice: " + "; ".join(state["warnings"]) + ".",
                                   text_ar="ملاحظة على فاتورة مُرحّلة: " + "؛ ".join(state["warnings"]) + ".",
                                   dedupe_key=f"invoice_warning:{inv_id}",
                                   context={"invoice_id": inv_id, "document_id": state["document_id"]})
    return {"outcome": "posted" if out["outcome"] == "completed" else out["outcome"] or "awaiting_review"}


def _route(state: DocState) -> str:
    return state["route"]


def build_document() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DocState)
    g.add_node("extract", extract_node)
    g.add_node("validate", validate_node)
    g.add_node("reextract", reextract_node)
    g.add_node("ask", ask_node)
    g.add_node("post", post_node)
    g.add_edge(START, "extract")
    g.add_edge("extract", "validate")
    g.add_conditional_edges("validate", _route, {"reextract": "reextract", "ask": "ask", "post": "post"})
    g.add_edge("reextract", "extract")
    g.add_conditional_edges("ask", _route, {"validate": "validate", "end": END})
    g.add_edge("post", END)
    return g


async def process_document(business_id: uuid.UUID, document_id: uuid.UUID) -> dict[str, Any]:
    from app.graphs import runtime

    thread = f"document:{document_id}"
    async with write_session() as s:
        doc = await s.get(Document, document_id)
        assert doc is not None
        doc.graph_thread_id = thread
        doc.status = "extracting"
    out = await runtime.start(GRAPH, {"business_id": str(business_id), "document_id": str(document_id), "attempt": 1,
                                      "overrides": {}, "asked": 0, "warnings": []}, thread)
    v = out["values"]
    return {"document_id": str(document_id), "invoice_id": v.get("invoice_id"), "waiting_for_owner": out["interrupted"],
            "outcome": v.get("outcome"), "issues": [i["code"] for i in v.get("issues") or []]}


# ============================================================ reconciliation
class DayState(TypedDict, total=False):
    business_id: str
    date: str
    notes: dict[str, Any]


async def books_start(business_id: uuid.UUID) -> date | None:
    v = (await settings_store.get_all(business_id)).get("books_start_date")
    return date.fromisoformat(v) if v else None


async def rc_sales(state: DayState) -> dict[str, Any]:
    bid, d = uuid.UUID(state["business_id"]), date.fromisoformat(state["date"])
    start = await books_start(bid)
    if start is None or d < start:
        return {}
    await run_action("post_sales_summary", {"date": d.isoformat()}, bid)
    return {}


async def reconcile(business_id: uuid.UUID, up_to: date) -> dict[str, int]:
    start = await books_start(business_id)
    if start is None:
        return {"auto": 0, "suggested": 0, "unmatched": 0}
    high = float(await settings_store.get(business_id, "confidence_high"))
    low = float(await settings_store.get(business_id, "confidence_low"))
    stats = {"auto": 0, "suggested": 0, "unmatched": 0}
    async with read_session() as s:
        txns = list((await s.execute(select(BankTransaction).where(
            BankTransaction.business_id == business_id, BankTransaction.date >= start, BankTransaction.date <= up_to,
            BankTransaction.match_status.in_(("unmatched", "suggested"))).order_by(BankTransaction.date))).scalars())
    for txn in txns:
        async with read_session() as s:
            fresh = await s.get(BankTransaction, txn.id)
            if fresh is None or fresh.match_status not in ("unmatched", "suggested"):
                continue  # matched as the other half of a transfer
            acct = await s.get(BankAccount, txn.account_id)
            assert acct is not None
            cands = await matching.candidates(s, fresh, acct)
        top = matching.best(fresh, cands)
        if top is not None and top.score >= high:
            c = top.candidate
            out = await run_action("apply_bank_match", {"txn_id": str(txn.id), "source": "auto", "confidence": top.score,
                                                        "candidate": {"type": c.type, "ref": c.ref, "name": c.name, "extra": c.extra}},
                                   business_id)
            stats["auto" if out["outcome"] == "completed" else "unmatched"] += 1
        elif top is not None and top.score >= low:
            async with write_session() as s:
                row = await s.get(BankTransaction, txn.id)
                if row is not None:
                    row.match_status, row.matched_type, row.match_confidence = "suggested", top.candidate.type, top.score
                    row.meta = {**(row.meta or {}), "suggestion": {"type": top.candidate.type, "ref": top.candidate.ref,
                                                                   "name": top.candidate.name, "extra": top.candidate.extra}}
            stats["suggested"] += 1
        else:
            stats["unmatched"] += 1
    return stats


async def rc_match(state: DayState) -> dict[str, Any]:
    bid, d = uuid.UUID(state["business_id"]), date.fromisoformat(state["date"])
    return {"notes": {"reconciliation": await reconcile(bid, d)}}


def build_reconciliation() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DayState)
    g.add_node("post_sales", rc_sales)
    g.add_node("match", rc_match)
    g.add_edge(START, "post_sales")
    g.add_edge("post_sales", "match")
    g.add_edge("match", END)
    return g


# ============================================================ trial balance
async def tb_check(state: DayState) -> dict[str, Any]:
    bid = uuid.UUID(state["business_id"])
    async with read_session() as s:
        entries = (await s.execute(select(JournalEntry).where(JournalEntry.business_id == bid,
                                                              JournalEntry.status == "posted"))).scalars().all()
        lines = (await s.execute(select(JournalLine).where(JournalLine.business_id == bid))).scalars().all()
    by_entry: dict[uuid.UUID, list[JournalLine]] = {}
    for ln in lines:
        by_entry.setdefault(ln.entry_id, []).append(ln)
    bad = [e for e in entries if sum(x.debit_minor for x in by_entry.get(e.id, [])) != sum(x.credit_minor for x in by_entry.get(e.id, []))]
    for e in bad:
        await run_action("quarantine_entry", {"entry_id": str(e.id), "reason": "entry does not balance"}, bid)
        await open_incident(business_id=bid, agent="accountant", type="trial_balance", detected_by="trial_balance",
                            summary=f"Journal entry {e.reference_type} {e.reference_id} does not balance; it was quarantined",
                            refs={"entry_id": str(e.id)}, dedupe_key=f"trial_balance:{e.id}",
                            action_taken="quarantined the entry")
    return {"notes": {"unbalanced": [str(e.id) for e in bad]}}


async def ledger_bank_balances(business_id: uuid.UUID) -> dict[str, dict[str, int]]:
    """Ledger vs bank-statement balance per bank account (FR: bank balance agreement)."""
    async with read_session() as s:
        accts = {a.code: a for a in (await s.execute(select(Account).where(Account.business_id == business_id))).scalars()}
        out: dict[str, dict[str, int]] = {}
        for ba in (await s.execute(select(BankAccount).where(BankAccount.business_id == business_id))).scalars():
            acct = accts.get(ba.ledger_account_code)
            if acct is None:
                continue
            rows = (await s.execute(select(JournalLine).join(JournalEntry, JournalEntry.id == JournalLine.entry_id).where(
                JournalLine.account_id == acct.id, JournalEntry.status.in_(("posted", "reversed"))))).scalars().all()
            ledger = sum(r.debit_minor - r.credit_minor for r in rows)
            snap = (await s.execute(select(BankBalanceSnapshot).where(BankBalanceSnapshot.account_id == ba.id)
                                    .order_by(BankBalanceSnapshot.as_of.desc()).limit(1))).scalar_one_or_none()
            out[ba.name] = {"ledger_minor": ledger, "bank_minor": snap.balance.amount_minor if snap else 0}
    return out


async def tb_valuation(state: DayState) -> dict[str, Any]:
    """Stock valuation agreement: a mismatch is published so the Stock Agent opens a joint incident."""
    bid = uuid.UUID(state["business_id"])
    start = await books_start(bid)
    if start is None:
        return {}
    tolerance = float(await settings_store.get(bid, "stock_variance_pct"))
    async with write_session() as s:
        c = await checks.stock_valuation(s, bid, tolerance)
        s.add(CheckResult(business_id=bid, check_name=c.name, passed=c.passed, details=c.details))
        if not c.passed and c.details["currency"]:
            cur = c.details["currency"]
            publish(s, "stock_valuation.mismatch", {
                "ledger_value": Money(c.details["ledger_minor"], cur), "stock_value": Money(c.details["stock_value_minor"], cur),
                "difference": Money(c.details["difference_minor"], cur),
                "received_not_invoiced": Money(c.details["received_not_invoiced_minor"], cur)},
                producer="accountant", business_id=bid)
    return {"notes": {**state.get("notes", {}), "stock_valuation": c.passed}}


def build_trial_balance() -> StateGraph[Any]:
    g: StateGraph[Any] = StateGraph(DayState)
    g.add_node("check", tb_check)
    g.add_node("valuation", tb_valuation)
    g.add_edge(START, "check")
    g.add_edge("check", "valuation")
    g.add_edge("valuation", END)
    return g


async def _run(graph: str, bid: uuid.UUID, d: date) -> Any:
    from app.graphs import runtime

    return await runtime.start(graph, {"business_id": str(bid), "date": d.isoformat(), "notes": {}},
                               f"{graph}:{bid}:{d.isoformat()}:{uuid.uuid4().hex[:6]}")


async def step_reconcile(bid: uuid.UUID, d: date) -> Any:
    return await _run("reconciliation", bid, d)


async def step_trial_balance(bid: uuid.UUID, d: date) -> Any:
    return await _run("trial_balance", bid, d)


def register_graphs() -> None:
    from app.graphs import runtime
    from app.graphs.daily_run import register_step

    runtime.register(GRAPH, build_document)
    runtime.register("reconciliation", build_reconciliation)
    runtime.register("trial_balance", build_trial_balance)
    register_step("reconcile", "reconciliation_graph", step_reconcile)
    register_step("trial_balance", "trial_balance_graph", step_trial_balance)
    from app.agents.accountant.vat import step_period_reminder

    register_step("trial_balance", "vat_period_reminder", step_period_reminder)
