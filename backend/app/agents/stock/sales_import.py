"""Sales CSV import and manual daily entry (T064) — the real-data alternative to the demo feed."""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, func, select

from app.core import clock
from app.core.i18n import normalize_arabic_name, normalize_digits
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.harness.action_spec import ActionContext, ActionSpec, VerifyOutcome, register
from app.models.clock import BusinessClock
from app.models.finance_master import PAYMENT_METHODS, Sale
from app.models.master import Item
from app.models.tenancy import Business

DEFAULT_MAPPING = {"date": "date", "item": "item", "qty": "qty", "amount": "amount", "payment_method": "payment_method"}


@dataclass
class ParsedFile:
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    sha256: str = ""


def _parse_date(text: str, fmt: str) -> date:
    t = normalize_digits(text.strip()).replace("/", "-")
    if len(t.split("-")[0]) == 4:
        return date.fromisoformat(t)
    a, b, c = (int(x) for x in t.split("-"))
    return date(c, b, a) if fmt == "DMY" else date(c, a, b)


async def parse_csv(business_id: uuid.UUID, data: bytes, mapping: dict[str, str] | None = None) -> ParsedFile:
    mapping = {**DEFAULT_MAPPING, **(mapping or {})}
    out = ParsedFile(sha256=hashlib.sha256(data).hexdigest())
    async with read_session() as s:
        business = await s.get(Business, business_id)
        assert business is not None
        items = [i for i in (await s.execute(select(Item).where(Item.business_id == business_id,
                                                               Item.is_sold.is_(True)))).scalars()]
    by_name: dict[str, Item] = {}
    for i in items:
        by_name[normalize_arabic_name(i.name_en)] = i
        if i.name_ar:
            by_name[normalize_arabic_name(i.name_ar)] = i
        by_name[str(i.id)] = i
    today = clock.today()
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for n, raw in enumerate(reader, start=2):  # row 1 is the header
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        try:
            d = _parse_date(row.get(mapping["date"], ""), business.default_date_format)
        except (ValueError, TypeError):
            out.errors.append({"row": n, "reason": "invalid date"})
            continue
        if d > today:
            out.errors.append({"row": n, "reason": "date is in the future"})
            continue
        name = row.get(mapping["item"], "")
        item = by_name.get(normalize_arabic_name(name)) or by_name.get(name)
        if item is None:
            out.errors.append({"row": n, "reason": f"unknown item: {name}"})
            continue
        try:
            qty = Decimal(normalize_digits(row.get(mapping["qty"], "")).replace(",", ""))
            amount = Money.from_decimal(normalize_digits(row.get(mapping["amount"], "")).replace(",", ""), business.currency)
        except (InvalidOperation, ValueError):
            out.errors.append({"row": n, "reason": "invalid quantity or amount"})
            continue
        if qty <= 0:
            out.errors.append({"row": n, "reason": "quantity must be greater than 0"})
            continue
        if amount.amount_minor < 0:
            out.errors.append({"row": n, "reason": "amount must not be negative"})
            continue
        method = row.get(mapping["payment_method"], "").lower() or "cash"
        if method not in PAYMENT_METHODS:
            out.errors.append({"row": n, "reason": f"invalid payment method: {method}"})
            continue
        key = f"{d.isoformat()}|{item.id}|{qty}|{amount.amount_minor}|{method}"
        out.rows.append({"row": n, "date": d.isoformat(), "item_id": str(item.id), "qty": str(qty),
                         "amount_minor": amount.amount_minor, "payment_method": method,
                         "row_hash": hashlib.sha256(key.encode()).hexdigest()})
    return out


async def file_already_imported(business_id: uuid.UUID, sha256: str) -> bool:
    async with read_session() as s:
        return (await s.execute(select(Sale.id).where(Sale.business_id == business_id,
                                                      Sale.import_batch_id == f"csv:{sha256}").limit(1))).first() is not None


# ------------------------------------------------------------------ import_sales action
async def _execute(ctx: ActionContext, inputs: dict[str, Any]) -> dict[str, Any]:
    async with read_session() as s:
        business = await s.get(Business, ctx.business_id)
    assert business is not None
    batch = inputs["batch_id"]
    seen: set[str] = set()
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    skipped = 0
    for r in inputs["rows"]:
        if r["row_hash"] in seen:
            skipped += 1
            continue
        seen.add(r["row_hash"])
        grouped.setdefault((r["date"], r["payment_method"]), []).append(r)
    # Ids are assigned here, so the rows go in with one flush instead of one per sale under the write lock.
    sales = [Sale(id=uuid.uuid4(), business_id=ctx.business_id, date=date.fromisoformat(d),
                  lines=[{"sold_item_id": r["item_id"], "qty": float(Decimal(r["qty"])), "amount_minor": r["amount_minor"]}
                         for r in rows],
                  amount_total=Money(sum(r["amount_minor"] for r in rows), business.currency),
                  payment_method=method, source=inputs.get("source", "csv_upload"), import_batch_id=batch,
                  row_hash=rows[0]["row_hash"])
             for (d, method), rows in grouped.items()]
    created = [str(sale.id) for sale in sales]
    async with write_session() as s:
        s.add_all(sales)
        await s.flush()
    return {"sale_ids": created, "imported_rows": len(seen), "skipped_duplicates": skipped,
            "total_minor": sum(r["amount_minor"] for r in inputs["rows"] if r["row_hash"] in seen),
            "dates": sorted({d for d, _ in grouped})}


async def _verify(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> VerifyOutcome:
    async with read_session() as s:
        rows = (await s.execute(select(Sale).where(Sale.business_id == ctx.business_id,
                                                   Sale.import_batch_id == inputs["batch_id"]))).scalars().all()
    total = sum(r.amount_total.amount_minor for r in rows)
    lines = sum(len(r.lines) for r in rows)
    ok = total == result["total_minor"] and lines == result["imported_rows"]
    return VerifyOutcome(ok, {"rows_in_db": lines, "expected_rows": result["imported_rows"], "total_minor": total})


async def _compensate(ctx: ActionContext, inputs: dict[str, Any], result: dict[str, Any]) -> None:
    async with write_session() as s:
        await s.execute(delete(Sale).where(Sale.business_id == ctx.business_id, Sale.import_batch_id == inputs["batch_id"]))


async def _finalize(ctx: ActionContext, inputs: dict[str, Any], outcome: str, result: dict[str, Any]) -> None:
    """Dates the daily run already processed get their stock deducted now, closing any sales-gap incident."""
    if outcome != "completed":
        return
    async with read_session() as s:
        clk = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == ctx.business_id))).scalar_one_or_none()
    last = clk.last_run_date if clk else None
    from app.agents.stock.graphs import step_stock_update
    from app.harness.incidents import resolve_incident
    from app.models.harness import Incident

    for d in result.get("dates", []):
        day = date.fromisoformat(d)
        if last is not None and day <= last:
            await step_stock_update(ctx.business_id, day)
        async with read_session() as s:
            incs = (await s.execute(select(Incident).where(Incident.business_id == ctx.business_id,
                                                           Incident.dedupe_key == f"sales_data_gap:{d}",
                                                           Incident.status == "open"))).scalars().all()
        for inc in incs:
            await resolve_incident(inc.id, root_cause="sales for the day were missing", category="data_gap",
                                   action_taken=f"sales imported ({inputs.get('source', 'csv_upload')})")


def register_spec() -> None:
    register(ActionSpec(name="import_sales", agent="stock", risk_class="reversible", execute=_execute,
                        title_en="Import sales", title_ar="استيراد المبيعات", verify=_verify, compensate=_compensate,
                        on_finalize=_finalize))


async def count_for_date(business_id: uuid.UUID, d: date) -> int:
    async with read_session() as s:
        return int((await s.execute(select(func.count()).select_from(Sale).where(Sale.business_id == business_id,
                                                                                Sale.date == d))).scalar_one())
