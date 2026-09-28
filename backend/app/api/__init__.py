"""All /api/v1 routers."""

from fastapi import APIRouter

from app.api.v1 import approvals, auth, business, clock, users

router = APIRouter()
for module in (auth, users, clock, business, approvals):
    router.include_router(module.router)
