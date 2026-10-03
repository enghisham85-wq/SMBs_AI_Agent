from __future__ import annotations

import statistics
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.harness.action_spec import Check
from app.models.finance_master import Sale
from app.models.master import Item, SupplierPrice
from app.models.purchasing import OPEN_STATUSES, PurchaseOrder, PurchaseOrderLine


def negative_or_impossible_stock(levels: dict[uuid.UUID, Decimal], items: dict[uuid.UUID, Item]) -> list[Check]:
    out: list[Check] = []
    for item_id, qty in levels.items():
        item = items.get(item_id)
        if item is None:
            continue
        if qty < 0:
            out.append(Check("negative_or_impossible_stock", False, {"item_id": item_id, "item": item.name_en, "qty": qty,
                             "likely_cause": "missing sales mapping or unrecorded delivery"},
                             reason_en=f"{item.name_en} stock is negative ({qty} {item.unit}); a delivery may be unrecorded",
                             reason_ar=f"مخزون {item.name_ar} سالب ({qty} {item.unit})؛ قد تكون هناك توريدة غير مسجلة"))
        elif item.storage_capacity is not None and qty > item.storage_capacity:
            out.append(Check("negative_or_impossible_stock", False, {"item_id": item_id, "item": item.name_en, "qty": qty,
                             "capacity": item.storage_capacity},
                             reason_en=f"{item.name_en} stock {qty} exceeds storage capacity {item.storage_capacity}",
                             reason_ar=f"مخزون {item.name_ar} {qty} يتجاوز السعة {item.storage_capacity}"))
    return out


def count_variance(item: Item, counted: Decimal, calculated: Decimal, tolerance_pct: float) -> Check:
    base = max(abs(calculated), Decimal("0.0001"))
    pct = float(abs(counted - calculated) / base * 100)
    ok = pct <= tolerance_pct
    return Check("count_variance", ok, {"item_id": item.id, "counted": counted, "calculated": calculated,
                                        "variance_pct": round(pct, 1)},
                 reason_en=f"{item.name_en} count differs from calculated stock by {pct:.1f}%",
                 reason_ar=f"جرد {item.name_ar} يختلف عن المخزون المحسوب بنسبة {pct:.1f}%")


async def price_history(s: AsyncSession, supplier_id: uuid.UUID, item_id: uuid.UUID, before: date) -> list[int]:
    rows = (await s.execute(select(SupplierPrice).where(SupplierPrice.supplier_id == supplier_id,
                                                        SupplierPrice.item_id == item_id,
                                                        SupplierPrice.valid_from < before)
                            .order_by(SupplierPrice.valid_from.desc()).limit(5))).scalars().all()
    return [r.price.amount_minor for r in rows]


def price_sanity(item: Item, new_price_minor: int, history: list[int], pct_limit: float) -> Check:
    if not history:
        return Check("price_sanity", True, {"item_id": item.id, "note": "no price history"})
    median = statistics.median(history)
    change = (new_price_minor - median) / median * 100 if median else 0.0
    ok = abs(change) <= pct_limit
    return Check("price_sanity", ok, {"item_id": item.id, "new_price_minor": new_price_minor, "median_minor": median,
                                      "change_pct": round(change, 1)}, ask=True,
                 reason_en=f"the price of {item.name_en} changed {change:+.0f}% from its usual price",
                 reason_ar=f"سعر {item.name_ar} تغيّر بنسبة {change:+.0f}% عن سعره المعتاد")


async def duplicate_po(s: AsyncSession, business_id: uuid.UUID, supplier_id: uuid.UUID, item_ids: set[str],
                       exclude_po: uuid.UUID | None = None) -> Check:
    # sent orders already count as incoming stock, so only unsent ones can be duplicates
    unsent = [st for st in OPEN_STATUSES if st not in ("sent", "partially_received")]
    q = select(PurchaseOrder).where(PurchaseOrder.business_id == business_id, PurchaseOrder.supplier_id == supplier_id,
                                    PurchaseOrder.status.in_(unsent))
    for po in (await s.execute(q)).scalars():
        if exclude_po is not None and po.id == exclude_po:
            continue
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars().all()
        overlap = item_ids & {str(line.item_id) for line in lines}
        if overlap:
            return Check("duplicate_po", False, {"existing_po_id": po.id, "existing_po": po.number,
                                                 "items": sorted(overlap)}, ask=True,
                         reason_en=f"an open order {po.number} for the same items already exists",
                         reason_ar=f"يوجد أمر شراء مفتوح {po.number} لنفس الأصناف")
    return Check("duplicate_po", True, {})


def unit_mismatch(item: Item, line_unit: str, line_pack: Decimal, price: SupplierPrice | None) -> Check:
    problems = []
    if line_unit != item.unit:
        problems.append(f"order unit {line_unit} vs stock unit {item.unit}")
    if price is not None and line_pack != price.pack_size:
        problems.append(f"pack size {line_pack} vs usual {price.pack_size}")
    ok = not problems
    return Check("unit_mismatch", ok, {"item_id": item.id, "problems": problems}, ask=True,
                 reason_en=f"{item.name_en}: " + "; ".join(problems) if problems else "",
                 reason_ar=f"{item.name_ar}: " + "؛ ".join(problems) if problems else "")


async def sales_data_gap(s: AsyncSession, business_id: uuid.UUID, d: date) -> Check:
    n = (await s.execute(select(Sale.id).where(Sale.business_id == business_id, Sale.date == d).limit(1))).first()
    return Check("sales_data_gap", n is not None, {"date": d},
                 reason_en=f"no sales were received for {d.isoformat()}",
                 reason_ar=f"لم تصل أي مبيعات ليوم {d.isoformat()}")


def forecast_accuracy(item: Item, mape_value: float | None, threshold: float) -> Check:
    ok = mape_value is None or mape_value <= threshold
    return Check("forecast_accuracy", ok, {"item_id": item.id, "mape": mape_value, "threshold": threshold},
                 reason_en=f"{item.name_en} forecast error is {0 if mape_value is None else mape_value * 100:.0f}%",
                 reason_ar=f"خطأ توقع {item.name_ar} هو {0 if mape_value is None else mape_value * 100:.0f}%")


async def late_deliveries(s: AsyncSession, business_id: uuid.UUID, today: date) -> list[dict[str, Any]]:
    """Open POs past their expected date: [{po, days_late, critical}]."""
    rows = (await s.execute(select(PurchaseOrder).where(PurchaseOrder.business_id == business_id,
                                                        PurchaseOrder.status.in_(("sent", "partially_received")),
                                                        PurchaseOrder.expected_date < today))).scalars().all()
    out = []
    for po in rows:
        lines = (await s.execute(select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id))).scalars().all()
        items = {i.id: i for i in (await s.execute(select(Item).where(Item.id.in_([li.item_id for li in lines])))).scalars()}
        out.append({"po": po, "days_late": (today - po.expected_date).days,
                    "critical": any(items[li.item_id].is_critical for li in lines if li.item_id in items),
                    "lines": lines, "items": items})
    return out
