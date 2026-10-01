"""MCP per-tool measurement — Python runtime (cross-runtime parity).

Byte-parity Python port of ``lib/boosthis-runtime-node/src/mcpMeasure.ts``.

An MCP server over HTTP is half-visible already: the ASGI/route sampler times
``POST /mcp`` as ONE opaque route, so a 40ms ``tools/list`` and a 4s tool call
are the same row. This module derives a PER-TOOL label so they can be told
apart, mirroring the label-bounding of the hosted endpoint verbatim:

  CARDINALITY + PII ARE BOUNDED ON PURPOSE. Both the JSON-RPC method and the
  tool name arrive from an UNTRUSTED caller. Methods outside a fixed protocol
  set collapse to ``mcp.other``; tool names outside the developer-registered
  roster collapse to ``mcp.call.other``. Tool ARGUMENTS are never read. An open
  label set would be a denial-of-service on our own fixed-size sample ring — a
  caller minting endless distinct names would evict every real sample.

Coverage model (honest, by transport):
  * HTTP MCP servers: AUTO. ``BoosthisTraceMiddleware`` relabels
    ``POST <any-path>/mcp`` requests per tool when the JSON-RPC body parses.
  * stdio MCP servers: EXPLICIT. No HTTP layer exists, so the developer (or
    their AI) wraps each handler in ``mcp_tool(name)`` — one decorator.

Registering a tool name is developer CODE (not caller input), so ``mcp_tool()``
self-registers its name and ``register_mcp_tools()`` pre-registers a roster for
the auto-HTTP path. Both are capped and validated.
"""

from __future__ import annotations

import asyncio
import functools
import re
import time
from typing import Any, Callable, Optional, Sequence, TypeVar

from boosthis import samples
from boosthis.pii import check_route_label
from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.score import get_duration_rating

F = TypeVar("F", bound=Callable[..., Any])

#: Fixed JSON-RPC protocol methods worth timing separately. CLOSED set.
MCP_METHODS: frozenset[str] = frozenset(
    {
        "initialize",
        "initialized",
        "ping",
        "tools/list",
        "tools/call",
        "resources/list",
        "resources/read",
        "prompts/list",
        "prompts/get",
        "notifications/initialized",
        "notifications/cancelled",
    }
)

#: Label prefix for tool-call samples; the axis + dashboard group on this.
MCP_CALL_PREFIX = "mcp.call."
#: Suffix marking a FAILED tool call (handler threw / 5xx). Closed 2x set.
MCP_ERROR_SUFFIX = ".error"

#: Roster ceiling — even developer code can't mint unbounded labels.
MCP_MAX_TOOLS = 64
#: Tool names must look like code identifiers: short, no spaces, no PII.
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_registered: set[str] = set()


def _valid_tool_name(name: Any) -> Optional[str]:
    """Validate one developer-supplied tool name. Returns the canonical name or
    None when it can't be a label (bad shape / PII-shaped / roster full)."""
    if not isinstance(name, str):
        return None
    trimmed = name.strip()
    if not _TOOL_NAME_RE.match(trimmed):
        return None
    if check_route_label(MCP_CALL_PREFIX + trimmed) is not None:
        return None
    return trimmed


def register_mcp_tools(names: Sequence[str]) -> None:
    """Pre-register the MCP tool roster (developer code — a CLOSED set) so the
    auto-HTTP path can label ``tools/call`` per tool. Invalid or over-cap names
    are silently skipped; their calls bucket to ``mcp.call.other``."""
    if not isinstance(names, (list, tuple)):
        return
    for n in names:
        if len(_registered) >= MCP_MAX_TOOLS:
            return
        ok = _valid_tool_name(n)
        if ok:
            _registered.add(ok)


def _reset_mcp_tools_for_tests() -> None:
    """@internal test hook."""
    _registered.clear()


def mcp_auto_label(msg: Any) -> Optional[str]:
    """Derive the bounded sample label for one inbound JSON-RPC message.

    Returns None when the value isn't a JSON-RPC-shaped object at all (so the
    caller can skip recording rather than mint a meaningless bucket). Batch
    arrays collapse to ``mcp.batch`` — one request, one sample.
    """
    if isinstance(msg, (list, tuple)):
        return "mcp.batch" if len(msg) > 0 else None
    if msg is None or not isinstance(msg, dict):
        return None
    method = msg.get("method")
    if not isinstance(method, str) or not method:
        return None
    if method not in MCP_METHODS:
        return "mcp.other"
    if method != "tools/call":
        return "mcp." + method.replace("/", "_")
    params = msg.get("params")
    raw = ""
    if isinstance(params, dict):
        name = params.get("name")
        if isinstance(name, str):
            raw = name
    return label_for_tool(raw)


def label_for_tool(name: str) -> str:
    """Bounded label for a tool name: registered → ``mcp.call.<name>``, anything
    else → ``mcp.call.other``. NEVER echoes unregistered caller text."""
    return (
        MCP_CALL_PREFIX + name
        if name in _registered
        else MCP_CALL_PREFIX + "other"
    )


def record_mcp_sample(label: str, duration_ms: float, is_error: bool) -> None:
    """Record one MCP sample. Failed calls land under a distinct ``.error``
    label (rated poor) so per-tool ERROR RATE is visible next to latency;
    successful calls rate on the shared TTI duration thresholds."""
    if is_boosthis_disabled():
        return
    name = label + MCP_ERROR_SUFFIX if is_error else label
    dt_ms = int(round(max(0.0, float(duration_ms))))
    rating = "poor" if is_error else get_duration_rating(dt_ms)
    # "pending" never applies here (get_duration_rating maps fast → "good");
    # buffer only stores measured ratings.
    measured = rating if rating != "pending" else "good"
    # Labels here are closed-set by construction, but keep the shared guard as
    # the final backstop (same posture as track_perf).
    if check_route_label(name) is None:
        samples.record(name, dt_ms, measured)  # type: ignore[arg-type]


def mcp_tool(name: str) -> Callable[[F], F]:
    """Explicit per-tool decorator — the ONE line a stdio MCP server needs (also
    fine over HTTP). Self-registers the name (developer code, bounded by the
    same roster cap) so the auto-HTTP path recognizes it too. A throwing handler
    records under ``<label>.error`` and re-raises unchanged. Recording failures
    are swallowed (instrumentation must never take down the host).

        @mcp_tool("fetch_weather")
        async def fetch_weather(city): ...
    """
    register_mcp_tools([name])

    def decorator(fn: F) -> F:
        label = label_for_tool(_valid_tool_name(name) or "")

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                start = time.perf_counter()
                failed = False
                try:
                    return await fn(*args, **kwargs)
                except BaseException:
                    failed = True
                    raise
                finally:
                    try:
                        record_mcp_sample(
                            label,
                            (time.perf_counter() - start) * 1000.0,
                            failed,
                        )
                    except Exception:  # noqa: BLE001
                        pass  # instrumentation must never take down the host.

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            failed = False
            try:
                return fn(*args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                try:
                    record_mcp_sample(
                        label,
                        (time.perf_counter() - start) * 1000.0,
                        failed,
                    )
                except Exception:  # noqa: BLE001
                    pass  # instrumentation must never take down the host.

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def is_mcp_path(path: str) -> bool:
    """True when a request path is an MCP endpoint (``/mcp`` or any path ending
    in ``/mcp``)."""
    return path == "/mcp" or path.endswith("/mcp")


__all__ = [
    "MCP_METHODS",
    "MCP_CALL_PREFIX",
    "MCP_ERROR_SUFFIX",
    "MCP_MAX_TOOLS",
    "register_mcp_tools",
    "mcp_auto_label",
    "label_for_tool",
    "record_mcp_sample",
    "mcp_tool",
    "is_mcp_path",
]
