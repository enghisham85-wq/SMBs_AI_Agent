"""Structured output schemas for every LLM role."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class FieldStr(BaseModel):
    value: str | None = None
    raw_text: str | None = None
    confidence: float = Field(ge=0, le=1)


class FieldDate(BaseModel):
    value: date | None = None
    raw_text: str | None = None
    confidence: float = Field(ge=0, le=1)


class FieldDecimal(BaseModel):
    value: Decimal | None = None
    raw_text: str | None = None
    confidence: float = Field(ge=0, le=1)


class InvoiceLine(BaseModel):
    description: FieldStr
    qty: FieldDecimal
    unit: FieldStr
    unit_price: FieldDecimal
    vat_rate_percent: FieldDecimal
    line_total: FieldDecimal


class InvoiceExtraction(BaseModel):
    language: Literal["en", "ar", "bilingual"]
    supplier_name: FieldStr
    supplier_name_alt: FieldStr | None = None
    supplier_vat_number: FieldStr | None = None
    invoice_number: FieldStr
    invoice_date: FieldDate
    due_date: FieldDate | None = None
    currency: FieldStr
    lines: list[InvoiceLine]
    subtotal: FieldDecimal
    vat_amount: FieldDecimal
    total: FieldDecimal
    total_alt: FieldDecimal | None = None
    date_format_observed: Literal["DMY", "MDY", "YMD", "ambiguous"]
    notes: list[str] = []


class VerifierIssue(BaseModel):
    field_or_aspect: str
    problem: str
    severity: Literal["low", "medium", "high"]
    suggested_value: str | None = None


class VerifierVerdict(BaseModel):
    agrees: bool
    issues: list[VerifierIssue] = []
    failure_modes_checked: list[str] = []
    confidence: float = Field(ge=0, le=1)


class ExpenseClassification(BaseModel):
    account_code: str
    confidence: float = Field(ge=0, le=1)
    rule_applied: str | None = None
    rationale: str


class OwnerMessage(BaseModel):
    text_en: str = Field(max_length=280)
    text_ar: str = Field(max_length=280)


class IncidentAnalysis(BaseModel):
    root_cause: str
    category: Literal["data_format", "data_gap", "duplicate", "pricing", "model_error", "external", "other"]


class RuleProposal(BaseModel):
    rule_text_en: str
    rule_text_ar: str
    kind: Literal["precondition", "check", "parsing_hint", "classification", "policy"]
    trigger: dict[str, Any]
    expected_effect: str
