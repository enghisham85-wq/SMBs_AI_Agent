"""Application settings, read from environment variables or backend/.env."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SESSION_SECRET = "change-me-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "sqlite+aiosqlite:///./var/app.db"
    # separate file so checkpoints don't fight business writes for the lock
    CHECKPOINT_DB_PATH: str = "./var/checkpoints.db"
    SQLITE_BUSY_TIMEOUT_MS: int = 5000

    APP_ENV: Literal["dev", "test", "prod"] = "dev"
    DEMO_MODE: bool = True
    # offline: deterministic stand-ins, no API calls
    LLM_MODE: Literal["live", "record", "replay", "offline"] = "offline"
    LLM_FIXTURES_DIR: str = "./tests/fixtures/llm"
    ANTHROPIC_API_KEY: str | None = None
    # openai_compatible uses LLM_BASE_URL + LLM_API_KEY. Gateways name models their own way, hence LLM_MODEL.
    LLM_PROVIDER: Literal["anthropic", "openai_compatible"] = "anthropic"
    LLM_BASE_URL: str | None = None
    LLM_API_KEY: str | None = None
    LLM_MODEL: str | None = None
    TELEGRAM_BOT_TOKEN: str | None = None

    FILES_DIR: str = "./var/files"
    SAMPLE_INVOICES_DIR: str = "./var/sample_invoices"
    SESSION_SECRET: str = DEFAULT_SESSION_SECRET

    DEFAULT_COUNTRY: str = "EG"
    # overrides the country profile's VAT for new businesses
    DEFAULT_VAT_RATE_PERCENT: Decimal | None = Field(default=None, ge=0, le=100)


class InsecureSettingsError(RuntimeError):
    pass


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # Anyone could forge cookies with the default secret. Checked here, not in a validator,
    # so the error doesn't echo the other settings.
    if settings.APP_ENV == "prod" and settings.SESSION_SECRET == DEFAULT_SESSION_SECRET:
        raise InsecureSettingsError("SESSION_SECRET must be set to a private random value when APP_ENV=prod")
    return settings
