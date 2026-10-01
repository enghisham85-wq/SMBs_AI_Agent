"""Generate context blobs for AI agents.

Two surfaces:
  • ``build_context()`` — structured JSON for tool-using agents (MCP, HTTP)
  • ``render_markdown_context()`` — drop-in block for ``replit.md`` so even
    no-tool agents learn what Boosthis can do
  • ``match_rules_for_code(snippet)`` — cheap keyword matcher that returns
    rule IDs whose ``when_to_apply`` matches the code the agent is editing
"""

from __future__ import annotations

import math
import re

from boosthis import samples
from boosthis.checklist import BOOSTHIS_CHECKLIST, CHECKLIST_COUNT, CHECKLIST_VERSION
from boosthis.community import (
    read_community_cache,
    refresh_community_cache_if_stale,
)
from boosthis.thresholds import (
    RATING_CUTOFFS,
    RUNTIME_VERSION,
    SCORE_THRESHOLDS,
    SCORE_WEIGHTS,
)


# Cheap heuristics: token → rule_ids whose `when_to_apply` mentions it.
# Hand-curated for high signal — keep small, the LLM does the real reasoning.
_CODE_HINTS: dict[str, list[str]] = {
    "iterrows": ["pandas-iterrows-anti-pattern"],
    "df.apply": ["pandas-iterrows-anti-pattern"],
    "select_related": ["n-plus-one-orm-query"],
    "prefetch_related": ["n-plus-one-orm-query"],
    "lazy='select'": ["n-plus-one-orm-query"],
    "joinedload": ["n-plus-one-orm-query"],
    "requests.get": ["requests-default-no-timeout", "sync-io-in-async-handler"],
    "requests.post": ["requests-default-no-timeout", "sync-io-in-async-handler"],
    "httpx.get": ["requests-default-no-timeout"],
    "time.sleep": ["sync-io-in-async-handler"],
    "subprocess.run": ["sync-io-in-async-handler"],
    "psycopg2": ["sync-io-in-async-handler"],
    "Image.open": ["pillow-decode-on-request-path"],
    "PIL.Image": ["pillow-decode-on-request-path"],
    "@shared_task": ["celery-task-no-timeout"],
    "celery.task": ["celery-task-no-timeout"],
    "@app.task": ["celery-task-no-timeout"],
    "asyncio.create_task": ["asyncio-create-task-no-reference"],
    "create_task(": ["asyncio-create-task-no-reference"],
    "logger.debug(f": ["logging-in-hot-loop"],
    "log.debug(f": ["logging-in-hot-loop"],
    "json.dumps": ["json-vs-orjson-throughput"],
    "open(": ["open-files-without-context-manager"],
    "gunicorn": ["gunicorn-cold-fork-startup"],
    # v0.2 expansion — keyword hints for the 10 new rules so the AI matcher
    # has real signal, not just title substring hits.
    "def get(": ["fastapi-sync-route-blocks-event-loop"],
    "@app.get": ["fastapi-sync-route-blocks-event-loop"],
    "@router.get": ["fastapi-sync-route-blocks-event-loop"],
    "session = Session": ["sqlalchemy-session-per-request-leak"],
    "scoped_session": ["sqlalchemy-session-per-request-leak"],
    "sessionmaker": ["sqlalchemy-session-per-request-leak"],
    "np.array": ["numpy-python-loop-over-array"],
    "numpy": ["numpy-python-loop-over-array"],
    "df[": ["numpy-python-loop-over-array", "pandas-iterrows-anti-pattern"],
    "redis.set": ["redis-pipeline-not-used-for-bulk"],
    "redis.get": ["redis-pipeline-not-used-for-bulk"],
    "redis.hset": ["redis-pipeline-not-used-for-bulk"],
    "model_validate": ["pydantic-v2-model-validate-in-hot-path"],
    "BaseModel": ["pydantic-v2-model-validate-in-hot-path"],
    ".objects.all(": ["django-only-defer-on-large-rows"],
    ".objects.filter(": ["django-only-defer-on-large-rows"],
    "asyncio.gather": ["asyncio-gather-without-return-exceptions"],
    "TaskGroup": ["asyncio-gather-without-return-exceptions"],
    "requests.Session": ["requests-no-session-reuse"],
    "httpx.AsyncClient": ["requests-no-session-reuse", "sync-io-in-async-handler"],
    "httpx.Client": ["requests-no-session-reuse"],
    " in [": ["dict-membership-on-list"],
}


def _registration_rejected_reason() -> str | None:
    """Coarse, code-defined registration-rejection marker (or ``None``).

    Lazy-imported + fully guarded: ``import boosthis`` must stay cheap and the
    dashboard read must never break because telemetry is unavailable. The value
    is an enum the telemetry module defines (currently only
    ``"invalid_install_id"``) — never server- or developer-provided text.
    """
    try:
        from boosthis import telemetry as tm

        return tm.registration_rejected_reason()
    except Exception:  # noqa: BLE001
        return None


def build_context(*, sample_limit: int = 25) -> dict:
    """Structured context blob — meant to be served as JSON to an AI agent.

    Shape is intentionally stable; treat it like a public API.
    """
    from boosthis.health_axes import compute_dashboard_axes

    session_summary = samples.summary()
    return {
        "boosthis": {
            "runtime": "python",
            "version": RUNTIME_VERSION,
            "checklist_version": CHECKLIST_VERSION,
            "checklist_count": CHECKLIST_COUNT,
            # Coarse marker so the local dashboard can show a "Registration
            # rejected" card instead of looking connected/awaiting. Fixed enum
            # only — the card's copy is defined in dashboard.html.
            "registration_rejected": _registration_rejected_reason(),
        },
        "thresholds": {
            "score": SCORE_THRESHOLDS,
            "weights": SCORE_WEIGHTS,
            "rating_cutoffs": RATING_CUTOFFS,
            "formula": "score = TTFF*0.25 + TTI*0.45 + FID*0.30",
        },
        "session": {
            **session_summary,
            "recent": [s.to_dict() for s in samples.recent(limit=sample_limit)],
            "axes": compute_dashboard_axes(session_summary),
        },
        "rules": [
            {
                "id": r["id"],
                "title": r["title"],
                "category": r["category"],
                "languages": r["languages"],
            }
            for r in BOOSTHIS_CHECKLIST
        ],
    }


def render_markdown_context() -> str:
    """One-page markdown overview ready to inline into ``replit.md``.

    Designed so that a Replit AI agent reading ``replit.md`` immediately knows
    Boosthis exists, what tools are available, and what data it can ask for.
    """
    summary = samples.summary()
    poor = sum(
        1
        for r in summary.get("byRoute", {}).values()
        if r["worst_rating"] == "poor"
    )
    return f"""<!-- BOOSTHIS:BEGIN  Do not edit manually. Run `boosthis context` to regenerate. -->
## Boosthis — perf toolkit for this project

This project is instrumented with **Boosthis** (workspace-installed perf toolkit — installed via `pip install -e <boosthis-checkout>/lib/boosthis-py/`, not from PyPI). It tracks
per-route latency, classifies it against shared thresholds, and ships {CHECKLIST_COUNT}
Python performance rules.

**For AI agents (you, reading this):** Boosthis exposes itself via a local
MCP server and an HTTP server. Both run on demand:

| What you need | How to ask Boosthis |
|---|---|
| All rules | `boosthis list` · MCP: `boosthis.list_rules` · HTTP: `GET /rules` |
| Rule detail (fix template + when-to-apply) | `boosthis show <id>` · MCP: `boosthis.get_rule` · HTTP: `GET /rules/<id>` |
| Which rules might apply to a file | MCP: `boosthis.match_rules_for_code` · HTTP: `POST /match` |
| Recent perf samples + p50/p75/p99 | MCP: `boosthis.session_summary` · HTTP: `GET /context` |
| Patch prompt for a finding | `boosthis fix --finding <id>` |

Run `boosthis serve` for HTTP (default port 7787) or `boosthis mcp` for stdio MCP.

**Score thresholds** — used to classify any duration_ms measurement:
- TTFF: good ≤{SCORE_THRESHOLDS['ttff']['good']}ms · poor ≥{SCORE_THRESHOLDS['ttff']['poor']}ms · weight {SCORE_WEIGHTS['ttff']}
- TTI:  good ≤{SCORE_THRESHOLDS['tti']['good']}ms · poor ≥{SCORE_THRESHOLDS['tti']['poor']}ms · weight {SCORE_WEIGHTS['tti']}
- FID:  good ≤{SCORE_THRESHOLDS['fid']['good']}ms · poor ≥{SCORE_THRESHOLDS['fid']['poor']}ms · weight {SCORE_WEIGHTS['fid']}
- Composite rating: good ≥{RATING_CUTOFFS['good']} · needs-work ≥{RATING_CUTOFFS['needsWork']} · poor below

**Current session:** {summary['total']} samples, {poor} route(s) currently rated poor.

**Privacy contract:** Boosthis refuses to transmit any payload whose keys match
its 83-entry PII denylist. If you generate code that calls `safe_transmit()`
or `assert_no_pii()`, do not strip those guards.
<!-- BOOSTHIS:END -->
"""


# ── Scoring model (shared in spirit with the RN + Node matchers) ──────────────
# Three deterministic improvements over a naive substring ranker — no AI:
#   1. SHARPER MATCHING. Curated _CODE_HINTS stay (they are phrase patterns), but
#      id tokens and distinctive when_to_apply words are matched on WORD
#      BOUNDARIES (a token set), not raw ``in`` — so "value" no longer matches
#      inside "evaluate". Each when_to_apply hit is weighted by inverse document
#      frequency (IDF): a word in few rules is a far sharper signal.
#   2. SMARTER COMMUNITY WEIGHTING. A proven fix is boosted by how many DISTINCT
#      projects proved it AND how much evidence backs it — both with diminishing
#      (log) returns and a hard cap, so community refines the ranking instead of
#      swamping a strong direct code match.
#   3. IMPACT-AWARE TIE-BREAKS. Equal scores break by proven-project count, then
#      category (case studies first — real prod incidents), then how battle-
#      tested the rule is (evidence count), then id for determinism.
_HINT_WEIGHT = 2.0
_ID_TOKEN_WEIGHT = 3.0
_WHEN_BASE_WEIGHT = 1.0
_WHEN_IDF_WEIGHT = 3.0
_COMMUNITY_BASE = 1.5
_COMMUNITY_PROJECT_WEIGHT = 1.5
_COMMUNITY_EVIDENCE_WEIGHT = 0.5
_COMMUNITY_CAP = 6.0
_CATEGORY_RANK = {"case-study": 0, "industry": 1, "operational": 2}

# Generic words that carry no discriminating signal in when_to_apply text.
_STOPWORDS = {
    "python", "async", "await", "route", "routes", "request", "requests",
    "handler", "server", "performance", "function", "return", "before",
    "after", "instead", "value", "values", "using", "every", "where",
    "which", "while", "their", "there", "these", "those", "would",
    "should", "could", "symptom",
}

_WORD_RE = re.compile(r"[a-z][a-z0-9]+")


def _word_set(text: str, min_len: int) -> set[str]:
    """Lowercased identifier-ish words of length >= min_len (word-boundary)."""
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) >= min_len}


def _distinctive_words(text: str) -> set[str]:
    """Distinctive when_to_apply words (>=5 chars, not a stopword)."""
    return {w for w in _word_set(text, 5) if w not in _STOPWORDS}


_WORD_DF: dict[str, int] | None = None


def _word_doc_freq() -> dict[str, int]:
    """How many rules each distinctive when_to_apply word appears in (cached)."""
    global _WORD_DF
    if _WORD_DF is None:
        df: dict[str, int] = {}
        for r in BOOSTHIS_CHECKLIST:
            for w in _distinctive_words(r["when_to_apply"]):
                df[w] = df.get(w, 0) + 1
        _WORD_DF = df
    return _WORD_DF


def _community_boost(c: dict) -> float:
    """Confidence-weighted boost with diminishing returns + a hard cap."""
    boost = (
        _COMMUNITY_BASE
        + _COMMUNITY_PROJECT_WEIGHT * math.log2(1 + c["projects"])
        + _COMMUNITY_EVIDENCE_WEIGHT * math.log2(1 + c["evidence"])
    )
    return min(boost, _COMMUNITY_CAP)


def match_rules_for_code(snippet: str, *, language: str = "python") -> list[dict]:
    """Return rules whose ``when_to_apply`` likely matches the snippet.

    Pure heuristic — keyword index, NOT a real analyzer. The point is to give
    an AI agent a short, ranked list of rules to consider, not to make a
    pronouncement. The agent is expected to validate against the rule's full
    ``when_to_apply`` text before applying any fix.
    """
    text = snippet.lower()
    code_words = _word_set(snippet, 4)  # >=4 covers id tokens (>=4) + when words
    df = _word_doc_freq()

    # (1a) curated hints — phrase patterns, matched as substrings by design.
    hint_score: dict[str, float] = {}
    for needle, rule_ids in _CODE_HINTS.items():
        if needle.lower() in text:
            for rid in rule_ids:
                hint_score[rid] = hint_score.get(rid, 0.0) + _HINT_WEIGHT

    # Fail-open: an empty community book leaves the static ranking untouched.
    refresh_community_cache_if_stale("py")
    community = read_community_cache("py")

    scored: list[dict] = []
    for r in BOOSTHIS_CHECKLIST:
        if language not in r["languages"]:
            continue
        relevance = hint_score.get(r["id"], 0.0)
        # (1b) id tokens (>=4 chars), word-boundary.
        for tok in re.findall(r"[a-z]{4,}", r["id"]):
            if tok in code_words:
                relevance += _ID_TOKEN_WEIGHT
        # (1c) distinctive when_to_apply words, IDF-weighted (rarer => sharper).
        for w in _distinctive_words(r["when_to_apply"]):
            if w in code_words:
                relevance += _WHEN_BASE_WEIGHT + _WHEN_IDF_WEIGHT / df.get(w, 1)
        if relevance <= 0:
            continue
        # (2) community boost: only applied to a rule that ALREADY fits the page
        # (relevance > 0) — we never inject a proven-but-irrelevant rule.
        c = community.get(r["id"])
        score = relevance + _community_boost(c) if c else relevance
        scored.append(
            {
                "id": r["id"],
                "score": score,
                "projects": c["projects"] if c else 0,
                "category_rank": _CATEGORY_RANK.get(r["category"], 99),
                "evidence_count": len(r.get("evidence", [])),
            }
        )

    # (3) impact-aware, fully deterministic ordering.
    scored.sort(
        key=lambda s: (
            -s["score"],
            -s["projects"],
            s["category_rank"],
            -s["evidence_count"],
            s["id"],
        )
    )

    out = []
    for s in scored[:8]:
        rule = next((r for r in BOOSTHIS_CHECKLIST if r["id"] == s["id"]), None)
        if rule is None:
            continue
        # Candidates only — the prescriptive fix is NOT included here. The
        # agent calls boosthis.get_rule for the chosen rule to fetch its fix
        # per-rule from the server (the fix text is not bundled in this pkg).
        entry = {
            "id": rule["id"],
            "title": rule["title"],
            "category": rule["category"],
            "match_score": round(s["score"], 2),
            "when_to_apply": rule["when_to_apply"],
        }
        c = community.get(s["id"])
        if c:
            entry["community_proven"] = True
            entry["community_projects"] = c["projects"]
            entry["community_evidence"] = c["evidence"]
            entry["community_circumstances"] = c["circumstances"][:3]
        out.append(entry)
    return out
