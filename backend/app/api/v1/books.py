"""Books endpoints (T080) and customer invoices (T082)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agents.accountant import graphs as acc_graphs
from app.agents.accountant import receivables, reports
from app.agents.accountant.intake import UnsupportedFileError, submit
from app.api.common import J, get_business, with_freshness
from app.approvals import service as approvals
from app.config import get_settings
from app.core import clock
from app.core.auth import CurrentUser, RequireManager
from app.core.errors import AppError, not_found
from app.db.currencies import exponent
from app.db.engine import read_session
from app.harness.graph import run_action
from app.models.books import Document, Extraction, PayableInvoice, ReceivableInvoice
from app.models.finance_master import BankAccount, BankTransaction
from app.models.harness import ApprovalRequest
from app.models.master import Supplier
from app.models.tenancy import FileRef

router = APIRouter(tags=["books"])


def _money(minor: int, cur: str) -> dict[str, Any]:
    from app.db.types import Money

    return Money(minor, cur).to_json()


# ------------------------------------------------------------------ documents
@router.post("/documents", status_code=202)
async def upload_document(file: UploadFile = File(...), user: CurrentUser = RequireManager) -> Response:
    try:
        res = await submit(user.business_id, await file.read(), file.content_type or "application/octet-stream",
                           file.filename or "document", "dashboard", user.id)
    except UnsupportedFileError as exc:
        raise AppError(415, "unsupported_file", message_en="Please upload a PDF or an image.",
                       message_ar="يرجى رفع ملف PDF أو صورة.") from exc
    return J(res, 202)


@router.get("/documents/samples")
async def sample_documents(user: CurrentUser = RequireManager) -> Response:
    files = Path(get_settings().SAMPLE_INVOICES_DIR) / "files.json"
    return J({"samples": json.loads(files.read_text(encoding="utf-8")) if files.exists() else []})


@router.post("/documents/samples/{name}", status_code=202)
async def submit_sample(name: str, user: CurrentUser = RequireManager) -> Response:
    root = Path(get_settings().SAMPLE_INVOICES_DIR)
    listing = json.loads((root / "files.json").read_text(encoding="utf-8")) if (root / "files.json").exists() else []
    entry = next((f for f in listing if f["name"] == name), None)
    if entry is None:
        raise not_found("Sample")
    res = await submit(user.business_id, (root / entry["file"]).read_bytes(), entry["mime"], entry["file"], "dashboard", user.id)
    return J(res, 202)


@router.get("/documents")
async def list_documents(status: str | None = None, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        q = select(Document).where(Document.business_id == user.business_id)
        if status:
            q = q.where(Document.status == status)
        docs = (await s.execute(q.order_by(Document.created_at.desc()).limit(200))).scalars().all()
        invs = {i.id: i for i in (await s.execute(select(PayableInvoice).where(
            PayableInvoice.id.in_([d.payable_invoice_id for d in docs if d.payable_invoice_id])))).scalars()} if docs else {}
        sups = {sp.id: sp for sp in (await s.execute(select(Supplier).where(Supplier.business_id == user.business_id))).scalars()}
    out = []
    for d in docs:
        inv = invs.get(d.payable_invoice_id) if d.payable_invoice_id else None
        sp = sups.get(inv.supplier_id) if inv and inv.supplier_id else None
        out.append({"id": d.id, "name": d.original_name, "channel": d.channel, "status": d.status, "language": d.language_detected,
                    "confidence": d.document_confidence, "received_at": d.created_at,
                    "invoice": {"id": inv.id, "number": inv.invoice_number, "status": inv.status, "total": inv.total,
                                "supplier_en": sp.name_en if sp else None, "supplier_ar": sp.name_ar if sp else None,
                                "hold_reason": inv.hold_reason} if inv else None})
    return J(with_freshness({"documents": out}, {"books": max((d.updated_at for d in docs), default=None)}))


@router.get("/documents/{doc_id}")
async def document_detail(doc_id: uuid.UUID, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        d = await s.get(Document, doc_id)
        if d is None or d.business_id != user.business_id:
            raise not_found("Document")
        exts = (await s.execute(select(Extraction).where(Extraction.document_id == doc_id).order_by(Extraction.attempt))).scalars().all()
        inv = await s.get(PayableInvoice, d.payable_invoice_id) if d.payable_invoice_id else None
        sp = await s.get(Supplier, inv.supplier_id) if inv and inv.supplier_id else None
        pending = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.graph_thread_id == d.graph_thread_id,
                                                                 ApprovalRequest.status == "pending"))).scalars().all() if d.graph_thread_id else []
    return J(with_freshness({
        "document": {"id": d.id, "name": d.original_name, "mime": d.mime, "status": d.status, "channel": d.channel,
                     "language": d.language_detected, "confidence": d.document_confidence, "file_url": f"/api/v1/files/{d.file_id}"},
        "extractions": [{"attempt": e.attempt, "source": e.source, "document_confidence": e.document_confidence,
                         "fields": e.fields} for e in exts],
        "invoice": {"id": inv.id, "number": inv.invoice_number, "status": inv.status, "invoice_date": inv.invoice_date,
                    "due_date": inv.due_date, "supplier": {"id": sp.id, "name_en": sp.name_en, "name_ar": sp.name_ar} if sp else None,
                    "lines": inv.lines, "subtotal": inv.subtotal, "vat_amount": inv.vat_amount, "total": inv.total,
                    "hold_reason": inv.hold_reason, "match_result": inv.match_result,
                    "supplier_vat_number": inv.supplier_vat_number} if inv else None,
        "questions": [approvals.to_dict(r) for r in pending],
    }, {"books": inv.updated_at if inv else d.updated_at}))


@router.get("/files/{file_id}")
async def get_file(file_id: uuid.UUID, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        ref = await s.get(FileRef, file_id)
    if ref is None or ref.business_id != user.business_id or not await asyncio.to_thread(Path(ref.path).exists):
        raise not_found("File")
    return FileResponse(ref.path, media_type=ref.mime, filename=ref.original_name)


# ------------------------------------------------------------------ review and reconciliation
@router.get("/review-queue")
async def review_queue(user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        qs = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.business_id == user.business_id,
                                                            ApprovalRequest.agent == "accountant",
                                                            ApprovalRequest.status == "pending"))).scalars().all()
        held = (await s.execute(select(PayableInvoice).where(PayableInvoice.business_id == user.business_id,
                                                             PayableInvoice.status == "held"))).scalars().all()
        suggested = (await s.execute(select(BankTransaction).where(BankTransaction.business_id == user.business_id,
                                                                   BankTransaction.match_status == "suggested"))).scalars().all()
    return J(with_freshness({"questions": [approvals.to_dict(q) for q in qs],
              "held_invoices": [{"id": i.id, "number": i.invoice_number, "total": i.total, "hold_reason": i.hold_reason}
                                for i in held],
              "suggested_matches": [{"id": t.id, "date": t.date, "amount": t.amount, "description": t.description,
                                     "confidence": t.match_confidence, "suggestion": (t.meta or {}).get("suggestion")}
                                    for t in suggested]},
                            {"books": max([*(q.updated_at for q in qs), *(i.updated_at for i in held),
                                           *(t.updated_at for t in suggested)], default=None)}))


@router.get("/reconciliation")
async def reconciliation(user: CurrentUser = RequireManager) -> Response:
    start = await acc_graphs.books_start(user.business_id)
    async with read_session() as s:
        q = select(BankTransaction).where(BankTransaction.business_id == user.business_id)
        if start:
            q = q.where(BankTransaction.date >= start)
        txns = (await s.execute(q.order_by(BankTransaction.date.desc()))).scalars().all()
        accounts = {a.id: a for a in (await s.execute(select(BankAccount).where(BankAccount.business_id == user.business_id))).scalars()}
        last_bank = max((t.created_at for t in txns), default=None)
    matched = [t for t in txns if t.match_status in ("auto_matched", "confirmed")]
    pct = round(100 * len(matched) / len(txns), 1) if txns else 100.0
    open_items = [{"id": t.id, "date": t.date, "account": accounts[t.account_id].name if t.account_id in accounts else "",
                   "amount": t.amount, "description": t.description, "status": t.match_status,
                   "confidence": t.match_confidence, "suggestion": (t.meta or {}).get("suggestion")}
                  for t in txns if t.match_status in ("unmatched", "suggested")]
    recent = [{"id": t.id, "date": t.date, "amount": t.amount, "description": t.description, "matched_type": t.matched_type,
               "source": t.match_source, "confidence": t.match_confidence} for t in matched[:30]]
    cur = (await get_business(user.business_id)).currency
    bank_vs_ledger = {name: {"ledger": _money(v["ledger_minor"], cur), "bank": _money(v["bank_minor"], cur)}
                      for name, v in (await acc_graphs.ledger_bank_balances(user.business_id)).items()}
    return J(with_freshness({"percent_matched": pct, "total": len(txns), "matched": len(matched), "open": open_items,
                             "recent_matches": recent, "bank_vs_ledger": bank_vs_ledger}, {"bank": last_bank}))


class MatchIn(BaseModel):
    use_suggestion: bool = False
    type: str | None = None  # supplier_invoice | customer_payment | obligation | expense | settlement | card_fee
    ref: str | None = None
    account_code: str | None = None


@router.post("/reconciliation/{txn_id}/match")
async def confirm_match(txn_id: uuid.UUID, body: MatchIn, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        txn = await s.get(BankTransaction, txn_id)
    if txn is None or txn.business_id != user.business_id:
        raise not_found("Bank transaction")
    if body.use_suggestion:
        sug = (txn.meta or {}).get("suggestion")
        if not sug:
            raise AppError(422, "no_suggestion", message_en="There is no suggestion to confirm.", message_ar="لا يوجد اقتراح لتأكيده.")
        cand, source = sug, "suggested_confirmed"
    elif body.type == "expense" and body.account_code:
        cand, source = {"type": "expense", "ref": "", "name": txn.description, "extra": {"account_code": body.account_code}}, "manual"
    elif body.type and body.ref:
        cand, source = {"type": body.type, "ref": body.ref, "name": txn.description, "extra": {}}, "manual"
    else:
        raise AppError(422, "invalid_match", message_en="Choose a suggestion, a record or an account.", message_ar="اختر اقتراحاً أو سجلاً أو حساباً.")
    out = await run_action("apply_bank_match", {"txn_id": str(txn_id), "source": source,
                                                "confidence": txn.match_confidence if body.use_suggestion else 1.0,
                                                "candidate": cand}, user.business_id)
    if out["outcome"] != "completed":
        raise AppError(422, "match_failed", message_en="The match could not be applied.", message_ar="تعذر تطبيق المطابقة.")
    return J({"status": "matched", "source": source})


# ------------------------------------------------------------------ reports
@router.get("/reports/pnl")
async def pnl(from_: date | None = Query(default=None, alias="from"), to: date | None = None,
              user: CurrentUser = RequireManager) -> Response:
    b = await get_business(user.business_id)
    end = to or clock.today()
    start = from_ or end.replace(day=1)
    data = await reports.pnl(user.business_id, start, end)
    data["currency"], data["decimals"] = b.currency, exponent(b.currency)
    return J(with_freshness(data, {"books": clock.today()}))


@router.get("/vat/summary")
async def vat_summary(period: str | None = None, user: CurrentUser = RequireManager) -> Response:
    """Input VAT, output VAT, net payable and the supporting invoices for a period (FR-053).

    `period` is `2026-10` (monthly) or `2026-Q4` (quarterly); default: the current period. The figures
    are reviewed by the independent second check (FR-003) before `status` becomes `ready`.
    """
    from app.agents.accountant import vat

    b = await get_business(user.business_id)
    period = period or vat.period_for(clock.today(), b.vat_period)
    try:
        data = await vat.summary_with_status(user.business_id, period)
    except vat.PeriodError as exc:
        raise AppError(422, "invalid_period", message_en=str(exc), message_ar="صيغة الفترة غير صحيحة، مثل 2026-10 أو 2026-Q4.") from exc
    data["currency"], data["decimals"] = b.currency, exponent(b.currency)
    return J(with_freshness(data, {"books": clock.today()}))


@router.get("/reports/balance-sheet")
async def balance_sheet(as_of: date | None = None, user: CurrentUser = RequireManager) -> Response:
    b = await get_business(user.business_id)
    data = await reports.balance_sheet(user.business_id, as_of or clock.today())
    data["currency"], data["decimals"] = b.currency, exponent(b.currency)
    return J(with_freshness(data, {"books": clock.today()}))


# ------------------------------------------------------------------ customer invoices
class RecLine(BaseModel):
    description: str = ""
    qty: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    vat_rate_percent: float | None = Field(default=None, ge=0, le=100)


class RecIn(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    customer_contact: str | None = None
    invoice_date: date
    due_date: date
    number: str | None = None
    lines: list[RecLine] = Field(min_length=1)


def _rec_out(r: ReceivableInvoice, today: date) -> dict[str, Any]:
    return {"id": r.id, "number": r.number, "customer_name": r.customer_name, "customer_contact": r.customer_contact,
            "invoice_date": r.invoice_date, "due_date": r.due_date, "status": r.status, "lines": r.lines,
            "subtotal": r.subtotal, "vat_amount": r.vat_amount, "total": r.total,
            "amount_paid": _money(r.amount_paid_minor, r.total.currency), "outstanding": _money(receivables.outstanding(r), r.total.currency),
            "paid_on": r.paid_on, "ageing": receivables.ageing_bucket(r, today),
            "days_overdue": max(0, (today - r.due_date).days) if r.status in ("open", "partially_paid") else 0, "source": r.source}


@router.post("/receivables", status_code=201)
async def create_receivable(body: RecIn, user: CurrentUser = RequireManager) -> Response:
    b = await get_business(user.business_id)
    try:  # validate first so the owner gets a precise error rather than a failed action
        async with read_session() as s:
            await receivables.build(s, b, body.model_dump(mode="json"))
    except receivables.ReceivableError as exc:
        raise AppError(409 if exc.code == "duplicate_number" else 422, exc.code, message_en=exc.message_en,
                       message_ar=exc.message_ar) from exc
    out = await run_action("create_receivable", body.model_dump(mode="json"), user.business_id)
    if out["outcome"] != "completed":
        raise AppError(422, "not_created", message_en="The invoice could not be created.", message_ar="تعذر إنشاء الفاتورة.")
    async with read_session() as s:
        rec = await s.get(ReceivableInvoice, uuid.UUID(out["result"]["invoice_id"]))
    assert rec is not None
    return J(_rec_out(rec, clock.today()), 201)


@router.get("/receivables")
async def list_receivables(status: str | None = None, overdue: bool | None = None, user: CurrentUser = RequireManager) -> Response:
    today = clock.today()
    async with read_session() as s:
        q = select(ReceivableInvoice).where(ReceivableInvoice.business_id == user.business_id)
        if status:
            q = q.where(ReceivableInvoice.status == status)
        rows = (await s.execute(q.order_by(ReceivableInvoice.due_date))).scalars().all()
    out = [_rec_out(r, today) for r in rows]
    if overdue:
        out = [r for r in out if r["days_overdue"] > 0]
    return J(with_freshness({"receivables": out}, {"books": max((r.updated_at for r in rows), default=None)}))


@router.get("/receivables/{rec_id}")
async def get_receivable(rec_id: uuid.UUID, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        r = await s.get(ReceivableInvoice, rec_id)
        if r is None or r.business_id != user.business_id:
            raise not_found("Invoice")
        payments = (await s.execute(select(BankTransaction).where(BankTransaction.matched_id == rec_id))).scalars().all()
        reminders: list[Any] = []
        try:
            from app.models.cash import PaymentPromise, PaymentReminder  # US3

            reminders = [{"level": x.level, "status": x.status, "scheduled_for": x.scheduled_for}
                         for x in (await s.execute(select(PaymentReminder).where(PaymentReminder.receivable_invoice_id == rec_id))).scalars()]
            promises = [{"promised_date": p.promised_date, "amount": p.amount}
                        for p in (await s.execute(select(PaymentPromise).where(PaymentPromise.receivable_invoice_id == rec_id))).scalars()]
        except ImportError:
            promises = []
    out = _rec_out(r, clock.today())
    out.update({"payments": [{"date": p.date, "amount": p.amount, "description": p.description} for p in payments],
                "reminders": reminders, "promises": promises})
    return J(with_freshness(out, {"books": r.updated_at, "bank": max((p.created_at for p in payments), default=None)}))


class VoidIn(BaseModel):
    reason: str = Field(min_length=2)


@router.post("/receivables/{rec_id}/void")
async def void_receivable(rec_id: uuid.UUID, body: VoidIn, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        r = await s.get(ReceivableInvoice, rec_id)
    if r is None or r.business_id != user.business_id:
        raise not_found("Invoice")
    if r.amount_paid_minor > 0 or r.status != "open":
        raise AppError(409, "cannot_void", message_en="Only an unpaid invoice can be voided.", message_ar="يمكن إلغاء الفاتورة غير المدفوعة فقط.")
    out = await run_action("void_receivable", {"invoice_id": str(rec_id), "reason": body.reason}, user.business_id)
    if out["outcome"] != "completed":
        raise AppError(422, "not_voided", message_en="The invoice could not be voided.", message_ar="تعذر إلغاء الفاتورة.")
    return J({"status": "void"})
