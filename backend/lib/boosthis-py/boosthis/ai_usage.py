"""The sole privacy boundary for reading AI response bodies: numbers only."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Optional, Sequence

AI_USAGE_PATHS = {
    "tokensIn": (("usage", "prompt_tokens"), ("usage", "input_tokens"), ("usageMetadata", "promptTokenCount"), ("meta", "billed_units", "input_tokens")),
    "tokensOut": (("usage", "completion_tokens"), ("usage", "output_tokens"), ("usageMetadata", "candidatesTokenCount"), ("meta", "billed_units", "output_tokens")),
    "cachedIn": (("usage", "prompt_tokens_details", "cached_tokens"), ("usage", "input_tokens_details", "cached_tokens"), ("usage", "cache_read_input_tokens"), ("usageMetadata", "cachedContentTokenCount")),
    "costUsd": (("usage", "cost"), ("usage", "total_cost")),
}
AI_MODEL_PATHS = (("model",), ("modelVersion",), ("message", "model"))
AI_PRICE_AS_OF = "2026-08-01"
AI_PRICE_TABLE_DAY = (date.fromisoformat(AI_PRICE_AS_OF) - date(1970, 1, 1)).days
AI_MODEL_PRICES = (
    {"prefix": "gpt-4o-mini", "inPerM": .15, "outPerM": .6, "cachedPerM": .075},
    {"prefix": "gpt-4o", "inPerM": 2.5, "outPerM": 10, "cachedPerM": 1.25},
    {"prefix": "gpt-4.1-nano", "inPerM": .1, "outPerM": .4, "cachedPerM": .025},
    {"prefix": "gpt-4.1-mini", "inPerM": .4, "outPerM": 1.6, "cachedPerM": .1},
    {"prefix": "gpt-4.1", "inPerM": 2, "outPerM": 8, "cachedPerM": .5},
    {"prefix": "gpt-5-mini", "inPerM": .25, "outPerM": 2, "cachedPerM": .025},
    {"prefix": "gpt-5-nano", "inPerM": .05, "outPerM": .4, "cachedPerM": .005},
    {"prefix": "gpt-5", "inPerM": 1.25, "outPerM": 10, "cachedPerM": .125},
    {"prefix": "o4-mini", "inPerM": 1.1, "outPerM": 4.4, "cachedPerM": .275},
    {"prefix": "o3-mini", "inPerM": 1.1, "outPerM": 4.4, "cachedPerM": .55},
    {"prefix": "o3", "inPerM": 2, "outPerM": 8, "cachedPerM": .5},
    {"prefix": "claude-3-5-haiku", "inPerM": .8, "outPerM": 4, "cachedPerM": .08},
    {"prefix": "claude-3-5-sonnet", "inPerM": 3, "outPerM": 15, "cachedPerM": .3},
    {"prefix": "claude-haiku-4", "inPerM": 1, "outPerM": 5, "cachedPerM": .1},
    {"prefix": "claude-sonnet-4", "inPerM": 3, "outPerM": 15, "cachedPerM": .3},
    {"prefix": "claude-opus-4", "inPerM": 15, "outPerM": 75, "cachedPerM": 1.5},
    {"prefix": "gemini-2.5-pro", "inPerM": 1.25, "outPerM": 10, "cachedPerM": .31},
    {"prefix": "gemini-2.5-flash", "inPerM": .3, "outPerM": 2.5, "cachedPerM": .075},
    {"prefix": "gemini-2.0-flash", "inPerM": .1, "outPerM": .4, "cachedPerM": .025},
    {"prefix": "deepseek-chat", "inPerM": .27, "outPerM": 1.1, "cachedPerM": .07},
    {"prefix": "mistral-large", "inPerM": 2, "outPerM": 6, "cachedPerM": 2},
    {"prefix": "mistral-small", "inPerM": .2, "outPerM": .6, "cachedPerM": .2},
)

TRUNCATED_CODES = frozenset(("length", "max_tokens", "MAX_TOKENS", "model_length"))
FILTERED_CODES = frozenset(("content_filter", "refusal", "SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "RECITATION"))
QUOTA_CODES = frozenset(("insufficient_quota", "billing_hard_limit_reached", "billing_not_active", "quota_exceeded", "credit_limit_exceeded", "RESOURCE_EXHAUSTED"))
RATE_CODES = frozenset(("rate_limit_error", "rate_limit_exceeded", "requests", "tokens", "overloaded_error"))
# The answer ended ON PURPOSE. Gemini/Vertex has no terminal stream event at
# all - its last chunk simply carries finishReason STOP - so without this list
# every ordinary Gemini answer looked like a stream that died halfway.
CLEAN_FINISH_CODES = frozenset(("stop", "tool_calls", "function_call", "STOP", "end_turn", "stop_sequence", "tool_use", "COMPLETE", "STOP_SEQUENCE", "TOOL_CALL"))
OUTCOME_CODE_PATHS = (("choices", "0", "finish_reason"), ("stop_reason",), ("delta", "stop_reason"), ("candidates", "0", "finishReason"), ("promptFeedback", "blockReason"), ("error", "code"), ("error", "type"), ("error", "status"), ("type",))

AI_HEADER_NAMES = {
    "requestsRemaining": ("x-ratelimit-remaining-requests", "anthropic-ratelimit-requests-remaining"),
    "requestsLimit": ("x-ratelimit-limit-requests", "anthropic-ratelimit-requests-limit"),
    "tokensRemaining": ("x-ratelimit-remaining-tokens", "anthropic-ratelimit-tokens-remaining", "anthropic-ratelimit-input-tokens-remaining"),
    "tokensLimit": ("x-ratelimit-limit-tokens", "anthropic-ratelimit-tokens-limit", "anthropic-ratelimit-input-tokens-limit"),
    "retryAfter": ("retry-after", "x-ratelimit-reset-requests"),
    "serverMs": ("openai-processing-ms", "x-envoy-upstream-service-time"),
}
STREAM_MARKERS = ('"usage"', '"usageMetadata"', '"finish_reason"', '"stop_reason"', '"finishReason"', '"billed_units"', '"blockReason"', '"error"')
STREAM_TERMINATORS = ("[DONE]", '"message_stop"', '"response.completed"')


@dataclass(frozen=True)
class AiUsageRead:
    reported: int = 0
    tokensIn: int = 0
    tokensOut: int = 0
    cachedIn: int = 0
    reportedCostMicros: Optional[int] = None
    priceCode: int = 0


@dataclass(frozen=True)
class AiOutcomeRead:
    truncated: int = 0
    filtered: int = 0
    quota: int = 0
    rateLimited: int = 0
    # The provider NAMED how this reply ended - cleanly, at a token cap, or on
    # a filter - so the stream did not simply stop being sent.
    ended: int = 0


@dataclass(frozen=True)
class AiHeaderRead:
    requestsRemaining: Optional[float] = None
    requestsLimit: Optional[float] = None
    tokensRemaining: Optional[float] = None
    tokensLimit: Optional[float] = None
    retryAfterMs: Optional[int] = None
    serverMs: Optional[float] = None


NO_USAGE = AiUsageRead()
NO_OUTCOME = AiOutcomeRead()
NO_HEADERS = AiHeaderRead()


def _at(body: Any, path: Sequence[str], allow_string: bool = False) -> Any:
    try:
        cur = body
        for key in path:
            if isinstance(cur, list):
                cur = cur[int(key)]
            elif isinstance(cur, dict):
                cur = cur.get(key)
            else:
                return None
        if allow_string:
            return cur if isinstance(cur, str) else None
        return float(cur) if isinstance(cur, (int, float)) and not isinstance(cur, bool) and math.isfinite(cur) else None
    except Exception:  # noqa: BLE001
        return None


def _first(body: Any, paths: Sequence[Sequence[str]]) -> Optional[float]:
    for path in paths:
        value = _at(body, path)
        if value is not None:
            return value
    return None


def _price_code(body: Any) -> int:
    try:
        raw = next((_at(body, p, True) for p in AI_MODEL_PATHS if _at(body, p, True)), None)
        if not raw:
            return 0
        model = raw[:64].lower()
        best, length = 0, 0
        for i, price in enumerate(AI_MODEL_PRICES):
            prefix = price["prefix"]
            if prefix in model and len(prefix) > length:
                best, length = i + 1, len(prefix)
        return best
    except Exception:  # noqa: BLE001
        return 0


def read_ai_usage(body: Any) -> AiUsageRead:
    try:
        if not isinstance(body, dict):
            return NO_USAGE
        ti, to = _first(body, AI_USAGE_PATHS["tokensIn"]), _first(body, AI_USAGE_PATHS["tokensOut"])
        cached, usd = _first(body, AI_USAGE_PATHS["cachedIn"]), _first(body, AI_USAGE_PATHS["costUsd"])
        if ti is None and to is None and usd is None:
            return NO_USAGE
        return AiUsageRead(1, max(0, round(ti or 0)), max(0, round(to or 0)), max(0, round(cached or 0)), round(usd * 1_000_000) if usd is not None and usd >= 0 else None, _price_code(body))
    except Exception:  # noqa: BLE001
        return NO_USAGE


def estimate_cost_micros(read: AiUsageRead) -> Optional[int]:
    try:
        if read.priceCode <= 0:
            return None
        price = AI_MODEL_PRICES[read.priceCode - 1]
        inp, out, cache = price["inPerM"], price["outPerM"], price["cachedPerM"]
        cached = min(read.cachedIn, read.tokensIn)
        usd = ((read.tokensIn - cached) * inp + cached * cache + read.tokensOut * out) / 1_000_000
        return round(usd * 1_000_000) if math.isfinite(usd) and usd >= 0 else None
    except Exception:  # noqa: BLE001
        return None


def read_ai_outcome(body: Any) -> AiOutcomeRead:
    try:
        flags = [0, 0, 0, 0, 0]
        for path in OUTCOME_CODE_PATHS:
            code = _at(body, path, True)
            if not code:
                continue
            code = code[:64]
            flags[0] |= int(code in TRUNCATED_CODES); flags[1] |= int(code in FILTERED_CODES)
            flags[2] |= int(code in QUOTA_CODES); flags[3] |= int(code in RATE_CODES)
            flags[4] |= int(code in CLEAN_FINISH_CODES or code in TRUNCATED_CODES or code in FILTERED_CODES)
        return AiOutcomeRead(*flags)
    except Exception:  # noqa: BLE001
        return NO_OUTCOME


_DURATION_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")
def _duration(raw: str) -> Optional[int]:
    s = raw.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return round(float(s) * 1000)
    m = _DURATION_RE.fullmatch(s)
    if not m or not any(m.groups()):
        return None
    vals = [float(x or 0) for x in m.groups()]
    return round(vals[0] * 3600000 + vals[1] * 60000 + vals[2] * 1000 + vals[3])


def read_ai_headers(get: Callable[[str], Optional[str]]) -> AiHeaderRead:
    def num(names: Sequence[str]) -> Optional[float]:
        for name in names:
            try:
                raw = get(name)
                if isinstance(raw, str) and raw.strip():
                    value = float(raw.strip())
                    if math.isfinite(value) and value >= 0:
                        return value
            except Exception:  # noqa: BLE001
                pass
        return None
    def dur(names: Sequence[str]) -> Optional[int]:
        for name in names:
            try:
                raw = get(name)
                value = _duration(raw) if isinstance(raw, str) else None
                if value is not None and value >= 0:
                    return value
            except Exception:  # noqa: BLE001
                pass
        return None
    return AiHeaderRead(num(AI_HEADER_NAMES["requestsRemaining"]), num(AI_HEADER_NAMES["requestsLimit"]), num(AI_HEADER_NAMES["tokensRemaining"]), num(AI_HEADER_NAMES["tokensLimit"]), dur(AI_HEADER_NAMES["retryAfter"]), num(AI_HEADER_NAMES["serverMs"]))


def stream_line_is_interesting(line: str) -> bool:
    return isinstance(line, str) and any(x in line for x in STREAM_MARKERS)


def stream_line_is_terminal(line: str) -> bool:
    return isinstance(line, str) and any(x in line for x in STREAM_TERMINATORS)