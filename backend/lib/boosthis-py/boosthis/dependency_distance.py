"""New-connection TCP+TLS setup timing, grouped into closed numeric kinds."""
from __future__ import annotations

import math
import os
import threading
from typing import Any, Dict, List, Optional

from .health_axes import linear_score, rating_for
from .runtime_flags import is_boosthis_disabled

MIN_CONNECTIONS = 5
RING_CAP = 128
KIND_MIN = 1
KIND_MAX = 6
SETUP_GOOD_MS = 20
SETUP_POOR_MS = 150

_lock = threading.Lock()
_samples: Dict[int, List[float]] = {}
_unmeasurable = 0

_REGION_VARS = (
    "VERCEL_REGION", "AWS_REGION", "AWS_DEFAULT_REGION", "FUNCTION_REGION",
    "GOOGLE_CLOUD_REGION", "REGION_NAME", "FLY_REGION",
    "RAILWAY_REPLICA_REGION", "RENDER_REGION",
)
_PREFIX_AREAS = (
    ("us-east", 2), ("us-west", 3), ("us-central", 4), ("ca-central", 2),
    ("ca-west", 3), ("sa-east", 5), ("southamerica-", 5), ("eu-north", 7),
    ("eu-", 6), ("europe-north", 7), ("europe-", 6), ("ap-south-", 9),
    ("ap-southeast-2", 12), ("ap-southeast-4", 12), ("ap-", 8),
    ("asia-south", 9), ("asia-", 8), ("australia", 12),
    ("northamerica-northeast", 2), ("me-", 10), ("il-", 10), ("af-", 11),
    ("africa-", 11), ("eastus", 2), ("westus", 3), ("centralus", 4),
    ("northcentralus", 4), ("southcentralus", 4), ("westcentralus", 4),
    ("canada", 2), ("brazil", 5), ("chile", 5), ("northeurope", 6),
    ("westeurope", 6), ("uk", 6), ("france", 6), ("germany", 6),
    ("switzerland", 6), ("italy", 6), ("spain", 6), ("poland", 6),
    ("sweden", 7), ("norway", 7), ("eastasia", 8), ("southeastasia", 8),
    ("japan", 8), ("korea", 8), ("centralindia", 9), ("southindia", 9),
    ("westindia", 9), ("jioindia", 9), ("uae", 10), ("qatar", 10),
    ("israel", 10), ("southafrica", 11),
)
_AIRPORT_AREAS = {
    **dict.fromkeys(("iad", "bos", "ewr", "atl", "mia", "yyz", "yul"), 2),
    **dict.fromkeys(("cle", "ord", "dfw", "den", "qro"), 4),
    **dict.fromkeys(("sfo", "sjc", "lax", "pdx", "sea", "phx"), 3),
    **dict.fromkeys(("gru", "gig", "scl", "eze", "bog"), 5),
    **dict.fromkeys(("lhr", "cdg", "fra", "ams", "dub", "mad", "waw", "otp", "zrh"), 6),
    **dict.fromkeys(("arn", "osl", "hel", "cph"), 7),
    **dict.fromkeys(("nrt", "hnd", "kix", "icn", "hkg", "sin", "bkk"), 8),
    **dict.fromkeys(("bom", "maa", "del"), 9),
    **dict.fromkeys(("syd", "mel", "akl"), 12),
    **dict.fromkeys(("dxb", "bah", "tlv"), 10),
    **dict.fromkeys(("jnb", "cpt", "los"), 11),
}


def area_for_region(raw: Any) -> int:
    try:
        region = raw.strip().lower() if isinstance(raw, str) else ""
        if not region:
            return 0
        for prefix, code in _PREFIX_AREAS:
            if region.startswith(prefix):
                return code
        return _AIRPORT_AREAS.get(region.rstrip("0123456789"), 1)
    except Exception:
        return 0


def declared_host_area() -> int:
    try:
        for name in _REGION_VARS:
            raw = os.environ.get(name)
            if isinstance(raw, str) and raw.strip():
                return area_for_region(raw)
    except Exception:
        pass
    return 0


def record_dependency_connection(kind: int, connect_ms: float, tls_ms: float = 0) -> None:
    """Record setup phases for one newly-opened connection.

    ``kind`` is the closed Node depKinds code (1..6). Hosts and addresses are
    deliberately not accepted by this API.
    """
    if is_boosthis_disabled():
        return
    try:
        code = int(kind)
        connect = float(connect_ms)
        tls = float(tls_ms)
        if code < KIND_MIN or code > KIND_MAX:
            return
        if not math.isfinite(connect) or not math.isfinite(tls):
            return
        setup = max(0.0, connect) + max(0.0, tls)
        with _lock:
            ring = _samples.setdefault(code, [])
            ring.append(setup)
            del ring[:-RING_CAP]
    except Exception:
        pass


def record_connection_for_host(host: Any, setup_ms: float) -> None:
    """Internal stdlib-hook feed; the host is classified and dropped here."""
    try:
        bare = str(host or "").split(":", 1)[0].lower()
        if not bare:
            return
        from boosthis.ai_providers import ai_provider_code
        from boosthis.db_work import is_hosted_database_call
        if is_hosted_database_call(bare, ""):
            code = 2
        elif ai_provider_code(bare):
            code = 3
        else:
            from boosthis.dependency_kinds import dependency_kind_for
            word = dependency_kind_for(bare)
            code = 4 if word == "storage" else 5 if word == "payments" else 6
        record_dependency_connection(code, setup_ms)
    except Exception:
        pass


def note_dependency_distance_unavailable() -> None:
    """Declare one observed outbound client whose setup phase is unavailable."""
    global _unmeasurable
    try:
        with _lock:
            _unmeasurable += 1
    except Exception:
        pass


def _median(values: List[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def compute_dependency_distance(
    kinds: List[Dict[str, Any]], new_connections: int,
    typical_request_ms: Optional[float] = None, host_area: int = 0,
) -> Dict[str, Any]:
    typical = (
        float(typical_request_ms)
        if isinstance(typical_request_ms, (int, float))
        and not isinstance(typical_request_ms, bool)
        and math.isfinite(typical_request_ms) and typical_request_ms > 0
        else 0
    )
    rows: List[Dict[str, Any]] = []
    worst_kind = judged = 0
    worst_setup = 0.0
    # THE EXCLUDED SIDE OF THE SAME SWEEP. A kind below the connection floor
    # is rightly kept out of the verdict, but publishing the smaller judged
    # figure under the word "worst" while holding a bigger one makes the word
    # false, so the excluded maximum travels beside it.
    unjudged = 0
    unjudged_worst_setup: Optional[float] = None
    unjudged_worst_kind: Optional[int] = None
    unjudged_worst_connections: Optional[int] = None
    for raw in kinds:
        try:
            kind = round(float(raw.get("kind", 0)))
            connections = max(0, round(float(raw.get("connections", 0))))
            if kind <= 0 or connections <= 0:
                continue
            setup = max(0.0, round(float(raw.get("setupMs", 0)) * 10) / 10)
            row: Dict[str, Any] = {"kind": kind, "connections": connections, "setupMs": setup}
            if typical > 0:
                row["sharePct"] = max(0, min(100, round(setup / typical * 1000) / 10))
            rows.append(row)
            if connections >= MIN_CONNECTIONS:
                judged += 1
                if setup > worst_setup:
                    worst_kind, worst_setup = kind, setup
            else:
                unjudged += 1
                if unjudged_worst_setup is None or setup > unjudged_worst_setup:
                    unjudged_worst_setup = setup
                    unjudged_worst_kind = kind
                    unjudged_worst_connections = connections
        except Exception:
            continue
    rows.sort(key=lambda row: (-row["setupMs"], row["kind"]))
    common = {
        "newConnections": max(0, round(new_connections)), "minConnections": MIN_CONNECTIONS,
        "judgedKinds": judged, "worstKind": worst_kind, "worstSetupMs": worst_setup,
        "typicalRequestMs": round(typical), "hostArea": max(0, round(host_area)),
        "ownBackendOnly": 0, "kinds": rows,
        # Only sent when it is BIGGER than the published worst — a smaller
        # excluded reading changes nothing about what "worst" means.
        "unjudgedKinds": unjudged,
        "unjudgedWorstSetupMs": (
            unjudged_worst_setup
            if unjudged_worst_setup is not None and unjudged_worst_setup > worst_setup
            else None
        ),
        "unjudgedWorstKind": (
            unjudged_worst_kind
            if unjudged_worst_setup is not None and unjudged_worst_setup > worst_setup
            else None
        ),
        "unjudgedWorstConnections": (
            unjudged_worst_connections
            if unjudged_worst_setup is not None and unjudged_worst_setup > worst_setup
            else None
        ),
    }
    if judged == 0:
        return {"score": None, "rating": "pending", "measurable": 0,
                "worstSharePct": None, **common}
    score = linear_score(worst_setup, SETUP_GOOD_MS, SETUP_POOR_MS)
    share = max(0, min(100, round(worst_setup / typical * 1000) / 10)) if typical > 0 else None
    return {"score": score, "rating": rating_for(score), "measurable": 1,
            "worstSharePct": share, **common}


def read_dependency_distance() -> Optional[Dict[str, Any]]:
    if is_boosthis_disabled():
        return None
    try:
        with _lock:
            copied = {kind: list(values) for kind, values in _samples.items()}
            unavailable = _unmeasurable
        total = sum(len(values) for values in copied.values())
        if total == 0:
            if unavailable:
                return {
                    "score": None, "rating": "pending", "measurable": 0,
                    "newConnections": None, "minConnections": MIN_CONNECTIONS,
                    "judgedKinds": 0, "worstKind": 0, "worstSetupMs": None,
                    "worstSharePct": None, "typicalRequestMs": 0,
                    "hostArea": declared_host_area(), "ownBackendOnly": 0, "kinds": [],
                }
            return None
        rows = [
            {"kind": kind, "connections": len(values), "setupMs": _median(values)}
            for kind, values in copied.items() if values
        ]
        typical = None
        try:
            from boosthis import samples
            typical = samples.summary().get("p50_ms")
        except Exception:
            pass
        return compute_dependency_distance(rows, total, typical, declared_host_area())
    except Exception:
        return None


def clear_dependency_distance() -> None:
    global _unmeasurable
    try:
        with _lock:
            _samples.clear()
            _unmeasurable = 0
    except Exception:
        pass
