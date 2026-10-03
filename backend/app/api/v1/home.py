"""Home: health strip, today's decisions and alerts by urgency.

The strip's figures come from the same functions the Stock, Cash and Books views use, so Home
never shows a number those views would contradict.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Response
from sqlalchemy import select

from app.api.common import J, with_freshness
from app.api.v1 import books, cash, stock
from app.approvals import service as approvals
from app.core import clock
from app.core.auth import CurrentUser, RequireManager
from app.db.engine import read_session
from app.models.harness import ApprovalRequest
from app.models.tenancy import ROLE_RANK

router = APIRouter(tags=["home"])


async def _pending(business_id: uuid.UUID) -> list[ApprovalRequest]:
    async with read_session() as s:
        return list((await s.execute(select(ApprovalRequest).where(
            ApprovalRequest.business_id == business_id, ApprovalRequest.status == "pending"))).scalars())


@router.get("/home")
async def home(user: CurrentUser = RequireManager) -> Response:
    # Independent reads, each in its own session (separate pooled connections; SQLite WAL lets
    # readers run side by side), so they run concurrently.
    items, forecast, recon, review, pending = await asyncio.gather(
        stock.items_data(user.business_id, show_money=True), cash.forecast_data(user.business_id, "30d"),
        books.reconciliation_summary(user.business_id), books.review_queue_data(user.business_id),
        _pending(user.business_id))

    rows = items["items"]
    warnings = [i for i in rows if i["reorder_status"] == "reorder" or i["expiry_risk"]]
    stock_strip = {"items": len(rows), "ok": len(rows) - len(warnings), "warnings": len(warnings),
                   "critical_warnings": sum(1 for i in warnings if i["is_critical"]),
                   "on_order": sum(1 for i in rows if i["reorder_status"] == "on_order"),
                   "data_as_of": items["data_as_of"]}
    cash_strip = {"lowest": forecast["lowest"], "buffer": forecast["buffer"],
                  "first_below_buffer": forecast["first_below_buffer"], "confidence": forecast["confidence"],
                  "data_as_of": forecast["data_as_of"]}
    review_count = len(review["questions"]) + len(review["held_invoices"]) + len(review["suggested_matches"])
    books_strip = {"percent_reconciled": recon["percent_matched"], "unmatched": recon["open_count"],
                   "review_count": review_count, "data_as_of": recon["data_as_of"]}

    mine = [r for r in pending if ROLE_RANK[user.role] >= ROLE_RANK.get(r.required_role, 1)]
    # Most urgent first; among equals, the nearest deadline, then the newest.
    decisions = sorted((r for r in mine if r.kind != "alert"),
                       key=lambda r: (-r.urgency, r.deadline is None, r.deadline or r.created_at, -r.created_at.timestamp()))
    alerts = sorted((r for r in mine if r.kind == "alert"), key=lambda r: (-r.urgency, -r.created_at.timestamp()))
    newest = max((r.created_at for r in pending), default=None)
    return J(with_freshness({
        "health": {"stock": stock_strip, "cash": cash_strip, "books": books_strip},
        "decisions": [approvals.to_dict(r) for r in decisions],
        "alerts": [approvals.to_dict(r) for r in alerts],
        "business_date": clock.today(),
    }, {"stock": _first(items, "stock"), "forecast": _first(forecast, "forecast"), "bank": _first(recon, "bank"),
        "approvals": newest}))


def _first(body: dict[str, Any], source: str) -> Any:
    return (body.get("data_as_of") or {}).get(source)
