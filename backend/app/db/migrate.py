"""Run Alembic migrations programmatically (startup and seed)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic.config import Config

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def upgrade_head_sync() -> None:
    command.upgrade(_config(), "head")


async def upgrade_head() -> None:
    # Alembic's async env calls asyncio.run(), so run it in a worker thread.
    await asyncio.to_thread(upgrade_head_sync)
