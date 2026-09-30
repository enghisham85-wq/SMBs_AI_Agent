"""Chaos mode endpoints (contracts/rest-api.md "Chaos mode"): owner only, demo mode only."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app.api.common import J
from app.chaos import service
from app.chaos.scenarios import ChaosError
from app.config import get_settings
from app.core.auth import CurrentUser, RequireOwner
from app.core.errors import AppError, not_found

router = APIRouter(tags=["chaos"])


@router.get("/chaos/scenarios")
async def list_scenarios(user: CurrentUser = RequireOwner) -> Response:
    recent = await service.recent(user.business_id)
    return J({"demo_mode": get_settings().DEMO_MODE, "scenarios": service.scenarios(),
              "recent": [service.to_json(r) for r in recent]})


class InjectIn(BaseModel):
    scenario: str
    params: dict[str, Any] | None = None


@router.post("/chaos/inject")
async def inject(body: InjectIn, user: CurrentUser = RequireOwner) -> Response:
    try:
        row = await service.inject(user.business_id, body.scenario, body.params, user.id)
    except service.ChaosDisabledError as exc:
        raise AppError(403, "chaos_disabled", message_en="Chaos mode is only available in demo mode.",
                       message_ar="وضع الفوضى متاح في الوضع التجريبي فقط.") from exc
    except service.ChaosBusyError as exc:
        raise AppError(409, "chaos_busy", message_en="Another scenario is still running.",
                       message_ar="هناك سيناريو آخر قيد التشغيل.") from exc
    except ChaosError as exc:
        raise AppError(422, "chaos_invalid", message_en=str(exc), message_ar="لا يمكن تشغيل هذا السيناريو الآن.") from exc
    return J(service.to_json(row), 201)


@router.get("/chaos/injections/{injection_id}")
async def get_injection(injection_id: uuid.UUID, user: CurrentUser = RequireOwner) -> Response:
    row = await service.get(user.business_id, injection_id)
    if row is None:
        raise not_found("Injection")
    return J(service.to_json(row))
