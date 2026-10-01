"""Closed, append-only AI provider classification table."""
from __future__ import annotations

import os
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit

AI_PROVIDERS: Tuple[dict[str, Any], ...] = (
    {"id": "openai", "hosts": ("api.openai.com",)},
    {"id": "anthropic", "hosts": ("api.anthropic.com",)},
    {"id": "google", "hosts": ("generativelanguage.googleapis.com", "aiplatform.googleapis.com", ".aiplatform.googleapis.com")},
    {"id": "azure-openai", "hosts": (".openai.azure.com", ".cognitiveservices.azure.com")},
    {"id": "aws-bedrock", "hosts": (".amazonaws.com",)},
    {"id": "mistral", "hosts": ("api.mistral.ai", "codestral.mistral.ai")},
    {"id": "cohere", "hosts": ("api.cohere.ai", "api.cohere.com")},
    {"id": "groq", "hosts": ("api.groq.com",)},
    {"id": "together", "hosts": ("api.together.xyz", "api.together.ai")},
    {"id": "perplexity", "hosts": ("api.perplexity.ai",)},
    {"id": "deepseek", "hosts": ("api.deepseek.com",)},
    {"id": "xai", "hosts": ("api.x.ai",)},
    {"id": "fireworks", "hosts": ("api.fireworks.ai",)},
    {"id": "openrouter", "hosts": ("openrouter.ai",)},
    {"id": "replicate", "hosts": ("api.replicate.com",)},
    {"id": "huggingface", "hosts": ("api-inference.huggingface.co", "router.huggingface.co")},
    {"id": "voyage", "hosts": ("api.voyageai.com",)},
    {"id": "cerebras", "hosts": ("api.cerebras.ai",)},
    # THE ENDPOINT THE CUSTOMER RUNS THEMSELVES. Carries no hosts of its own:
    # the table walk can never reach it, and it is assigned only when the
    # destination matches an address the app declared. One code covers every
    # such endpoint, so a private address can never be reconstructed.
    {"id": "declared", "hosts": ()},
)
DECLARED_PROVIDER_CODE = next(
    (i + 1 for i, entry in enumerate(AI_PROVIDERS) if entry["id"] == "declared"),
    0,
)
AWS_BEDROCK_PREFIXES = ("bedrock-runtime.", "bedrock.")
MAX_DECLARED_AI_ENDPOINTS = 8
_MAX_HOST_LENGTH = 253
_DECLARED_ENV_VAR = "BOOSTHIS_AI_ENDPOINTS"
_declared_hosts: Tuple[str, ...] = ()
_env_read = False


def ai_provider_code(host: Any) -> int:
    """Return the provider's 1-based wire code, or zero. Never raises."""
    try:
        h = host.lower() if isinstance(host, str) else ""
        if not h:
            return 0
        for i, entry in enumerate(AI_PROVIDERS):
            for suffix in entry["hosts"]:
                hit = (h.endswith(suffix) or h == suffix[1:]) if suffix.startswith(".") else h == suffix
                if not hit:
                    continue
                if entry["id"] == "aws-bedrock" and not any(h.startswith(p) for p in AWS_BEDROCK_PREFIXES):
                    continue
                return i + 1
        return 0
    except Exception:  # noqa: BLE001
        return 0


def ai_provider_id(code: Any) -> Optional[str]:
    try:
        if isinstance(code, bool) or not isinstance(code, (int, float)):
            return None
        idx = round(code) - 1
        return AI_PROVIDERS[idx]["id"] if 0 <= idx < len(AI_PROVIDERS) else None
    except Exception:  # noqa: BLE001
        return None


# ── The endpoint the customer runs themselves ──────────────────────────────
#
# A declared endpoint is compared here, in memory, and only the fixed-table
# number above can leave the process. We deliberately do not price it from a
# public model-name table: a self-hosted model has no list price, and a private
# gateway's contract is not ours to infer.

def normalise_declared_endpoint(raw: Any) -> Optional[str]:
    """Reduce a declaration to one bare hostname, or None. Never raises."""
    try:
        if not isinstance(raw, str):
            return None
        value = raw.strip().lower()
        if not value or any(char.isspace() for char in value) or "*" in value:
            return None
        if "://" in value:
            value = urlsplit(value).hostname or ""
        else:
            # Strip path, credentials, then port in that order: a colon in a
            # path must never be mistaken for a port.
            value = value.split("/", 1)[0]
            value = value.rsplit("@", 1)[-1]
            if not value.startswith("[") and ":" in value:
                value = value.rsplit(":", 1)[0]
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        if not value or len(value) > _MAX_HOST_LENGTH or "*" in value:
            return None
        return value
    except Exception:  # noqa: BLE001
        return None


def _read_env_declarations() -> list[Any]:
    try:
        raw = os.environ.get(_DECLARED_ENV_VAR)
        if not isinstance(raw, str) or not raw.strip():
            return []
        return raw.split(",")
    except Exception:  # noqa: BLE001
        return []


def _rebuild(extra: Any) -> None:
    global _declared_hosts
    try:
        candidates = [*_read_env_declarations(), *extra]
    except Exception:  # noqa: BLE001
        candidates = _read_env_declarations()
    out = []
    for candidate in candidates:
        host = normalise_declared_endpoint(candidate)
        if host is None or host in out:
            continue
        if len(out) >= MAX_DECLARED_AI_ENDPOINTS:
            break
        out.append(host)
    _declared_hosts = tuple(out)


def _ensure_env_read() -> None:
    global _env_read
    if _env_read:
        return
    _env_read = True
    _rebuild(())


def set_declared_ai_endpoints(entries: Any) -> int:
    """Replace declarations, merged after BOOSTHIS_AI_ENDPOINTS. Never raises."""
    global _declared_hosts, _env_read
    try:
        _env_read = True
        extra = [] if entries is None else list(entries) if isinstance(entries, (list, tuple)) else [entries]
        _rebuild(extra)
        return len(_declared_hosts)
    except Exception:  # noqa: BLE001
        _declared_hosts = ()
        return 0


def declared_ai_endpoint_count() -> int:
    """Return only the count; declared hostnames are never exposed."""
    try:
        _ensure_env_read()
        return len(_declared_hosts)
    except Exception:  # noqa: BLE001
        return 0


def declared_ai_provider_code(host: Any) -> int:
    """Classify against exact declared hostnames only. Never raises."""
    try:
        _ensure_env_read()
        h = host.lower() if isinstance(host, str) else ""
        if h.startswith("[") and h.endswith("]"):
            h = h[1:-1]
        return DECLARED_PROVIDER_CODE if h and h in _declared_hosts else 0
    except Exception:  # noqa: BLE001
        return 0


def ai_provider_code_or_declared(host: Any) -> int:
    """Consult the maintained table first, then exact declarations."""
    known = ai_provider_code(host)
    return known if known else declared_ai_provider_code(host)


def _reset_declared_ai_endpoints_for_tests() -> None:
    """Test seam: forget declarations and re-read the environment next time."""
    global _declared_hosts, _env_read
    _declared_hosts = ()
    _env_read = False
