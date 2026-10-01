"""The single path to Claude (research R4, contracts/llm-outputs.md).

Modes (LLM_MODE):
- live:    call the API.
- record:  call the API and save the parsed output as a fixture keyed by request hash.
- replay:  read the fixture; raise MissingFixtureError if absent (tests, offline demo).
- offline: never call the API; use the caller's deterministic `offline` fallback. Clearly a
           stand-in for development and demos without an API key.

Every call uses model `claude-opus-5-5` with structured output (Pydantic), an effort level per
role, server-side refusal fallback, and top-level prompt caching of the stable prefix.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from app.config import get_settings

log = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"
EFFORT: dict[str, str] = {
    "extraction": "high",
    "verifier": "high",
    "classification": "low",
    "message": "medium",
    "incident": "medium",
}
# Thinking is always on (Claude Opus 5.5) and counts toward max_tokens, so leave room for it as well as
# the reply. Each role sets its effort explicitly: the model's own default is "medium".
MAX_TOKENS: dict[str, int] = {"classification": 4000}
DEFAULT_MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"

T = TypeVar("T", bound=BaseModel)


class LLMRefusalError(RuntimeError):
    """The model declined (stop_reason == "refusal"). Callers treat it as low confidence."""


class MissingFixtureError(RuntimeError):
    pass


class LLMUnavailableError(RuntimeError):
    pass


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def document_block(data: bytes, media_type: str) -> dict[str, Any]:
    b64 = base64.b64encode(data).decode("ascii")
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": b64}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}}


def _content_for_hash(blocks: list[dict[str, Any]]) -> list[Any]:
    out: list[Any] = []
    for b in blocks:
        src = b.get("source")
        if isinstance(src, dict) and "data" in src:
            out.append({"type": b["type"], "sha256": hashlib.sha256(src["data"].encode()).hexdigest()})
        else:
            out.append(b)
    return out


def request_hash(role: str, system: str, blocks: list[dict[str, Any]], schema_name: str) -> str:
    raw = json.dumps(
        # The model and effort change the answer, so a recorded fixture only replays for the same pair.
        {"model": MODEL, "effort": EFFORT.get(role, "medium"), "role": role, "system": system,
         "content": _content_for_hash(blocks), "schema": schema_name},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LLMClient:
    def __init__(self, mode: str | None = None, fixtures_dir: str | None = None) -> None:
        settings = get_settings()
        self.mode = mode or settings.LLM_MODE
        self.fixtures_dir = Path(fixtures_dir or settings.LLM_FIXTURES_DIR)
        self._client: Any = None

    def _api(self) -> Any:
        if self._client is None:
            import anthropic

            settings = get_settings()
            kwargs: dict[str, Any] = {"max_retries": 2}
            if settings.ANTHROPIC_API_KEY:
                kwargs["api_key"] = settings.ANTHROPIC_API_KEY
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    async def parse(
        self,
        role: str,
        system: str,
        content: list[dict[str, Any]],
        output_model: type[T],
        *,
        offline: Callable[[], T],
    ) -> T:
        if self.mode == "offline":
            return offline()

        key = request_hash(role, system, content, output_model.__name__)
        fixture = self.fixtures_dir / f"{key}.json"
        if self.mode == "replay":
            if not fixture.exists():
                raise MissingFixtureError(f"no LLM fixture for {role}/{output_model.__name__} ({key[:12]})")
            return output_model.model_validate_json(fixture.read_text(encoding="utf-8"))

        result = await self._call(role, system, content, output_model)
        if self.mode == "record":
            self.fixtures_dir.mkdir(parents=True, exist_ok=True)
            fixture.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result

    async def _call(self, role: str, system: str, content: list[dict[str, Any]], output_model: type[T]) -> T:
        import anthropic

        try:
            response = await self._api().beta.messages.parse(
                model=MODEL,
                max_tokens=MAX_TOKENS.get(role, DEFAULT_MAX_TOKENS),
                system=system,
                messages=[{"role": "user", "content": content}],
                output_format=output_model,
                output_config={"effort": EFFORT.get(role, "medium")},
                thinking={"type": "adaptive"},
                cache_control={"type": "ephemeral"},
                fallbacks="default",
                betas=[FALLBACK_BETA],
            )
        except anthropic.RateLimitError as exc:
            raise LLMUnavailableError("rate limited") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError("connection failed") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"API error {exc.status_code}") from exc

        if response.stop_reason == "refusal":
            raise LLMRefusalError(str(getattr(response, "stop_details", "")))
        parsed = response.parsed_output
        if parsed is None:
            raise LLMUnavailableError(f"no parsed output (stop_reason={response.stop_reason})")
        return parsed  # type: ignore[no-any-return]


_default: LLMClient | None = None


def get_llm() -> LLMClient:
    global _default
    if _default is None:
        _default = LLMClient()
    return _default


def set_llm(client: LLMClient | None) -> None:
    """Tests inject a fake client here."""
    global _default
    _default = client
