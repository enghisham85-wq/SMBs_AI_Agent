"""Sales import and manual entry (T064). Managers only; staff never see amounts."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, File, Form, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agents.stock.sales_import import file_already_imported, parse_csv
from app.api.common import J
from app.core import clock
from app.core.auth import CurrentUser, RequireManager
from app.core.errors import AppError
from app.db.engine import read_session
from app.harness.graph import run_action
from app.models.finance_master import Sale
from app.models.master import Item

router = APIRouter(tags=["sales"])


def _result(out: dict, errors: list) -> dict:
    res = out.get("result") or {}
    return {"batch_id": out.get("batch_id"), "outcome": out["outcome"], "imported": res.get("imported_rows", 0),
            "skipped_duplicates": res.get("skipped_duplicates", 0), "errors": errors}


@router.post("/sales/import", status_code=201)
async def import_csv(file: UploadFile = File(...), mapping: str | None = Form(None),
                     user: CurrentUser = RequireManager) -> Response:
    data = await file.read()
    parsed = await parse_csv(user.business_id, data, json.loads(mapping) if mapping else None)
    if await file_already_imported(user.business_id, parsed.sha256):
        raise AppError(409, "file_already_imported", message_en="This file was already imported.",
                       message_ar="تم استيراد هذا الملف من قبل.")
    if not parsed.rows:
        return J({"batch_id": None, "outcome": "nothing_to_import", "imported": 0, "skipped_duplicates": 0,
                  "errors": parsed.errors}, 201)
    batch = f"csv:{parsed.sha256}"
    out = await run_action("import_sales", {"batch_id": batch, "rows": parsed.rows, "source": "csv_upload"}, user.business_id)
    out["batch_id"] = batch
    return J(_result(out, parsed.errors), 201)


class ManualLine(BaseModel):
    item_id: uuid.UUID
    qty: Decimal = Field(gt=0)
    amount: Decimal = Field(ge=0)


class ManualIn(BaseModel):
    date: date
    lines: list[ManualLine] = Field(min_length=1)
    payment_method: Literal["cash", "card", "transfer", "credit"] = "cash"


@router.post("/sales/manual", status_code=201)
async def manual_entry(body: ManualIn, user: CurrentUser = RequireManager) -> Response:
    if body.date > clock.today():
        raise AppError(422, "future_date", message_en="The date cannot be in the future.", message_ar="لا يمكن أن يكون التاريخ في المستقبل.")
    from app.api.common import get_business
    from app.db.types import Money

    b = await get_business(user.business_id)
    rows = []
    async with read_session() as s:
        for ln in body.lines:
            item = await s.get(Item, ln.item_id)
            if item is None or item.business_id != user.business_id or not item.is_sold:
                raise AppError(422, "unknown_item", message_en=f"Unknown item {ln.item_id}", message_ar="صنف غير معروف.")
            minor = Money.from_decimal(ln.amount, b.currency).amount_minor
            key = f"{body.date}|{ln.item_id}|{ln.qty}|{minor}|{body.payment_method}|{uuid.uuid4()}"
            rows.append({"row": len(rows) + 1, "date": body.date.isoformat(), "item_id": str(ln.item_id), "qty": str(ln.qty),
                         "amount_minor": minor, "payment_method": body.payment_method,
                         "row_hash": hashlib.sha256(key.encode()).hexdigest()})
    batch = f"manual:{uuid.uuid4().hex}"
    out = await run_action("import_sales", {"batch_id": batch, "rows": rows, "source": "manual"}, user.business_id)
    out["batch_id"] = batch
    return J(_result(out, []), 201)


@router.get("/sales")
async def list_sales(date: date, user: CurrentUser = RequireManager) -> Response:
    async with read_session() as s:
        rows = (await s.execute(select(Sale).where(Sale.business_id == user.business_id, Sale.date == date))).scalars().all()
        items = {str(i.id): i for i in (await s.execute(select(Item).where(Item.business_id == user.business_id))).scalars()}
    return J({"date": date, "sales": [
        {"id": r.id, "payment_method": r.payment_method, "source": r.source, "amount_total": r.amount_total,
         "lines": [{**ln, "name_en": items[ln["sold_item_id"]].name_en if ln["sold_item_id"] in items else "",
                    "name_ar": items[ln["sold_item_id"]].name_ar if ln["sold_item_id"] in items else ""} for ln in r.lines]}
        for r in rows]})
