"""Privacy-safe AI call collector, fed by the existing httpx transport hook."""
from __future__ import annotations

import json
import math
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .ai_providers import DECLARED_PROVIDER_CODE, ai_provider_code_or_declared
from .ai_usage import (
    AI_PRICE_TABLE_DAY,
    AiUsageRead,
    estimate_cost_micros,
    read_ai_headers,
    read_ai_outcome,
    read_ai_usage,
    stream_line_is_interesting,
    stream_line_is_terminal,
)
from .runtime_flags import is_boosthis_disabled

AI_STREAM_STALL_MS = 10_000
AI_CACHEABLE_INPUT_TOKENS = 2048
AI_SERIAL_THRESHOLD = 2
MAX_DISTINCT_PROMPTS = 128
MAX_PROMPT_CHARS = 4096
DURATION_RING = 256
MAX_STREAM_CARRY = 8192
MAX_ERROR_BODY_CHARS = 8192
UNWATCHABLE_HTTP_CLIENTS = ("requests", "urllib3", "aiohttp", "http.client")
# Paths an OpenAI-compatible inference server answers on.
#
# WHAT THIS LIST IS FOR. An app can call a model that is not on the maintained
# provider list and was never declared: the team's own gateway, a regional
# endpoint, a sovereign host. The classifier reads the HOSTNAME, so it says
# nothing about any of them, and without this list the app would look like an
# app that calls no AI at all. A POST to one of these paths is grounds for
# saying "there is AI-shaped traffic here I could not classify", and for
# nothing else.
#
# WHAT IT MUST NOT DO. A matched call is never timed, never scored, never
# counted as an AI call and never priced -- the kit does not know whose model
# it is. It increments one number. The list is the Node kit's, kept short and
# closed for the same reason: a shape unrelated services also serve would turn
# an honest blind-spot note into a false alarm.
AI_SHAPED_PATHS = (
    # OpenAI-compatible -- the shape almost every self-hosted server exposes.
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/responses",
    "/v1/embeddings",
    # Anthropic-compatible.
    "/v1/messages",
    # Ollama's native API.
    "/api/generate",
    "/api/chat",
    "/api/embeddings",
    # Hugging Face text-generation-inference.
    "/generate_stream",
)


@dataclass
class AiRequestTally:
    prompts: Dict[int, int] = field(default_factory=dict)
    calls: int = 0
    ms: float = 0.0
    serialRun: int = 0
    runLength: int = 0
    lastEndAt: float = -1.0
    costMicros: int = 0


_current_ai: ContextVar[Optional[AiRequestTally]] = ContextVar("boosthis_ai_work", default=None)
_lock = threading.RLock()
_armed = False
_feed_on = False
_window_started = 0.0
_totals: Dict[str, float] = {}
_providers: Dict[int, int] = {}
_durations: list[float] = []
_ttfts: list[float] = []
_last_refusal: Dict[int, tuple[float, float]] = {}


def _fresh_totals() -> Dict[str, float]:
    return {k: 0 for k in (
        "watchedRequests", "watchedRequestMs", "aiMs", "callCount", "streamCount",
        "stallCount", "failCount", "rateLimitedCount", "quotaCount", "timeoutCount",
        "truncatedCount", "filteredCount", "noTimeLimitCalls", "retryNoBackoffCount",
        "tokensIn", "tokensOut", "cachedIn", "costMicros", "reportedCostCalls",
        "pricedCalls", "unpricedCalls", "declaredCalls", "declaredUnpricedCalls",
        "worstRequestCostMicros", "usageMissingCalls",
        "streamUsageMissingCalls", "promptRepeatWorst", "promptRepeatRequests",
        "serialWorst", "serialRequests", "headroomReads", "worstRetryAfterMs",
        "serverMsTotal", "serverMsCalls", "unclassifiedCalls",
    )} | {"worstRequestsPct": math.nan, "worstTokensPct": math.nan}


_totals.update(_fresh_totals())


def arm_ai_calls() -> None:
    global _armed, _feed_on, _window_started
    if is_boosthis_disabled():
        return
    try:
        _armed = True
        _feed_on = True
        if not _window_started:
            _window_started = time.time() * 1000
    except Exception:  # noqa: BLE001
        pass


def unarm_ai_calls() -> None:
    global _armed, _feed_on
    _feed_on = False
    _armed = False


def begin_ai_work() -> Any:
    if is_boosthis_disabled():
        return None
    try:
        scope = AiRequestTally()
        return scope, _current_ai.set(scope)
    except Exception:  # noqa: BLE001
        return None


def end_ai_work(scope: Any, request_ms: float) -> None:
    """Close a begin_ai_work handle and fold requests which actually called AI."""
    if not scope:
        return
    try:
        tally, token = scope
        try:
            _current_ai.reset(token)
        except Exception:  # noqa: BLE001
            pass
        if not isinstance(tally, AiRequestTally) or tally.calls == 0:
            return
        wall = max(0.0, float(request_ms or 0))
        with _lock:
            _totals["watchedRequests"] += 1
            _totals["watchedRequestMs"] += wall
            _totals["aiMs"] += min(tally.ms, wall) if wall > 0 else tally.ms
            worst = max(tally.prompts.values(), default=0)
            _totals["promptRepeatWorst"] = max(_totals["promptRepeatWorst"], worst)
            if worst > 1:
                _totals["promptRepeatRequests"] += 1
            _totals["serialWorst"] = max(_totals["serialWorst"], tally.serialRun)
            if tally.serialRun >= AI_SERIAL_THRESHOLD:
                _totals["serialRequests"] += 1
            _totals["worstRequestCostMicros"] = max(_totals["worstRequestCostMicros"], tally.costMicros)
    except Exception:  # noqa: BLE001
        pass


def _prompt_identity(body: Any) -> int:
    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8", "ignore")
        if not isinstance(body, str) or not body:
            return 0
        head = body[:MAX_PROMPT_CHARS]
        # FNV-1a, local grouping key only.
        h = 0x811C9DC5
        for char in f"{len(body)}:{head}":
            h ^= ord(char)
            h = (h * 0x01000193) & 0xFFFFFFFF
        return h or 1
    except Exception:  # noqa: BLE001
        return 0


def _push(ring: list[float], value: float) -> None:
    ring.append(max(0.0, value))
    if len(ring) > DURATION_RING:
        del ring[0]


def _merge_usage(old: Optional[AiUsageRead], new: AiUsageRead) -> Optional[AiUsageRead]:
    if not new.reported:
        return old
    if old is None:
        return new
    return AiUsageRead(1, max(old.tokensIn, new.tokensIn), max(old.tokensOut, new.tokensOut),
                       max(old.cachedIn, new.cachedIn), new.reportedCostMicros if new.reportedCostMicros is not None else old.reportedCostMicros,
                       new.priceCode or old.priceCode)


class AiCallObserver:
    """One httpx call. It retains numbers and bounded framing bytes only."""
    def __init__(self, provider: int, request: Any) -> None:
        self.provider = provider
        self.scope = _current_ai.get()
        self.started = time.monotonic() * 1000
        self.first = 0.0
        self.last = 0.0
        self.worst_gap = 0.0
        self.prompt_id = _prompt_identity(getattr(request, "content", None))
        extensions = getattr(request, "extensions", {}) or {}
        timeout = extensions.get("timeout") if isinstance(extensions, dict) else None
        self.had_time_limit = isinstance(timeout, dict) and any(v is not None for v in timeout.values())
        self.streamed = False
        self.terminal = False
        self.usage: Optional[AiUsageRead] = None
        self.filed = False
        self.carry = ""
        self.body = bytearray()
        self.status = 0

    def headers(self, response: Any) -> None:
        try:
            self.status = int(getattr(response, "status_code", 0) or 0)
            headers = getattr(response, "headers", None)
            get = (lambda name: headers.get(name)) if headers is not None else (lambda _name: None)
            read = read_ai_headers(get)
            saw = False
            with _lock:
                for remain, limit, key in (
                    (read.requestsRemaining, read.requestsLimit, "worstRequestsPct"),
                    (read.tokensRemaining, read.tokensLimit, "worstTokensPct"),
                ):
                    if remain is not None and limit:
                        pct = max(0.0, min(100.0, remain / limit * 100))
                        _totals[key] = pct if math.isnan(_totals[key]) else min(_totals[key], pct)
                        saw = True
                if read.retryAfterMs is not None:
                    _totals["worstRetryAfterMs"] = max(_totals["worstRetryAfterMs"], read.retryAfterMs)
                    saw = True
                if saw:
                    _totals["headroomReads"] += 1
                if read.serverMs is not None:
                    _totals["serverMsTotal"] += read.serverMs
                    _totals["serverMsCalls"] += 1
                if self.status == 429:
                    prev = _last_refusal.get(self.provider)
                    if prev and self.started - prev[0] < prev[1]:
                        _totals["retryNoBackoffCount"] += 1
                    _last_refusal[self.provider] = (time.monotonic() * 1000, float(read.retryAfterMs or 1000))
            ctype = str(get("content-type") or "").lower()
            self.streamed = "event-stream" in ctype or "x-ndjson" in ctype
        except Exception:  # noqa: BLE001
            pass

    def chunk(self, chunk: Any) -> None:
        try:
            at = time.monotonic() * 1000
            if not self.first:
                self.first = at
            elif self.last:
                self.worst_gap = max(self.worst_gap, at - self.last)
            self.last = at
            raw = bytes(chunk)
            if not self.streamed:
                if len(self.body) + len(raw) <= MAX_ERROR_BODY_CHARS:
                    self.body.extend(raw)
                else:
                    self.body.clear()
                return
            self.carry += raw.decode("utf-8", "ignore")
            if len(self.carry) > MAX_STREAM_CARRY and "\n" not in self.carry:
                self.carry = ""
            while "\n" in self.carry:
                line, self.carry = self.carry.split("\n", 1)
                self._line(line)
        except Exception:  # noqa: BLE001
            pass

    def _line(self, line: str) -> None:
        if stream_line_is_terminal(line):
            self.terminal = True
        if not stream_line_is_interesting(line):
            return
        brace = line.find("{")
        if brace < 0:
            return
        try:
            self._body(json.loads(line[brace:]))
        except Exception:  # noqa: BLE001
            pass

    def _body(self, parsed: Any) -> None:
        try:
            self.usage = _merge_usage(self.usage, read_ai_usage(parsed))
            outcome = read_ai_outcome(parsed)
            # The provider said how this answer ended. Some providers
            # (Gemini/Vertex) have no terminal stream event and say it only
            # here, so a reply that names its ending is a finished reply - and
            # one already counted as cut off must not be counted again at EOF.
            if outcome.ended:
                self.terminal = True
            with _lock:
                _totals["truncatedCount"] += outcome.truncated
                _totals["filteredCount"] += outcome.filtered
                if self.status >= 400:
                    _totals["quotaCount"] += outcome.quota
                    if self.status != 429:
                        _totals["rateLimitedCount"] += outcome.rateLimited
        except Exception:  # noqa: BLE001
            pass

    def finish(self, exc: Optional[BaseException] = None) -> None:
        if self.filed:
            return
        self.filed = True
        try:
            if not self.streamed and self.body:
                try:
                    self._body(json.loads(bytes(self.body).decode("utf-8")))
                except Exception:  # noqa: BLE001
                    pass
            _file_observer(self, exc)
        except Exception:  # noqa: BLE001
            pass
        finally:
            # Response framing bytes are observation scratch space, never
            # collector state. Drop them even when parsing/filing failed.
            self.body.clear()
            self.carry = ""


def _note_unclassified_ai_shape(request: Any) -> None:
    """Tally an unclassified call whose path looked like an inference request.

    Count only -- see AI_SHAPED_PATHS. The evidence is a POST to an inference
    shape: a GET or a HEAD on the same path is a health check or a probe, and
    counting one would turn an honest blind-spot note into a false alarm on an
    app with no AI in it at all.
    """
    try:
        method = str(getattr(request, "method", "") or "").upper()
        if method != "POST":
            return
        path = str(getattr(getattr(request, "url", None), "path", "") or "")
        path = path.lower().rstrip("/")
        if not path or path not in AI_SHAPED_PATHS:
            return
        with _lock:
            _totals["unclassifiedCalls"] += 1
    except Exception:  # noqa: BLE001
        pass


def observer_for_httpx(request: Any) -> Optional[AiCallObserver]:
    """Create an observer for a classified httpx request, otherwise None."""
    if not _feed_on or is_boosthis_disabled():
        return None
    try:
        host = getattr(getattr(request, "url", None), "host", None)
        provider = ai_provider_code_or_declared(host)
        if not provider:
            # Not a provider we know and not one this app declared. Before the
            # call passes out of sight, ask the one question that costs
            # nothing: did it LOOK like inference? That answer is the
            # difference between an app with no AI and an app whose AI we
            # cannot see.
            _note_unclassified_ai_shape(request)
            return None
        return AiCallObserver(provider, request)
    except Exception:  # noqa: BLE001
        return None


def _file_observer(ctx: AiCallObserver, exc: Optional[BaseException]) -> None:
    end = time.monotonic() * 1000
    elapsed = max(0.0, end - ctx.started)
    with _lock:
        _totals["callCount"] += 1
        declared = ctx.provider == DECLARED_PROVIDER_CODE
        if declared:
            _totals["declaredCalls"] += 1
        _providers[ctx.provider] = _providers.get(ctx.provider, 0) + 1
        _push(_durations, elapsed)
        if not ctx.had_time_limit:
            _totals["noTimeLimitCalls"] += 1
        if exc is not None or ctx.status >= 400:
            _totals["failCount"] += 1
            if "timeout" in (type(exc).__name__ if exc else "").lower():
                _totals["timeoutCount"] += 1
        if ctx.status == 429:
            _totals["rateLimitedCount"] += 1
        if ctx.status == 402:
            _totals["quotaCount"] += 1
        if ctx.streamed:
            _totals["streamCount"] += 1
            if ctx.first:
                _push(_ttfts, ctx.first - ctx.started)
            # The quiet BEFORE the end counts. A gap is otherwise only noticed
            # when a next chunk arrives, so the commonest stall of all — an
            # answer that goes silent half-way and then just stops — would be
            # filed as a clean, if slow, stream. Matches the Node kit.
            if ctx.last:
                ctx.worst_gap = max(ctx.worst_gap, end - ctx.last)
            if ctx.worst_gap >= AI_STREAM_STALL_MS:
                _totals["stallCount"] += 1
            if not ctx.terminal:
                _totals["truncatedCount"] += 1
            if not ctx.usage or not ctx.usage.reported:
                _totals["streamUsageMissingCalls"] += 1
        if not ctx.usage or not ctx.usage.reported:
            _totals["usageMissingCalls"] += 1
            _totals["unpricedCalls"] += 1
        else:
            usage = ctx.usage
            _totals["tokensIn"] += usage.tokensIn; _totals["tokensOut"] += usage.tokensOut; _totals["cachedIn"] += usage.cachedIn
            micros = usage.reportedCostMicros
            if micros is not None:
                _totals["reportedCostCalls"] += 1; _totals["pricedCalls"] += 1
            elif declared:
                # Tokens are facts reported by this endpoint. Public list
                # pricing is not: a private gateway may return a familiar model
                # name without sharing that provider's published price.
                _totals["unpricedCalls"] += 1
                _totals["declaredUnpricedCalls"] += 1
            else:
                micros = estimate_cost_micros(usage)
                if micros is None:
                    _totals["unpricedCalls"] += 1
                else:
                    _totals["pricedCalls"] += 1
            if micros is not None and micros > 0:
                _totals["costMicros"] += micros
                if ctx.scope:
                    ctx.scope.costMicros += micros
        tally = ctx.scope
        if tally is not None:
            tally.calls += 1; tally.ms += elapsed
            if ctx.prompt_id and (ctx.prompt_id in tally.prompts or len(tally.prompts) < MAX_DISTINCT_PROMPTS):
                tally.prompts[ctx.prompt_id] = tally.prompts.get(ctx.prompt_id, 0) + 1
            tally.runLength = max(2, tally.runLength + 1) if tally.lastEndAt >= 0 and ctx.started >= tally.lastEndAt else 1
            tally.serialRun = max(tally.serialRun, tally.runLength)
            tally.lastEndAt = end


def unwatched_ai_client_count(
    presence_probe: Optional[Callable[[str], bool]] = None,
) -> int:
    """Count loaded HTTP client families outside this kit's httpx watch.

    ``presence_probe`` is a deterministic test seam. It receives client-family
    names only inside this process; the returned count is the sole value that
    may leave.
    """
    if not _armed or is_boosthis_disabled():
        return 0
    try:
        probe = presence_probe or (lambda name: name in sys.modules)
        return max(0, sum(1 for name in UNWATCHABLE_HTTP_CLIENTS if probe(name)))
    except Exception:  # noqa: BLE001
        return 0


def _percentile(values: list[float], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(pct / 100 * len(ordered)) - 1))])


def get_ai_call_stats(
    presence_probe: Optional[Callable[[str], bool]] = None,
) -> Dict[str, Any]:
    """Plain numeric aggregate with the exact scorer field names."""
    with _lock:
        top = max(_providers, key=_providers.get) if _providers else 0
        t = dict(_totals)
        stats = {
            "watchedRequests": int(t["watchedRequests"]), "watchedRequestMs": t["watchedRequestMs"], "aiMs": t["aiMs"],
            "callCount": int(t["callCount"]), "providerCount": len(_providers), "topProvider": top,
            "p75Ms": _percentile(_durations, 75), "worstMs": round(max(_durations, default=0)),
            "streamCount": int(t["streamCount"]), "ttftP75Ms": _percentile(_ttfts, 75),
            "stallCount": int(t["stallCount"]), "failCount": int(t["failCount"]),
            "rateLimitedCount": int(t["rateLimitedCount"]), "quotaCount": int(t["quotaCount"]),
            "timeoutCount": int(t["timeoutCount"]), "truncatedCount": int(t["truncatedCount"]),
            "filteredCount": int(t["filteredCount"]),
            "serverMsP75": round(t["serverMsTotal"] / t["serverMsCalls"]) if t["serverMsCalls"] else 0,
            "serverMsCalls": int(t["serverMsCalls"]),
            "unclassifiedCalls": int(t["unclassifiedCalls"]),
            "tokensIn": int(t["tokensIn"]), "tokensOut": int(t["tokensOut"]), "cachedIn": int(t["cachedIn"]),
            "costMicros": int(t["costMicros"]), "reportedCostCalls": int(t["reportedCostCalls"]),
            "pricedCalls": int(t["pricedCalls"]), "unpricedCalls": int(t["unpricedCalls"]),
            "declaredCalls": int(t["declaredCalls"]),
            "declaredUnpricedCalls": int(t["declaredUnpricedCalls"]),
            "worstRequestCostMicros": int(t["worstRequestCostMicros"]),
            "windowMs": max(0, round(time.time() * 1000 - _window_started)) if _window_started else 0,
            "usageMissingCalls": int(t["usageMissingCalls"]), "streamUsageMissingCalls": int(t["streamUsageMissingCalls"]),
            "promptRepeatWorst": int(t["promptRepeatWorst"]), "promptRepeatRequests": int(t["promptRepeatRequests"]),
            "serialWorst": int(t["serialWorst"]), "serialRequests": int(t["serialRequests"]),
            "noTimeLimitCalls": int(t["noTimeLimitCalls"]), "retryNoBackoffCount": int(t["retryNoBackoffCount"]),
            "headroomReads": int(t["headroomReads"]),
            "worstRequestsPct": None if math.isnan(t["worstRequestsPct"]) else t["worstRequestsPct"],
            "worstTokensPct": None if math.isnan(t["worstTokensPct"]) else t["worstTokensPct"],
            "worstRetryAfterMs": int(t["worstRetryAfterMs"]),
        }
        # Absence means observation never looked. Once armed, zero is a real
        # coverage claim and must ride every emitted aiCalls shape.
        if _armed and not is_boosthis_disabled():
            stats["unwatchedClients"] = unwatched_ai_client_count(presence_probe)
        return stats


def collect_ai_findings() -> list[Dict[str, Any]]:
    s = get_ai_call_stats()
    findings = []
    def add(kind: str, name: str, p95: float, count: int, hint: str) -> None:
        if count > 0:
            findings.append({"kind": kind, "name": name, "p95": p95, "count": count, "hint": hint})
    add("ai-no-timeout", "AI calls without a timeout", s["p75Ms"], s["noTimeLimitCalls"], "Put a finite timeout on every AI call.")
    add("ai-retry-no-backoff", "AI retries ignored retry-after", s["worstRetryAfterMs"], s["retryNoBackoffCount"], "Wait at least as long as the provider requested.")
    add("ai-serial-calls", "AI calls ran serially", s["p75Ms"], s["serialRequests"], "Run independent AI calls concurrently.")
    add("ai-duplicate-prompt", "AI prompts were duplicated", s["promptRepeatWorst"], s["promptRepeatRequests"], "Reuse an identical prompt result inside one request.")
    # Averaged over the calls that ACTUALLY REPORTED input tokens, never over
    # every call: a refused call carries no prompt, so counting it shrinks the
    # apparent prompt size and hides the finding from the app that most needs
    # it. Matches the Node kit exactly.
    usage_calls = max(0, s["callCount"] - s["usageMissingCalls"])
    # The count is how many calls the average was taken over and the reading is
    # that average — the same two numbers, in the same two places, as every
    # other finding here and as the Node kit's own ai-cache-cold.
    cold_calls = (
        usage_calls
        if usage_calls >= 2
        and s["cachedIn"] == 0
        and s["tokensIn"] >= AI_CACHEABLE_INPUT_TOKENS * usage_calls
        else 0
    )
    add("ai-cache-cold", "AI prompt cache stayed cold",
        round(s["tokensIn"] / usage_calls) if usage_calls else 0,
        cold_calls, "Enable provider prompt caching for large stable prefixes.")
    add("ai-stream-usage-missing", "AI stream usage was missing", 0, s["streamUsageMissingCalls"], "Request the provider's final stream usage event.")
    return findings


def clear_ai_calls() -> None:
    global _window_started
    unarm_ai_calls()
    with _lock:
        _totals.clear(); _totals.update(_fresh_totals())
        _providers.clear(); _durations.clear(); _ttfts.clear(); _last_refusal.clear()
        _window_started = 0


__all__ = ["arm_ai_calls", "unarm_ai_calls", "begin_ai_work", "end_ai_work", "get_ai_call_stats",
           "clear_ai_calls", "unwatched_ai_client_count", "observer_for_httpx", "collect_ai_findings",
           "AI_STREAM_STALL_MS", "AI_CACHEABLE_INPUT_TOKENS"]