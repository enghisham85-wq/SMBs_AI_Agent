"""The single path to the model.

Modes (LLM_MODE):
- live:    call the API.
- record:  call the API and save the parsed output as a fixture keyed by request hash.
- replay:  read the fixture; raise MissingFixtureError if absent (tests, offline demo).
- offline: never call the API; use the caller's deterministic `offline` fallback. Clearly a
           stand-in for development and demos without an API key.

Providers (LLM_PROVIDER):
- anthropic: model `claude-opus-5-5` through the Anthropic SDK, with structured output (Pydantic), an
  effort level per role and server-side refusal fallback. Callers mark their stable prefix (a document,
  the chart of accounts) with `cache=True`; a top-level marker would land on the unique per-request
  text and never be read back.
- openai_compatible: an OpenAI-style chat-completions gateway at LLM_BASE_URL (model LLM_MODEL). The
  same blocks are sent as text/image/file parts in JSON mode, with the output schema in the system
  prompt; the answer is validated against the Pydantic model and re-asked once if it doesn't fit. Cache markers and refusal
  fallback have no equivalent there, and the gateway may ignore the effort level.

Each call has a deadline; a timeout counts as the model being unavailable.
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


def active_model() -> str:
    """The model id sent to the provider: LLM_MODEL when set, otherwise the Anthropic default."""
    return get_settings().LLM_MODEL or MODEL


def request_hash(role: str, system: str, blocks: list[dict[str, Any]], schema_name: str) -> str:
    raw = json.dumps(
        # The model and effort change the answer, so a recorded fixture only replays for the same pair.
        {"model": active_model(), "effort": EFFORT.get(role, "medium"), "role": role, "system": system,
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


def _openai_parts(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-style blocks (files already base64) as chat-completions content parts."""
    parts: list[dict[str, Any]] = []
    for b in blocks:
        src = b.get("source") or {}
        if b["type"] == "text":
            parts.append({"type": "text", "text": b["text"]})
        elif b["type"] == "image":
            parts.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
        elif b["type"] == "document":
            parts.append({"type": "file", "file": {"filename": "document.pdf",
                                                    "file_data": f"data:{src['media_type']};base64,{src['data']}"}})
        else:
            raise ValueError(f"unsupported content block {b['type']!r}")
    return parts


def _json_text(text: str) -> str:
    """The JSON in a reply, without a markdown fence some gateways wrap it in."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _log_chat_usage(role: str, response: Any) -> None:
    usage = getattr(response, "usage", None)
    if usage is not None:
        log.info("llm %s: input=%s output=%s", role, getattr(usage, "prompt_tokens", None),
                 getattr(usage, "completion_tokens", None))


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
            settings = get_settings()
            if settings.LLM_PROVIDER == "openai_compatible":
                import openai

                if not settings.LLM_BASE_URL or not settings.LLM_API_KEY or not settings.LLM_MODEL:
                    raise LLMUnavailableError("LLM_PROVIDER=openai_compatible needs LLM_BASE_URL, LLM_API_KEY and LLM_MODEL")
                self._client = openai.AsyncOpenAI(base_url=settings.LLM_BASE_URL, api_key=settings.LLM_API_KEY,
                                                  timeout=REQUEST_TIMEOUT_S, max_retries=2)
            else:
                import anthropic

                kwargs: dict[str, Any] = {"max_retries": 2,
                                          "timeout": anthropic.Timeout(REQUEST_TIMEOUT_S, connect=5.0)}
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
        wire = await asyncio.to_thread(_wire_content, content) if _has_raw_bytes(content) else content
        deadline = DEADLINE_S.get(role, DEFAULT_DEADLINE_S)
        if get_settings().LLM_PROVIDER == "openai_compatible":
            return await self._call_openai_compatible(role, system, wire, output_model, deadline)
        return await self._call_anthropic(role, system, wire, output_model, deadline)

    async def _call_openai_compatible(self, role: str, system: str, wire: list[dict[str, Any]],
                                      output_model: type[T], deadline: float) -> T:
        import openai
        from pydantic import ValidationError

        api = self._api()  # raises LLMUnavailableError when the gateway settings are incomplete
        # The schema goes in the instructions with plain JSON mode: CodeCraft's json_schema mode fails (502) on
        # schemas with anyOf/patterns, which most of ours have. The Pydantic check below enforces it instead.
        schema = json.dumps(output_model.model_json_schema(), ensure_ascii=False)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{system}\n\nReply with only a JSON object that matches this JSON schema:\n{schema}"},
            {"role": "user", "content": _openai_parts(wire)},
        ]
        response_format = {"type": "json_object"}
        timeout = REQUEST_TIMEOUT_S_BY_ROLE.get(role, REQUEST_TIMEOUT_S)
        try:
            async with asyncio.timeout(deadline):
                for attempt in range(2):
                    response = await api.chat.completions.create(
                        model=active_model(),
                        max_tokens=MAX_TOKENS.get(role, DEFAULT_MAX_TOKENS),
                        messages=messages,
                        response_format=response_format,
                        extra_body={"reasoning": {"effort": EFFORT.get(role, "medium")}},
                        timeout=timeout,
                    )
                    _log_chat_usage(role, response)
                    choice = response.choices[0]
                    if choice.finish_reason == "content_filter" or getattr(choice.message, "refusal", None):
                        raise LLMRefusalError(str(getattr(choice.message, "refusal", "") or "content filtered"))
                    text = choice.message.content or ""
                    try:
                        return output_model.model_validate_json(_json_text(text))
                    except ValidationError as exc:
                        if attempt:
                            raise LLMUnavailableError(f"answer did not match {output_model.__name__}") from exc
                        # No server-side schema guarantee through a gateway: show the model its mistake once.
                        messages += [{"role": "assistant", "content": text},
                                     {"role": "user", "content": "That reply did not match the required JSON schema "
                                      f"({exc.error_count()} errors: {str(exc)[:1500]}). Reply again with only the "
                                      "JSON object."}]
        except TimeoutError as exc:
            raise LLMUnavailableError(f"no answer within {deadline:.0f}s") from exc
        except openai.RateLimitError as exc:
            raise LLMUnavailableError("rate limited") from exc
        except openai.APITimeoutError as exc:
            raise LLMUnavailableError("request timed out") from exc
        except openai.APIConnectionError as exc:
            raise LLMUnavailableError("connection failed") from exc
        except openai.APIStatusError as exc:
            raise LLMUnavailableError(f"API error {exc.status_code}") from exc
        raise LLMUnavailableError("no answer")  # unreachable: the loop returns or raises

    async def _call_anthropic(self, role: str, system: str, wire: list[dict[str, Any]],
                              output_model: type[T], deadline: float) -> T:
        import anthropic

        per_request: dict[str, Any] = {}
        if role in REQUEST_TIMEOUT_S_BY_ROLE:
            per_request["timeout"] = anthropic.Timeout(REQUEST_TIMEOUT_S_BY_ROLE[role], connect=5.0)
        try:
            async with asyncio.timeout(deadline):
                response = await self._api().beta.messages.parse(
                    model=active_model(),
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
