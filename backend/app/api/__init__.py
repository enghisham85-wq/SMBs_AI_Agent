"""All /api/v1 routers."""

from fastapi import APIRouter

from app.api.v1 import approvals, auth, books, business, cash, clock, harness, sales, settings, stock, users

router = APIRouter()
for module in (auth, users, clock, business, settings, approvals, stock, sales, books, cash, harness):
    router.include_router(module.router)
