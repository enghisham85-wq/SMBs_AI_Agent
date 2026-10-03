"""Invoice extraction.

`extract()` reads the original file with Claude (or the offline stand-in); `normalise()` turns the
structured output into canonical values in integer minor units, compares printed vs canonical
digits, checks bilingual agreement, and computes per-field and document confidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.core.i18n import normalize_digits
from app.db.types import Money
from app.llm.client import (
    LLMRefusalError,
    LLMUnavailableError,
    MissingFixtureError,
    document_block,
    get_llm,
    text_block,
)
from app.llm.schemas import InvoiceExtraction

_PROMPT = (Path(__file__).resolve().parents[2] / "llm" / "prompts" / "invoice_extraction.md").read_text(encoding="utf-8")
REQUIRED = ("supplier", "invoice_number", "invoice_date", "total")


def sample_dirs() -> list[Path]:
    return [Path(get_settings().SAMPLE_INVOICES_DIR)]


def _empty() -> InvoiceExtraction:
    blank = {"value": None, "raw_text": None, "confidence": 0.0}
    return InvoiceExtraction.model_validate({
        "language": "en", "supplier_name": blank, "invoice_number": blank, "invoice_date": blank, "currency": blank,
        "lines": [], "subtotal": blank, "vat_amount": blank, "total": blank, "date_format_observed": "ambiguous",
        "notes": ["could not read this document offline"]})


def offline_extraction(sha256: str) -> InvoiceExtraction:
    """Offline stand-in: ground truth written by the sample-invoice generator, else an empty result."""
    from app.seed.invoices.generate import lookup_truth

    t = lookup_truth(sample_dirs(), sha256)
    if t is None:
        return _empty()
    return InvoiceExtraction.model_validate({k: v for k, v in t.items() if not k.startswith("_")})


async def extract(data: bytes, mime: str, sha256: str, hints: list[str], attempt: int) -> tuple[InvoiceExtraction, str]:
    """Returns (extraction, source) where source is llm | offline | refusal | unavailable."""
    hint_text = "\n".join(f"- {h}" for h in hints) or "- none"
    # The breakpoint sits on the document, so a re-read (attempt 2) reuses it; the hints and attempt vary.
    content = [document_block(data, mime, cache=True),
               text_block(f"Known suppliers and parsing hints:\n{hint_text}\n\nAttempt {attempt}. Extract the invoice.")]
    llm = get_llm()
    try:
        ext = await llm.parse("extraction", _PROMPT, content, InvoiceExtraction, offline=lambda: offline_extraction(sha256))
        return ext, "offline" if llm.mode == "offline" else "llm"
    except LLMRefusalError:
        return _empty(), "refusal"
    except (LLMUnavailableError, MissingFixtureError):
        return _empty(), "unavailable"


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(normalize_digits(str(v)).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _raw_matches(value: Any, raw: str | None) -> bool:
    """Printed text (after digit normalisation) agrees with the canonical value."""
    if value is None or raw is None:
        return True
    a, b = _dec(value), _dec(normalize_digits(raw).replace("٬", ""))
    if a is not None and b is not None:
        return a == b
    return True  # non-numeric fields (names, dates) are compared by later checks


@dataclass
class Normalised:
    language: str
    supplier_names: list[str]
    vat_number: str | None
    invoice_number: str | None
    invoice_date: date | None
    invoice_date_raw: str | None
    due_date: date | None
    currency: str | None
    lines: list[dict[str, Any]]
    subtotal_minor: int | None
    vat_minor: int | None
    total_minor: int | None
    total_alt_minor: int | None
    date_format_observed: str
    confidence: dict[str, float]
    notes: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        for k in ("invoice_date", "due_date"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @property
    def document_confidence(self) -> float:
        return min((self.confidence.get(k, 0.0) for k in REQUIRED), default=0.0)


def _minor(v: Any, currency: str) -> int | None:
    d = _dec(v)
    return Money.from_decimal(d, currency).amount_minor if d is not None else None


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def normalise(ext: InvoiceExtraction, business_currency: str) -> Normalised:
    cur = business_currency
    conf: dict[str, float] = {}

    def c(name: str, fld: Any) -> float:
        if fld is None:
            conf[name] = 0.0
            return 0.0
        penalty = 1.0 if _raw_matches(fld.value, fld.raw_text) else 0.5
        conf[name] = round(float(fld.confidence) * penalty, 3)
        return conf[name]

    names = [n for n in [ext.supplier_name.value, ext.supplier_name_alt.value if ext.supplier_name_alt else None] if n]
    c("supplier", ext.supplier_name)
    c("invoice_number", ext.invoice_number)
    c("invoice_date", ext.invoice_date)
    c("total", ext.total)
    c("subtotal", ext.subtotal)
    c("vat_amount", ext.vat_amount)
    lines = []
    for i, ln in enumerate(ext.lines):
        rate = _dec(ln.vat_rate_percent.value) if ln.vat_rate_percent else None
        lines.append({
            "description": ln.description.value or "",
            "qty": str(_dec(ln.qty.value) or Decimal(0)),
            "unit": ln.unit.value or "",
            "unit_price_minor": _minor(ln.unit_price.value, cur) or 0,
            "vat_rate_percent": str(rate) if rate is not None else None,
            "line_total_minor": _minor(ln.line_total.value, cur) or 0,
            "confidence": round(min(float(ln.qty.confidence), float(ln.unit_price.confidence), float(ln.line_total.confidence)), 3),
        })
        conf[f"line_{i}"] = lines[-1]["confidence"]
    n = Normalised(
        language=ext.language,
        supplier_names=names,
        vat_number=normalize_digits(ext.supplier_vat_number.value).replace(" ", "")
        if ext.supplier_vat_number and ext.supplier_vat_number.value else None,
        invoice_number=normalize_digits(ext.invoice_number.value).strip() if ext.invoice_number.value else None,
        invoice_date=_date(ext.invoice_date.value),
        invoice_date_raw=ext.invoice_date.raw_text,
        due_date=_date(ext.due_date.value) if ext.due_date else None,
        currency=(ext.currency.value or cur) if ext.currency else cur,
        lines=lines,
        subtotal_minor=_minor(ext.subtotal.value, cur),
        vat_minor=_minor(ext.vat_amount.value, cur),
        total_minor=_minor(ext.total.value, cur),
        total_alt_minor=_minor(ext.total_alt.value, cur) if ext.total_alt and ext.total_alt.value is not None else None,
        date_format_observed=ext.date_format_observed,
        confidence=conf,
        notes=list(ext.notes),
    )
    if n.currency in ("ج.م", "جنيه", "L.E.", "LE"):
        n.currency = "EGP"
    if n.total_alt_minor is not None and n.total_minor is not None and n.total_alt_minor != n.total_minor:
        n.conflicts.append({"field": "total", "primary": n.total_minor, "alt": n.total_alt_minor})
        n.confidence["total"] = round(n.confidence.get("total", 0) * 0.4, 3)
    return n


def penalise(n: Normalised, field_name: str, factor: float) -> None:
    n.confidence[field_name] = round(n.confidence.get(field_name, 0.0) * factor, 3)


def dumps(n: Normalised) -> str:
    return json.dumps(n.to_json(), ensure_ascii=False)
