"""The single path to Claude (research R4, contracts/llm-outputs.md).

Modes (LLM_MODE):
- live:    call the API.
- record:  call the API and save the parsed output as a fixture keyed by request hash.
- replay:  read the fixture; raise MissingFixtureError if absent (tests, offline demo).
- offline: never call the API; use the caller's deterministic `offline` fallback. Clearly a
           stand-in for development and demos without an API key.

Every call uses model `claude-opus-5-5` with structured output (Pydantic), an effort level per
role and server-side refusal fallback. Callers mark their stable prefix (a document, the chart of
accounts) with `cache=True`; a top-level marker would land on the unique per-request text and never
be read back. Each call has a deadline; a timeout counts as the model being unavailable.
"""

from __future__ import annotations

import asyncio
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
# Per-attempt HTTP timeout (the SDK default is 600 s) and an overall deadline per role that also covers
# the SDK's retries. Every caller has a fallback, so a stuck call should give up rather than hang a node.
REQUEST_TIMEOUT_S = 90.0
REQUEST_TIMEOUT_S_BY_ROLE: dict[str, float] = {"extraction": 180.0}
DEADLINE_S: dict[str, float] = {
    "extraction": 300.0,
    "verifier": 150.0,
    "incident": 120.0,
    "message": 60.0,
    "classification": 60.0,
}
DEFAULT_DEADLINE_S = 120.0

T = TypeVar("T", bound=BaseModel)


class LLMRefusalError(RuntimeError):
    """The model declined (stop_reason == "refusal"). Callers treat it as low confidence."""


class MissingFixtureError(RuntimeError):
    pass


class LLMUnavailableError(RuntimeError):
    pass


def text_block(text: str, *, cache: bool = False) -> dict[str, Any]:
    """`cache=True` puts a prompt-cache breakpoint after this block: use it only at the end of a stable prefix."""
    block: dict[str, Any] = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def document_block(data: bytes, media_type: str, *, cache: bool = False) -> dict[str, Any]:
    """The file stays raw bytes here; the client base64-encodes it off the event loop when sending."""
    kind = "document" if media_type == "application/pdf" else "image"
    block: dict[str, Any] = {"type": kind, "source": {"type": "base64", "media_type": media_type, "data": data}}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def _content_for_hash(blocks: list[dict[str, Any]]) -> list[Any]:
    out: list[Any] = []
    for b in blocks:
        src = b.get("source")
        if isinstance(src, dict) and "data" in src:
            raw = src["data"]
            digest = hashlib.sha256(raw if isinstance(raw, bytes | bytearray) else str(raw).encode()).hexdigest()
            out.append({"type": b["type"], "sha256": digest})
        else:
            # Cache markers change billing, not the answer, so they stay out of the fixture key.
            out.append({k: v for k, v in b.items() if k != "cache_control"})
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


def _has_raw_bytes(blocks: list[dict[str, Any]]) -> bool:
    return any(isinstance((b.get("source") or {}).get("data"), bytes | bytearray) for b in blocks)


def _wire_content(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The blocks as the API takes them: file bytes base64-encoded."""
    out: list[dict[str, Any]] = []
    for b in blocks:
        src = b.get("source")
        if isinstance(src, dict) and isinstance(src.get("data"), bytes | bytearray):
            b = {**b, "source": {**src, "data": base64.b64encode(src["data"]).decode("ascii")}}
        out.append(b)
    return out


def _log_usage(role: str, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    log.info("llm %s: input=%s cache_read=%s cache_write=%s output=%s", role, getattr(usage, "input_tokens", None),
             getattr(usage, "cache_read_input_tokens", None), getattr(usage, "cache_creation_input_tokens", None),
             getattr(usage, "output_tokens", None))


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
            kwargs: dict[str, Any] = {"max_retries": 2, "timeout": anthropic.Timeout(REQUEST_TIMEOUT_S, connect=5.0)}
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
        if self.mode not in ("replay", "record"):
            return await self._call(role, system, content, output_model)

        # Only fixtures need the key; hashing a large upload stays off the event loop.
        key = await asyncio.to_thread(request_hash, role, system, content, output_model.__name__)
        fixture = self.fixtures_dir / f"{key}.json"
        if self.mode == "replay":
            if not fixture.exists():
                raise MissingFixtureError(f"no LLM fixture for {role}/{output_model.__name__} ({key[:12]})")
            return output_model.model_validate_json(fixture.read_text(encoding="utf-8"))

        result = await self._call(role, system, content, output_model)
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)
        fixture.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return result

    async def _call(self, role: str, system: str, content: list[dict[str, Any]], output_model: type[T]) -> T:
        import anthropic

        wire = await asyncio.to_thread(_wire_content, content) if _has_raw_bytes(content) else content
        deadline = DEADLINE_S.get(role, DEFAULT_DEADLINE_S)
        per_request: dict[str, Any] = {}
        if role in REQUEST_TIMEOUT_S_BY_ROLE:
            per_request["timeout"] = anthropic.Timeout(REQUEST_TIMEOUT_S_BY_ROLE[role], connect=5.0)
        try:
            async with asyncio.timeout(deadline):
                response = await self._api().beta.messages.parse(
                    model=MODEL,
                    max_tokens=MAX_TOKENS.get(role, DEFAULT_MAX_TOKENS),
                    system=system,
                    messages=[{"role": "user", "content": wire}],
                    output_format=output_model,
                    output_config={"effort": EFFORT.get(role, "medium")},
                    thinking={"type": "adaptive"},
                    fallbacks="default",
                    betas=[FALLBACK_BETA],
                    **per_request,
                )
        except TimeoutError as exc:
            raise LLMUnavailableError(f"no answer within {deadline:.0f}s") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailableError("rate limited") from exc
        except anthropic.APITimeoutError as exc:
            raise LLMUnavailableError("request timed out") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError("connection failed") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"API error {exc.status_code}") from exc

        _log_usage(role, response)
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
