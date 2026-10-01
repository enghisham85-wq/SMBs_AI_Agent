"""Application settings, read from environment variables or backend/.env."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "sqlite+aiosqlite:///./var/app.db"
    # LangGraph checkpoints live in their own SQLite file so they never compete with business writes.
    CHECKPOINT_DB_PATH: str = "./var/checkpoints.db"
    SQLITE_BUSY_TIMEOUT_MS: int = 5000

    # dev and test turn on extra response checks (every figure-bearing response carries data_as_of).
    APP_ENV: Literal["dev", "test", "prod"] = "dev"
    DEMO_MODE: bool = True
    # offline = deterministic stand-ins, no API calls (default until an API key and fixtures exist)
    LLM_MODE: Literal["live", "record", "replay", "offline"] = "offline"
    LLM_FIXTURES_DIR: str = "./tests/fixtures/llm"
    ANTHROPIC_API_KEY: str | None = None
    TELEGRAM_BOT_TOKEN: str | None = None

    FILES_DIR: str = "./var/files"
    # Sample invoices written by the generator (the offline extractor reads their ground truth).
    SAMPLE_INVOICES_DIR: str = "./var/sample_invoices"
    SESSION_SECRET: str = "change-me-in-production"

    DEFAULT_COUNTRY: str = "EG"
    # When set, overrides the chosen country profile's VAT rate for newly created businesses.
    DEFAULT_VAT_RATE_PERCENT: Decimal | None = Field(default=None, ge=0, le=100)


@lru_cache
def get_settings() -> Settings:
    return Settings()
