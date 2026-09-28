"""Documents, invoices, journal and classification corrections (data-model.md §4). Written by the Accountant Agent."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, BigInteger, Date, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import Base, Money, TenantMixin, money_col

DOC_STATUSES = ("received", "extracting", "extracted", "needs_review", "posted", "rejected", "duplicate")


class Document(TenantMixin, Base):
    __tablename__ = "document"

    file_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("file_ref.id"))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime: Mapped[str] = mapped_column(String(100))
    original_name: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(Enum("dashboard", "telegram", native_enum=False))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    language_detected: Mapped[str | None] = mapped_column(Enum("en", "ar", "bilingual", native_enum=False), nullable=True)
    status: Mapped[str] = mapped_column(Enum(*DOC_STATUSES, native_enum=False), default="received", index=True)
    document_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    graph_thread_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payable_invoice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class Extraction(TenantMixin, Base):
    __tablename__ = "extraction"

    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("document.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)  # 1 or 2
    # {field: {value, raw_text, confidence, source}} plus lines
    fields: Mapped[dict[str, Any]] = mapped_column(JSON)
    document_confidence: Mapped[float] = mapped_column(Float)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    verifier_verdict: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="llm")  # llm | offline | owner


class PayableInvoice(TenantMixin, Base):
    __tablename__ = "payable_invoice"

    supplier_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("supplier.id"), nullable=True, index=True)
    invoice_number: Mapped[str] = mapped_column(String(60))
    # Normalised number for the duplicate guard (supplier_id, normalised_number).
    normalised_number: Mapped[str] = mapped_column(String(60), index=True)
    invoice_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # [{description, qty, unit, unit_price_minor, vat_rate_percent, line_total_minor, item_id?, account_code?,
    #   account_source?, account_confidence?}]
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    subtotal: Mapped[Money] = money_col("subtotal")
    vat_amount: Mapped[Money] = money_col("vat_amount")
    total: Mapped[Money] = money_col("total")
    supplier_vat_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("document.id"), nullable=True)
    purchase_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("purchase_order.id"), nullable=True)
    status: Mapped[str] = mapped_column(Enum("draft", "held", "posted", "paid", "void", native_enum=False),
                                        default="draft", index=True)
    hold_reason: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    match_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    amount_paid_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class ReceivableInvoice(TenantMixin, Base):
    __tablename__ = "receivable_invoice"
    __table_args__ = (UniqueConstraint("business_id", "number"),)

    customer_name: Mapped[str] = mapped_column(String(200))
    customer_contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_telegram: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Unique per business; auto INV-### when not given.
    number: Mapped[str] = mapped_column(String(40))
    invoice_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    # [{description, qty, unit_price_minor, vat_rate_percent, line_total_minor}]
    lines: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    subtotal: Mapped[Money] = money_col("subtotal")
    vat_amount: Mapped[Money] = money_col("vat_amount")
    total: Mapped[Money] = money_col("total")
    amount_paid_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(Enum("open", "partially_paid", "paid", "void", native_enum=False),
                                        default="open", index=True)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")  # seed | manual
    late_payment_history_score: Mapped[float] = mapped_column(Float, default=0.0)
    journal_entry_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class JournalEntry(TenantMixin, Base):
    """Never edited in place: a reversal creates an opposite entry."""

    __tablename__ = "journal_entry"

    date: Mapped[date] = mapped_column(Date, index=True)
    reference_type: Mapped[str] = mapped_column(String(40), index=True)
    reference_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    memo: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(60), default="accountant")
    status: Mapped[str] = mapped_column(Enum("posted", "quarantined", "reversed", native_enum=False), default="posted",
                                        index=True)
    reverses_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    currency: Mapped[str] = mapped_column(String(3))


class JournalLine(TenantMixin, Base):
    __tablename__ = "journal_line"

    entry_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("journal_entry.id"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("account.id"), index=True)
    debit_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    memo: Mapped[str] = mapped_column(String(200), default="")


class ClassificationCorrection(TenantMixin, Base):
    __tablename__ = "classification_correction"

    supplier_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("supplier.id"), nullable=True, index=True)
    from_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    to_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("account.id"))
    corrected_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    date: Mapped[date] = mapped_column(Date)
    source_ref: Mapped[str] = mapped_column(String(120), default="")


Q2 = Decimal("0.01")
