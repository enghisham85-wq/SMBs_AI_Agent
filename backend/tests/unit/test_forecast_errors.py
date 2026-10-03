"""Forecast accuracy for the daily check: one read of the week's sales and forecasts for all products."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.agents.stock import demand
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.models.finance_master import Sale
from app.models.master import Item
from app.models.stock_ops import DemandForecast

TODAY = date(2026, 10, 5)


def _day(n: int) -> date:
    return TODAY - timedelta(days=n)


async def test_errors_use_that_mornings_forecast_else_the_latest_before(business: dict[str, Any]) -> None:
    bid = business["id"]
    latte, tea, scone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def fc(item: uuid.UUID, day: date, generated: date, expected: str) -> DemandForecast:
        e = Decimal(expected)
        return DemandForecast(business_id=bid, item_id=item, forecast_date=day, low=e, expected=e, high=e,
                              method="same_weekday_avg", generated_on=generated)

    def sale(day: date, lines: list[tuple[uuid.UUID, int]]) -> Sale:
        return Sale(business_id=bid, date=day, lines=[{"sold_item_id": str(i), "qty": q, "amount_minor": 0} for i, q in lines],
                    amount_total=Money(0, "EGP"), payment_method="cash", source="manual")

    async with write_session() as s:
        for iid, name in ((latte, "Latte"), (tea, "Tea"), (scone, "Scone")):
            s.add(Item(id=iid, business_id=bid, name_en=name, unit="cup", is_sold=True,
                       unit_cost=Money(0, "EGP"), sale_price=Money(0, "EGP")))
        await s.flush()
        s.add_all([
            fc(latte, _day(1), _day(1), "10"), fc(latte, _day(1), _day(4), "99"),  # that morning's forecast wins
            fc(latte, _day(2), _day(4), "20"),  # no forecast that morning: the latest one before it
            fc(latte, _day(3), _day(2), "50"),  # made after the day: never used
            fc(tea, _day(1), _day(1), "5"),
            sale(_day(1), [(latte, 3), (tea, 5)]), sale(_day(1), [(latte, 5)]),
            sale(_day(2), [(tea, 2)]),  # no latte sold that day: actual 0 (floored at 1 unit)
            sale(_day(3), [(tea, 1)]),
            fc(tea, _day(4), _day(4), "7"),  # _day(4) has no sales records at all: a data gap, skipped
        ])

    async with read_session() as s:
        out = await demand.forecast_errors(s, bid, [latte, tea, scone], TODAY)

    assert out[latte] == (0.25, (0.25 + 20.0) / 2)
    assert out[tea] == (0.0, 0.0)
    assert out[scone] == (None, None)
