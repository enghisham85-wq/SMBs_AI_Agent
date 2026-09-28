"""Business clock and demo reset (contracts/rest-api.md "Clock and demo")."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Response
from pydantic import BaseModel, model_validator
from sqlalchemy import select

from app.api.common import J
from app.config import get_settings
from app.core import clock, scheduler
from app.core.auth import CurrentUser, RequireOwner, RequireStaff
from app.core.errors import AppError
from app.db.engine import read_session
from app.models.clock import BusinessClock

router = APIRouter(tags=["clock"])


@router.get("/clock")
async def get_clock(user: CurrentUser = RequireStaff) -> Response:
    async with read_session() as s:
        row = (await s.execute(select(BusinessClock).where(BusinessClock.business_id == user.business_id))).scalar_one_or_none()
    if row is None:
        return J({"mode": "real", "current_date": date.today(), "last_run_date": None, "advancing": False})
    return J({"mode": row.mode, "current_date": row.current_date, "last_run_date": row.last_run_date,
              "advancing": row.advancing, "today": clock.today()})


class AdvanceIn(BaseModel):
    days: int | None = None
    to_date: date | None = None

    @model_validator(mode="after")
    def _one(self) -> AdvanceIn:
        if (self.days is None) == (self.to_date is None):
            raise ValueError("give exactly one of days or to_date")
        if self.days is not None and not 1 <= self.days <= 120:
            raise ValueError("days must be 1-120")
        return self


@router.post("/clock/advance")
async def advance(body: AdvanceIn, user: CurrentUser = RequireOwner) -> Response:
    try:
        processed = await scheduler.advance(user.business_id, days=body.days, to_date=body.to_date)
    except clock.ClockBusyError as exc:
        raise AppError(409, "clock_busy") from exc
    except clock.ClockError as exc:
        raise AppError(422, "clock_invalid", message_en=str(exc), message_ar="طلب غير صالح لتقديم الساعة.") from exc
    return J({"processed": processed, "current_date": clock.today()})


class ResetIn(BaseModel):
    country: str | None = None


@router.post("/demo/reset")
async def demo_reset(body: ResetIn | None = None, user: CurrentUser = RequireOwner) -> Response:
    if not get_settings().DEMO_MODE:
        raise AppError(403, "demo_only", message_en="Only available in demo mode.", message_ar="متاح في الوضع التجريبي فقط.")
    from app.seed.sample_cafe import reset_and_seed

    info = await reset_and_seed(country=(body.country if body else None))
    return J(info)
