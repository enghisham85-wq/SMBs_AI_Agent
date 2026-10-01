"""Learning-loop WRITE client (dev-only, runs alongside the stdio MCP server).

This is the OUT side of Boosthis's "learn from experience" loop. When the
developer's own AI reports a slow pattern that no shipped rule covers
(``boosthis.report_unmatched_pattern``), this module forwards a privacy-safe
PROPOSAL to the maintainer's review queue (``POST /rules/propose``). The
maintainer approves or rejects it by hand — nothing here ever ships a rule.

TWO privacy tiers, decided by the SERVER:
  - TIER A (always): a demand signal only — runtime, a coarse category,
    severity + timing buckets, a NON-reversible sha256 of the pattern, and an
    occurrence count. No free text, no code, no identifiers.
  - TIER B (only when the app proves it has connected its AI): additionally
    the raw pattern text and any AI-drafted rule (id/title/when_to_apply/fix).
    Proof is the install's read/web-read/delete token in the
    ``X-Boosthis-Install-*`` headers; the project key alone never unlocks it.

Every guarantee of the rest of the runtime holds here: the payload rides the
shared PII guard chokepoint (``_safe_transmit_internal``), ``BOOSTHIS_DISABLED``
silences it (the chokepoint returns a synthetic 204), and it is entirely
fail-open — any error is swallowed so a best-effort proposal can never break the
tool the developer's AI called.

Sibling of ``community.py`` (the READ side of the same loop); the two share the
same env-driven endpoint/credential resolution but never the same direction.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Literal, Optional

from boosthis.pii import check_no_pii
from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.thresholds import SCORE_THRESHOLDS
from boosthis.transmit import _safe_transmit_internal

ProposalRuntime = Literal["rn", "node", "py"]

_CATEGORIES: frozenset[str] = frozenset(
    {
        "startup",
        "navigation",
        "interaction",
        "rendering",
        "network",
        "data",
        "memory",
        "other",
    }
)

_RULE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _endpoint_base() -> str:
    # ``or`` (not a default) so a blank env var never shadows the fallback.
    raw = (
        os.environ.get("BOOSTHIS_ENDPOINT")
        or os.environ.get("BOOSTEN_ENDPOINT")
        or "https://www.boosthis.com/api"
    )
    return raw.rstrip("/")


def _invite_key() -> Optional[str]:
    k = (os.environ.get("BOOSTHIS_INVITE_KEY") or os.environ.get("BOOSTEN_INVITE_KEY") or "").strip()
    return k or None


def _install_creds() -> Optional[tuple[str, str]]:
    """Read/web-read/delete token that proves control of a connected install.

    Same env vars the stdio server uses to wire live-data reads.
    """
    install_id = (
        os.environ.get("BOOSTHIS_INSTALL_ID") or os.environ.get("BOOSTEN_INSTALL_ID") or ""
    ).strip()
    token = (
        os.environ.get("BOOSTHIS_READ_TOKEN") or os.environ.get("BOOSTEN_READ_TOKEN") or ""
    ).strip()
    if not install_id or not token:
        return None
    return (install_id, token)


def runtime_from_language(language: str) -> ProposalRuntime:
    """Map the tool's ``language`` enum to the wire ``runtime`` enum."""
    if language == "node":
        return "node"
    if language == "react-native":
        return "rn"
    # Default to this runtime (Python), matching each runtime's own default.
    return "py"


def pattern_signature(pattern: str) -> str:
    """Non-reversible dedup + distinct-project counting key: sha256 of the
    whitespace-normalized, lower-cased pattern. The raw pattern is NEVER part of
    the tier-A signal — only this hash is.
    """
    norm = re.sub(r"\s+", " ", pattern.lower()).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def timing_bucket_for(observed_ms: Optional[float]) -> Literal["good", "warn", "poor"]:
    """Bucket the observed latency against the shared TTI thresholds. Absent or
    non-finite input degrades to "warn" (an unmatched pattern is, by nature, a
    suspected problem).
    """
    if not isinstance(observed_ms, (int, float)) or isinstance(observed_ms, bool):
        return "warn"
    try:
        val = float(observed_ms)
    except (TypeError, ValueError):
        return "warn"
    if val != val or val in (float("inf"), float("-inf")):  # NaN / inf
        return "warn"
    tti = SCORE_THRESHOLDS["tti"]
    if val <= tti["good"]:
        return "good"
    if val >= tti["poor"]:
        return "poor"
    return "warn"


def _severity_for(timing: str) -> Literal["low", "med", "high"]:
    if timing == "poor":
        return "high"
    if timing == "warn":
        return "med"
    return "low"


def _normalize_category(category: Optional[str]) -> str:
    return category if category in _CATEGORIES else "other"


def _clean_text(value: Optional[str], max_len: int) -> Optional[str]:
    """Include a tier-B free-text field ONLY if it is present and passes the
    shared PII guard. Dropping a PII-tripping field (rather than aborting) keeps
    the tier-A demand signal alive while never sending PII-shaped free text — the
    server re-runs the same guard as the authority.
    """
    if not isinstance(value, str):
        return None
    trimmed = value[:max_len]
    if not trimmed:
        return None
    return trimmed if check_no_pii(trimmed) is None else None


def report_unmatched_pattern(
    *,
    pattern: str,
    language: str,
    observed_ms: Optional[float] = None,
    category: Optional[str] = None,
    draft: Optional[dict[str, Any]] = None,
) -> None:
    """Fire-and-forget a rule PROPOSAL to the maintainer's review queue.

    Fully fail-open: returns silently (never raises) when there is no invite
    key, when ``BOOSTHIS_DISABLED`` is set, or on any network/PII/transport
    error. The caller (``report_unmatched_pattern`` tool) treats it as
    best-effort and never blocks the tool result on its outcome.
    """
    try:
        key = _invite_key()
        # Gate: a proposal requires an invite key (the distributable `fix`
        # scope). Without one, this is an unregistered install — stay on-device.
        if not key:
            return
        # Defensive kill-switch check; _safe_transmit_internal also honors it.
        if is_boosthis_disabled():
            return

        timing_bucket = timing_bucket_for(observed_ms)
        body: dict[str, Any] = {
            "runtime": runtime_from_language(language),
            "category": _normalize_category(category),
            "severityBucket": _severity_for(timing_bucket),
            "timingBucket": timing_bucket,
            "signature": pattern_signature(pattern),
            "occurrences": 1,
        }

        # TIER B: only when we can prove control of a connected install. The
        # server still decides the tier — we just attach the proof + drafted text.
        internal_headers: Optional[dict[str, str]] = None
        creds = _install_creds()
        if creds is not None:
            install_id, token = creds
            internal_headers = {
                "X-Boosthis-Install-Id": install_id,
                "X-Boosthis-Install-Token": token,
            }
            raw_pattern = _clean_text(pattern, 2000)
            if raw_pattern:
                body["pattern"] = raw_pattern
            d = draft or {}
            proposed_id = d.get("proposedRuleId")
            if isinstance(proposed_id, str):
                candidate = proposed_id[:80]
                if _RULE_ID_RE.match(candidate):
                    body["proposedRuleId"] = candidate
            title = _clean_text(d.get("title"), 160)
            if title:
                body["title"] = title
            when_to_apply = _clean_text(d.get("whenToApply"), 2000)
            if when_to_apply:
                body["whenToApply"] = when_to_apply
            fix_template = _clean_text(d.get("fixTemplate"), 8000)
            if fix_template:
                body["fixTemplate"] = fix_template

        _safe_transmit_internal(
            f"{_endpoint_base()}/rules/propose",
            body,
            auth_header=f"Bearer {key}",
            trusted_headers=internal_headers,
        )
    except Exception:
        # Best-effort learning loop: never let a proposal break the tool call.
        pass
