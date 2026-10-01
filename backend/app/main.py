"""FastAPI application: API, LangGraph runtime, daily scheduler and Telegram poller in one process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.common import first_business_id
from app.config import get_settings
from app.core import clock
from app.core.errors import AppError
from app.db.engine import init_engine
from app.graphs import runtime

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app import wiring
    from app.db.migrate import upgrade_head

    settings = get_settings()
    init_engine()
    await upgrade_head()
    await runtime.init()
    # Graphs, action specs, daily steps and every agent's event handlers (agents/*/handlers.py).
    wiring.register_all()
    tasks: list[asyncio.Task[Any]] = []
    bid = await first_business_id()
    if bid is not None:
        await clock.load(bid)
        if not settings.DEMO_MODE:
            from app.core.scheduler import real_time_loop

            tasks.append(asyncio.create_task(real_time_loop(bid)))
    if settings.TELEGRAM_BOT_TOKEN:
        from app.approvals import telegram_bot

        tasks.append(asyncio.create_task(telegram_bot.run_polling(settings.TELEGRAM_BOT_TOKEN)))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await runtime.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Small Business Agent Suite", version="0.1.0", lifespan=lifespan,
                  openapi_url="/api/v1/openapi.json", docs_url="/api/v1/docs")

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "validation_error", "message_en": "Some fields are invalid.",
                       "message_ar": "بعض الحقول غير صالحة.",
                       "fields": [{"loc": list(e.get("loc", [])), "msg": e.get("msg")} for e in exc.errors()]}},
            status_code=422,
        )

    if get_settings().APP_ENV != "prod":  # FR-046 guard: figures without data_as_of fail loudly
        from app.core.freshness import FreshnessCheck

        app.add_middleware(FreshnessCheck)

    from app.api import router as api_router

    app.include_router(api_router, prefix="/api/v1")

    settings = get_settings()
    if settings.BOOSTHIS_PROJECT_KEY and settings.APP_ENV != "test":
        try:
            import boosthis
        except ImportError as exc:  # the kit needs the Unix-only `resource` module, so not on native Windows
            log.warning("Boosthis not started: %s", exc)
        else:
            boosthis.enable_telemetry(invite_key=settings.BOOSTHIS_PROJECT_KEY,
                                      endpoint="https://www.boosthis.com/api", app_name="SMBAgents API")
            boosthis.mount(app, bubble=True)
    return app


app = create_app()
