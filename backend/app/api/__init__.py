"""All /api/v1 routers."""

from fastapi import APIRouter

from app.api.v1 import approvals, auth, books, business, clock, sales, stock, users

router = APIRouter()
for module in (auth, users, clock, business, approvals, stock, sales, books):
    router.include_router(module.router)
