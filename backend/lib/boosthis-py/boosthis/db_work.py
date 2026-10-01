"""Database work — how much of a request waited on the database (Python).

WHY THIS EXISTS. A Python app on FastAPI, Django or Flask could already see how
long each request took, and nothing told it how much of that time was spent
WAITING ON THE DATABASE — or that one request ran the same statement twenty
times. The rule book has always told developers to fix both
(``python-n-plus-one-query``, ``unbounded-result-set``); no reading ever found
them, because the kit only watched calls a developer had wrapped by hand plus
outbound HTTP. A driver talking Postgres over its own socket was completely
invisible, and the outside-service share was withheld entirely rather than
published with a hole in it.

This is the Node reader's shape, ported. Same fields, same vocabulary, same
thresholds — so the dashboard tile, the AI answers and the rules never have to
special-case the runtime.

TWO SHAPES OF DATABASE, AND THEY FAIL DIFFERENTLY.

 1. A TRADITIONAL DRIVER opens its own connection and never touches the HTTP
    stack, so nothing here saw it at all. This module watches the round-trip
    layer of the two mainstream Postgres drivers the host may already have
    loaded — ``psycopg`` (v3) at ``Cursor.execute`` / ``AsyncCursor.execute``,
    and ``asyncpg`` at ``Connection.execute`` and its fetch family. Those are
    the points every plain query AND every ORM query passes through, because
    SQLAlchemy, the Django ORM and the async ORMs all issue through a driver.
    Connection-level convenience wrappers that delegate to one of these are
    deliberately NOT patched: patching both would count one pooled query twice.

 2. A HOSTED DATABASE REACHED OVER THE WEB (Supabase, Firebase, Neon's HTTP
    driver, Turso, PlanetScale …) rides the outbound-call observation points
    the kit already owns — but it arrived there as a destination name and
    looked exactly like any other API call. :func:`is_hosted_database_call`
    classifies it as database work instead, so the reading covers a project
    that mixes both rather than silently reporting on half of it.

ONE CALL BELONGS TO ONE METER. A hosted database is claimed HERE and dropped by
the repeated-work and outside-dependency readings, never counted by both — one
repeated query reported twice would send a developer to fix the same thing in
two places. The wire-level readings (attempt counts, reliability, latency) still
see every call: those measure the SOCKET, which is the same socket either way.

NEVER LOADS A DRIVER THE HOST DID NOT CHOOSE. A library is observed only if the
app ALREADY imported it: the check is a lookup in ``sys.modules``, and the
module object is taken FROM there. This kit never imports a database driver,
never resolves one into existence, and an app with no database client is
untouched.

THE SCOPE IS TAKEN WHEN THE QUERY IS ISSUED. A pooled connection answers on a
socket opened during an EARLIER request; reading the ambient scope at completion
credits every query after the first to a request that already ended, and the
counts freeze at one while every unit test still passes. So
:func:`note_db_call_in` — which takes the scope explicitly — is the real entry
point, and :func:`note_db_call` is only an ambient convenience for a caller
standing in the request it is filing for.

PRIVACY — the whole point of counting in here. A statement's SHAPE plus its
ARGUMENTS are a visible input, which is exactly what makes repetition
judgeable; both are folded into an in-process hash on the stack and dropped. No
SQL text, no parameter value, no table or column name, no database host and no
connection string is stored, logged, or uploaded. Only counts, durations and
shares ever leave — the same numbers-only contract the repeated-work meter
already ships under.

HONEST WHEN BLIND. A driver this kit cannot watch must never read as an app with
no database work. :func:`unwatched_db_client_count` counts the database
libraries this process imported that have no safe round-trip layer to observe,
and that count ships beside the reading so the tile can say "cannot tell"
instead of "none". A kit that watched nothing at all reports NO axis, which the
dashboard reads as "cannot tell" — never a confident zero.

ADDITIVE. This never touches the composite Speed score, and it never files into
the repeated-work meter: database repetition is reported here, HTTP and
hand-wrapped repetition stays there, and neither reading moves because the other
shipped.
"""

from __future__ import annotations

import sys
import threading
import time
import weakref
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .runtime_flags import is_boosthis_disabled

# Most distinct statement identities tracked inside ONE request. Past this the
# tally stops learning new ones (repeats of the ones it already knows still
# count), so a pathological request can never grow memory.
MAX_DISTINCT_STATEMENTS = 512
# Longest statement text folded into an identity. A statement's shape is settled
# long before this; the cap keeps a generated mega-query cheap to hash.
MAX_SQL_CHARS = 512
# The module table is scanned at most this often while a watchable driver is
# still missing, so arming costs nothing on a hot request path.
ARM_RETRY_SEC = 1.0
# Most round-trip intervals retained for sequencing in one request. A request
# beyond this cap is omitted from dbSequencing only.
DB_WAVE_MAX_CALLS = 256


# ═══════════════════════════════════════════════════════════════════════════
# Which drivers we can watch, and which we can only admit to
# ═══════════════════════════════════════════════════════════════════════════
#: Drivers this kit knows how to watch. ``module`` is the ``sys.modules`` key
#: that proves the host already imported one — it is only ever READ, never
#: imported.
WATCHABLE_DB_CLIENTS: Tuple[str, ...] = ("psycopg", "asyncpg")
# Pool implementations whose live objects this module can read. This is one
# greppable declaration; observation never imports any of them.
WATCHABLE_DB_POOLS: Tuple[str, ...] = ("asyncpg", "psycopg_pool", "sqlalchemy")

#: Database clients this kit KNOWS about but has no safe round-trip layer for
#: yet. Listing them is the honest half of the deal: an app that imported one of
#: these has database calls this meter cannot see, and the tile must say so
#: rather than report a clean picture built on the calls it happened to catch.
#:
#: Deliberately NOT here: ORMs and query builders (SQLAlchemy, the Django ORM,
#: peewee, Tortoise, SQLModel). They do not talk to a database themselves — they
#: issue through a driver, and the driver is what decides whether the work is
#: visible. Counting them would double-report one blind spot. Cache clients
#: (redis) are also out: this meter is about the database a request waits on,
#: and calling a cache an unwatched database would overstate the gap.
UNWATCHABLE_DB_CLIENTS: Tuple[str, ...] = (
    # The cursor is a C extension type whose attributes cannot be replaced, and
    # wrapping ``connect`` to hand back a proxy would change what the host's own
    # call returns. Blind, and said so.
    "psycopg2",
    # Not Postgres, and no Python funnel located yet — but still database work a
    # request waits on, so the blind spot is real and must be counted.
    "pymysql",
    "MySQLdb",
    "mysql.connector",
    "aiomysql",
    "sqlite3",
    "aiosqlite",
    "pymongo",
    "motor",
    "oracledb",
    "cx_Oracle",
    "pymssql",
    "pyodbc",
    "libsql_client",
    "prisma",
)


# ═══════════════════════════════════════════════════════════════════════════
# Hosted-database classification (over HTTP)
# ═══════════════════════════════════════════════════════════════════════════
#: Hostname suffixes that identify a hosted database reached over the web.
#:
#: Matched against the bare hostname the outbound observer already computed —
#: nothing else about the call is read to make this decision. The list is
#: deliberately made of vendor-owned data-plane names, never a generic API host:
#: mistaking an ordinary API for a database would move real API traffic into a
#: database reading, which is the exact confusion this is here to end. The Node
#: table, verbatim, so one hosted database reads the same on both runtimes.
HOSTED_DB_HOST_SUFFIXES: Tuple[str, ...] = (
    ".supabase.co",
    ".supabase.in",
    ".supabase.net",
    ".firebaseio.com",
    ".firebasedatabase.app",
    "firestore.googleapis.com",
    ".neon.tech",
    ".upstash.io",
    ".turso.io",
    ".turso.tech",
    ".planetscale.com",
    ".psdb.cloud",
    ".cockroachlabs.cloud",
    ".mongodb-api.com",
    ".fauna.com",
    ".xata.sh",
    ".prisma-data.net",
)

#: Path prefixes that identify a hosted database on a host we cannot recognise —
#: a self-hosted Supabase / PostgREST behind the project's own domain. Only
#: consulted when the hostname did not already answer, and only the prefix is
#: examined: the rest of the path (which is where a PostgREST filter carries its
#: column names and values) is never read here.
HOSTED_DB_PATH_PREFIXES: Tuple[str, ...] = ("/rest/v1/",)


def is_hosted_database_call(host: Any, path: Any) -> bool:
    """Is this outbound call database work rather than ordinary API traffic?

    ``host`` is the bare hostname the outbound observer already normalised;
    ``path`` is the request path. Pure, total, and cheap — it runs on every
    outbound call. Never raises.
    """
    try:
        h = host.lower() if isinstance(host, str) else ""
        if h:
            for suffix in HOSTED_DB_HOST_SUFFIXES:
                if h == suffix or h.endswith(suffix):
                    return True
                # A suffix written with a leading dot also matches the bare apex.
                if suffix.startswith(".") and h == suffix[1:]:
                    return True
        p = path if isinstance(path, str) else ""
        if p:
            for prefix in HOSTED_DB_PATH_PREFIXES:
                if p.startswith(prefix):
                    return True
        return False
    except Exception:  # noqa: BLE001
        return False


def is_hosted_db_target(target: Any) -> bool:
    """The same question, asked of a composed outbound target.

    The kit's outbound observers all carry one string of the form
    ``"METHOD scheme://host[:port]/path"``. This splits out the host and the
    path — and nothing else — so the repeated-work and outside-dependency
    meters can drop a call this meter has claimed without each growing its own
    copy of the classification. Never raises.
    """
    try:
        if not isinstance(target, str) or not target:
            return False
        candidate = target.split(None, 1)[-1] if "://" in target else target
        parts = urlsplit(candidate)
        return is_hosted_database_call(parts.hostname or "", parts.path or "")
    except Exception:  # noqa: BLE001
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Per-request tally
# ═══════════════════════════════════════════════════════════════════════════
@dataclass
class DbRequestTally:
    """One request's database work.

    Carried by a ContextVar rather than hung off the repeated-work scope: that
    scope is a bare mapping of identities, and a second shape inside it would
    have to be understood by every reader of the first.
    """

    #: identity -> [runs, total ms]
    statements: Dict[int, List[float]] = field(default_factory=dict)
    #: Database round trips watched in this request.
    count: int = 0
    #: Total ms waited on the database in this request (calls may overlap).
    ms: float = 0.0
    #: Of ``count``, how many were a hosted database reached over the web.
    hosted: int = 0
    #: The largest number of ROWS a single statement handed back in this
    #: request. A row COUNT, never a row: no value, no column name and no table
    #: name is read to obtain it. A cursor or a streaming query reports none,
    #: and reports it as "unknown" rather than as zero.
    rows_worst: int = 0
    #: Result sizes include zero, but not calls whose size is unknown.
    rows_sum: int = 0
    rows_calls: int = 0
    #: Monotonic half-open [start,end) intervals, in milliseconds.
    spans: List[Tuple[float, float]] = field(default_factory=list)
    spans_over: bool = False


_current_db: ContextVar[Optional[DbRequestTally]] = ContextVar(
    "boosthis_db_work", default=None
)

# Re-entrancy guard. A driver whose public fetch helper is implemented on top of
# another watched method must count ONE round trip, not two — and the guard has
# to be per-task and per-thread or a concurrent request would be suppressed by
# somebody else's query. A ContextVar is both.
_in_query: ContextVar[bool] = ContextVar("boosthis_db_in_query", default=False)

_lock = threading.RLock()

# ── Session totals (the only things that ever leave) ────────────────────────
# Requests in which at least one database call was watched.
_watched_requests = 0
# Wall ms of those requests (the share denominator).
_watched_request_ms = 0.0
# Ms spent waiting on the database, clamped per request to that request's own
# wall time so overlapping queries can never push the share past 100%.
_db_ms = 0.0
# Database round trips watched.
_call_count = 0
# Of those, how many were a hosted database reached over the web.
_hosted_calls = 0
# Worst number of identical statements inside a SINGLE request.
_repeat_worst = 0
# Watched requests that ran the same statement more than once.
_repeat_requests = 0
# Ms attributable to the redundant runs, clamped per request.
_repeat_ms = 0.0
# Largest single-statement row count seen in any watched request.
_rows_worst = 0
_rows_sum = 0
_rows_calls = 0
_wave_requests = 0
_waves_total = 0
_waves_worst = 0
_wave_span_ms = 0.0
_wave_sum_ms = 0.0

# Weak references only: holding instrumentation must not extend a pool's life.
_pool_refs: List[Any] = []

# ── Observation state ───────────────────────────────────────────────────────
# library -> our observation is currently in place.
_installed: Dict[str, bool] = {}
# Every method we replaced: (library, owner, attribute, original, wrapper). The
# WRAPPER is kept so a restore can check ours is still the callable in place
# before putting the original back.
_patches: List[Tuple[str, Any, str, Any, Any]] = []
# Whether arming has ever been ATTEMPTED. Before that the kit claims nothing and
# the honesty report stays silent: an axis that has not started is not a blind
# spot.
_arm_attempted = False
# Live switch: a disarm that cannot restore a method still stops the feed.
_active = False
_last_arm_at = 0.0
_arm_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════
# Request lifecycle
# ═══════════════════════════════════════════════════════════════════════════
def begin_db_work() -> Any:
    """Open the per-request tally. Returns a handle for :func:`end_db_work`, or
    None when the kill-switch is on (every later call is then a no-op, so the
    pair can never half-run). Never raises."""
    if is_boosthis_disabled():
        return None
    try:
        tally = DbRequestTally()
        return tally, _current_db.set(tally)
    except Exception:  # noqa: BLE001
        return None


def end_db_work(handle: Any, request_ms: float) -> None:
    """Fold one finished request's database work into the session totals.

    ``request_ms`` is the request's own wall time — the denominator for both
    shares. Called from the boundary's guaranteed-exit path, AFTER the
    outside-dependency close: that reading borrows this request's database ms
    off the still-open tally so every share divides by one denominator, and
    closing here first would silently zero the borrowed half.

    Never raises; a request with no database work teaches this meter nothing and
    is not counted as watched.
    """
    global _watched_requests, _watched_request_ms, _db_ms, _call_count
    global _hosted_calls, _repeat_worst, _repeat_requests, _repeat_ms, _rows_worst
    global _rows_sum, _rows_calls, _wave_requests, _waves_total, _waves_worst
    global _wave_span_ms, _wave_sum_ms
    if not handle:
        return
    try:
        tally, token = handle
        try:
            _current_db.reset(token)
        except Exception:  # noqa: BLE001
            pass
        if not isinstance(tally, DbRequestTally) or tally.count == 0:
            return
        if is_boosthis_disabled():
            return
        wall = float(request_ms or 0)
        if not (wall > 0):
            wall = 0.0
        worst = 0
        redundant = 0.0
        with _lock:
            for runs, total_ms in tally.statements.values():
                n = int(runs)
                if n > worst:
                    worst = n
                # Mean duration × the redundant runs: we keep a total, not a
                # list.
                if n > 1:
                    redundant += (total_ms / n) * (n - 1)
            tally.statements.clear()
            _watched_requests += 1
            _watched_request_ms += wall
            # Clamp to this request's own wall time: parallel queries can add up
            # to more than the request lasted, and a share above 100% is not a
            # fact.
            _db_ms += min(tally.ms, wall) if wall > 0 else 0.0
            _call_count += tally.count
            _hosted_calls += tally.hosted
            if worst > _repeat_worst:
                _repeat_worst = worst
            if tally.rows_worst > _rows_worst:
                _rows_worst = tally.rows_worst
            _rows_sum += tally.rows_sum
            _rows_calls += tally.rows_calls
            if worst > 1:
                _repeat_requests += 1
                _repeat_ms += min(redundant, wall) if wall > 0 else 0.0
            if not tally.spans_over and tally.spans:
                waves, span_ms, sum_ms = _sweep_db_waves(tally.spans)
                _wave_requests += 1
                _waves_total += waves
                _waves_worst = max(_waves_worst, waves)
                _wave_span_ms += min(span_ms, wall) if wall > 0 else span_ms
                _wave_sum_ms += sum_ms
    except Exception:  # noqa: BLE001
        pass


def current_db_scope() -> Optional[DbRequestTally]:
    """The in-flight request's tally, read SYNCHRONOUSLY at the moment a query
    is issued. Hand the result to :func:`note_db_call_in`; do not re-read it
    when the query completes. Never raises."""
    try:
        return _current_db.get()
    except Exception:  # noqa: BLE001
        return None


def current_db_ms() -> Optional[float]:
    """Ms this request has waited on the database so far, or None when that is
    not knowable.

    None means "the database reader is not running here", which is what the
    outside-service share needs to hear: a zero would claim the request did no
    database work, and the share would be published on a denominator that is a
    guess. Never raises.
    """
    try:
        if not _arm_attempted or is_boosthis_disabled():
            return None
        tally = _current_db.get()
        if not isinstance(tally, DbRequestTally):
            # The boundary is running the reader but this unit never opened a
            # tally (an adapter that closes without a begin). Unknown, not zero.
            return None
        return max(0.0, float(tally.ms))
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Recording
# ═══════════════════════════════════════════════════════════════════════════
def note_db_call_in(
    scope: Optional[DbRequestTally],
    identity: Optional[int],
    duration_ms: float,
    hosted: bool = False,
    rows: Optional[int] = None,
    start_ms: Optional[float] = None,
    end_ms: Optional[float] = None,
) -> None:
    """File one finished round trip into the request that ISSUED it.

    THE REAL ENTRY POINT. ``scope`` must be the tally captured synchronously
    when the query was issued — see :func:`current_db_scope`. Never raises.
    """
    try:
        if scope is None or not isinstance(scope, DbRequestTally):
            return
        if identity is None or is_boosthis_disabled():
            return
        ms = float(duration_ms)
        if not (ms == ms) or ms < 0:  # NaN or negative
            ms = 0.0
        with _lock:
            scope.count += 1
            scope.ms += ms
            if hosted:
                scope.hosted += 1
            if start_ms is None or end_ms is None:
                end_ms = perf_counter() * 1000.0
                start_ms = end_ms - ms
            _note_db_span(scope, start_ms, end_ms)
            if isinstance(rows, int) and not isinstance(rows, bool) and rows >= 0:
                scope.rows_worst = max(scope.rows_worst, rows)
                scope.rows_sum += rows
                scope.rows_calls += 1
            seen = scope.statements.get(identity)
            if seen is not None:
                seen[0] += 1
                seen[1] += ms
                return
            if len(scope.statements) >= MAX_DISTINCT_STATEMENTS:
                return
            scope.statements[identity] = [1, ms]
    except Exception:  # noqa: BLE001
        pass  # instrumentation must never disturb the host


def _note_db_span(scope: DbRequestTally, start: Any, end: Any) -> None:
    if scope.spans_over:
        return
    try:
        a, b = float(start), float(end)
        if a != a or b != b or b < a:
            return
        if len(scope.spans) >= DB_WAVE_MAX_CALLS:
            scope.spans_over = True
            scope.spans.clear()
            return
        scope.spans.append((a, b))
    except Exception:  # noqa: BLE001
        return


def _sweep_db_waves(spans: Any) -> Tuple[int, float, float]:
    """Sweep stable start/end-sorted half-open intervals."""
    ordered = sorted(enumerate(list(spans)), key=lambda item: (item[1][0], item[1][1], item[0]))
    waves, span_ms, sum_ms = 0, 0.0, 0.0
    seg_start = seg_end = 0.0
    opened = False
    for _, (start, end) in ordered:
        sum_ms += end - start
        if not opened:
            opened = True
            waves = 1
            seg_start, seg_end = start, end
        elif start < seg_end:
            seg_end = max(seg_end, end)
        else:
            span_ms += seg_end - seg_start
            waves += 1
            seg_start, seg_end = start, end
    if opened:
        span_ms += seg_end - seg_start
    return waves, span_ms, sum_ms


def note_db_call(
    identity: Optional[int],
    duration_ms: float,
    hosted: bool = False,
    rows: Optional[int] = None,
) -> None:
    """Ambient convenience for a caller standing in the request it is filing
    for. Everything that observes a pooled driver must use
    :func:`note_db_call_in` instead. Never raises."""
    note_db_call_in(current_db_scope(), identity, duration_ms, hosted, rows)


def note_hosted_db_call(
    scope: Optional[DbRequestTally], target: Any, duration_ms: float
) -> None:
    """File one hosted-database call reached over the web — IF it is one.

    ``target`` is the outbound observer's own composed description of the call
    (method + origin + path); it is hashed immediately on this stack and never
    retained. ``scope`` is the tally captured when the call was ISSUED, so a
    response that lands after the request moved on is still credited correctly.

    The "is it a database?" question is answered HERE, not at the three
    observers that call this. One call belongs to one meter, and a rule with
    three copies is a rule with three ways to disagree: the outbound meters
    refuse a hosted database at their own doors using the very same test, so a
    second copy of it out there is how an ordinary API call ends up counted by
    nobody, or by both. Never raises.
    """
    try:
        if scope is None:
            return
        if not is_hosted_db_target(target):
            return
        from .repeated_work import call_identity

        note_db_call_in(scope, call_identity(str(target)), duration_ms, True, None)
    except Exception:  # noqa: BLE001
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Statement identity and row counts (both read on the stack, then dropped)
# ═══════════════════════════════════════════════════════════════════════════
_QUERY_KWARGS = ("query", "sql", "operation", "command", "statement")
_PARAM_KWARGS = ("params", "parameters", "vars", "args", "seq_of_parameters")


def statement_identity(args: Any, kwargs: Any = None) -> Optional[int]:
    """Fold a statement's text plus its arguments into one in-process number.

    Both are read on this stack, hashed and dropped. A call whose text we cannot
    recognise returns None and is left UNCOUNTED rather than folded into a
    catch-all identity, which would invent repeats out of unrelated queries.
    """
    try:
        text: Any = None
        values: List[Any] = []
        seq = list(args or ())
        kw = dict(kwargs or {})
        if seq:
            text = seq[0]
            values = seq[1:]
        else:
            for key in _QUERY_KWARGS:
                if key in kw:
                    text = kw.pop(key)
                    break
        for key in _PARAM_KWARGS:
            if key in kw:
                values.append(kw.pop(key))
        if isinstance(text, (bytes, bytearray)):
            try:
                text = bytes(text).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                text = None
        if not isinstance(text, str) or not text:
            return None
        if len(text) > MAX_SQL_CHARS:
            text = text[:MAX_SQL_CHARS]
        from .repeated_work import call_identity

        return call_identity(text, values)
    except Exception:  # noqa: BLE001
        return None


def _row_count_of(result: Any, receiver: Any, method_name: str = "") -> Optional[int]:
    """How many rows a finished query handed back, or None when the shape does
    not say.

    Reads a COUNT and nothing else — never a row, a value, a column name or a
    table name. An unrecognised shape reports nothing at all instead of a zero,
    because "no result set" and "an empty result set" are different facts and
    only one of them is a small query.
    """
    try:
        if isinstance(result, (list, tuple)):
            return len(result)
        # asyncpg.fetchrow has an unambiguous result cardinality: None means no
        # row, while a row containing SQL NULL is still a Record object.
        if method_name == "fetchrow":
            return 0 if result is None else 1
        n = getattr(receiver, "rowcount", None)
        if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
            return n
        return None
    except Exception:  # noqa: BLE001
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Wrapping a driver's round-trip method
# ═══════════════════════════════════════════════════════════════════════════
def _make_observed_query(real: Any) -> Any:
    """Build the wrapper that goes around a synchronous round-trip method.

    Separated from the module lookup so the host-safety rules it has to keep can
    be exercised directly against a stand-in driver — above all that the host's
    own method is invoked EXACTLY once however it fails, which a wrapper that
    guards its own call site cannot promise.
    """

    def observed(receiver: Any, *args: Any, **kwargs: Any) -> Any:
        if not _active:
            return real(receiver, *args, **kwargs)
        started = perf_counter()
        started_ms = started * 1000.0
        # Capture the issuing request HERE, on the host's own stack, while the
        # answer is still unambiguous. A pooled driver replies on a connection
        # it opened during an earlier request.
        scope = current_db_scope()
        identity, token = _open_query(args, kwargs)
        filed = [False]

        def file(rows: Optional[int] = None) -> None:
            # One round trip is one filing: a driver that both calls back and
            # throws is still counted once.
            if filed[0]:
                return
            filed[0] = True
            note_db_call_in(
                scope, identity, (perf_counter() - started) * 1000.0, False, rows,
                started_ms, perf_counter() * 1000.0,
            )

        # The host's own call is made on a path whose only handler RE-RAISES.
        # A guard around it that fell through would run the app's query a
        # second time.
        try:
            out = real(receiver, *args, **kwargs)
        except BaseException:
            try:
                file()
            except Exception:  # noqa: BLE001
                pass  # never disturb the host's own throw
            _close_query(token)
            raise
        try:
            file(_row_count_of(out, receiver, getattr(real, "__name__", "")))
        except Exception:  # noqa: BLE001
            pass  # never disturb the host's value
        _close_query(token)
        return out

    observed._boosthis_db_wrapper = True  # type: ignore[attr-defined]
    return observed


def _make_observed_async_query(real: Any) -> Any:
    """The same wrapper for a coroutine round-trip method. Awaiting keeps this
    task's own context, so the scope captured before the await is still the
    issuing request's when the answer lands."""

    async def observed(receiver: Any, *args: Any, **kwargs: Any) -> Any:
        if not _active:
            return await real(receiver, *args, **kwargs)
        started = perf_counter()
        started_ms = started * 1000.0
        scope = current_db_scope()
        identity, token = _open_query(args, kwargs)
        filed = [False]

        def file(rows: Optional[int] = None) -> None:
            if filed[0]:
                return
            filed[0] = True
            note_db_call_in(
                scope, identity, (perf_counter() - started) * 1000.0, False, rows,
                started_ms, perf_counter() * 1000.0,
            )

        try:
            out = await real(receiver, *args, **kwargs)
        except BaseException:
            try:
                file()
            except Exception:  # noqa: BLE001
                pass
            _close_query(token)
            raise
        try:
            file(_row_count_of(out, receiver, getattr(real, "__name__", "")))
        except Exception:  # noqa: BLE001
            pass
        _close_query(token)
        return out

    observed._boosthis_db_wrapper = True  # type: ignore[attr-defined]
    return observed


def _make_housekeeping_wrapper(real: Any, is_async: bool) -> Any:
    """Mark a driver's own tidy-up method as NOT the app's query.

    A pooled driver talks to the database on its own account: asyncpg's pool
    resets every connection as it is handed back, which is a genuine round trip
    on a genuine socket, issued while the borrowing request is still open. It is
    not something the application asked for, and counting it would tell a
    developer their two-query endpoint ran four queries — then send them looking
    for the other two.

    So the round trips a housekeeping method makes are suppressed the same way a
    nested one is: by standing inside the guard while it runs. The method is
    still called exactly once, on a path whose only handler re-raises, and what
    it returns is handed straight back.
    """
    if is_async:

        async def housekeeping_async(receiver: Any, *args: Any, **kwargs: Any) -> Any:
            if not _active:
                return await real(receiver, *args, **kwargs)
            token = None
            try:
                token = _in_query.set(True)
            except Exception:  # noqa: BLE001
                token = None
            try:
                return await real(receiver, *args, **kwargs)
            finally:
                _close_query(token)

        housekeeping_async._boosthis_db_wrapper = True  # type: ignore[attr-defined]
        return housekeeping_async

    def housekeeping(receiver: Any, *args: Any, **kwargs: Any) -> Any:
        if not _active:
            return real(receiver, *args, **kwargs)
        token = None
        try:
            token = _in_query.set(True)
        except Exception:  # noqa: BLE001
            token = None
        try:
            return real(receiver, *args, **kwargs)
        finally:
            _close_query(token)

    housekeeping._boosthis_db_wrapper = True  # type: ignore[attr-defined]
    return housekeeping


def _install_housekeeping(library: str, owner: Any, names: Tuple[str, ...]) -> None:
    """Wrap every named tidy-up method we can find on ``owner``. A method that
    is absent, or that will not take the swap, simply stays as it is: the cost
    is a driver round trip counted as the app's, never a broken host."""
    for name in names:
        try:
            original = owner.__dict__.get(name) if hasattr(owner, "__dict__") else None
            if original is None:
                original = getattr(owner, name, None)
            if original is None or not callable(original):
                continue
            if getattr(original, "_boosthis_db_wrapper", False):
                continue
            try:
                import inspect

                is_async = inspect.iscoroutinefunction(original)
            except Exception:  # noqa: BLE001
                is_async = False
            _swap(
                library,
                owner,
                name,
                original,
                _make_housekeeping_wrapper(original, is_async),
            )
        except Exception:  # noqa: BLE001
            continue


class _HeldPool:
    """A pool this process cannot take a weak reference to.

    asyncpg's ``Pool`` declares ``__slots__`` without ``__weakref__``, so
    ``weakref.ref(pool)`` raises TypeError. Swallowing that meant an asyncpg app
    — the commonest async Postgres app there is — silently reported no pool at
    all: not a bad number, no reading, indistinguishable from an app with no
    pool. A strong reference is the honest alternative, and the cost is bounded
    on both sides: only pools a hook POSITIVELY saw are held, the list is capped
    below, and a pool is a process-lifetime object in every real app, so the
    lifetime this extends is one it already had.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def __call__(self) -> Any:
        return self._pool


# Above this many pools we stop remembering new ones. A real app has one or
# two; a list that grew without limit would be a leak wearing a reading's hat.
_MAX_REMEMBERED_POOLS = 8


def _remember_pool(pool: Any) -> None:
    """Retain a non-owning reference to a pool positively seen by a hook."""
    try:
        try:
            ref: Any = weakref.ref(pool)
        except TypeError:
            # Not weak-referenceable (see _HeldPool). Hold it outright rather
            # than lose the reading.
            ref = _HeldPool(pool)
        with _lock:
            if any(existing() is pool for existing in _pool_refs):
                return
            if len(_pool_refs) >= _MAX_REMEMBERED_POOLS:
                return
            _pool_refs.append(ref)
    except Exception:  # noqa: BLE001
        pass


def _make_pool_touch_wrapper(real: Any) -> Any:
    """Remember the receiver, then leave the host method completely unchanged."""
    def observed(receiver: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            _remember_pool(receiver)
        except Exception:  # noqa: BLE001
            pass
        return real(receiver, *args, **kwargs)

    observed._boosthis_db_wrapper = True  # type: ignore[attr-defined]
    return observed


def _install_pool_touches(library: str, owner: Any, names: Tuple[str, ...]) -> bool:
    took = False
    for name in names:
        try:
            original = owner.__dict__.get(name) if hasattr(owner, "__dict__") else None
            if original is None or not callable(original):
                continue
            if getattr(original, "_boosthis_db_wrapper", False):
                took = True
                continue
            took = _swap(
                library, owner, name, original, _make_pool_touch_wrapper(original)
            ) or took
        except Exception:  # noqa: BLE001
            continue
    return took


def _open_query(args: Any, kwargs: Any) -> Tuple[Optional[int], Any]:
    """Our own bookkeeping for one round trip, and NOTHING else — the host's
    arguments are not touched. A query issued from inside another watched query
    takes no identity, so one round trip is never counted twice."""
    try:
        if _in_query.get():
            return None, None
        token = _in_query.set(True)
        return statement_identity(args, kwargs), token
    except Exception:  # noqa: BLE001
        return None, None


def _close_query(token: Any) -> None:
    try:
        if token is not None:
            _in_query.reset(token)
    except Exception:  # noqa: BLE001
        pass


def _loaded_module(name: str) -> Any:
    """The module object the host ALREADY imported, or None. A read of
    ``sys.modules`` and nothing more: no module is imported or resolved to
    answer it."""
    try:
        return sys.modules.get(name)
    except Exception:  # noqa: BLE001
        return None


def _swap(library: str, owner: Any, attr: str, original: Any, wrapper: Any) -> bool:
    """Put ``wrapper`` in place of ``original`` and record BOTH. Recording the
    wrapper is what makes the restore safe: at unpatch time the only honest
    question is "is the callable in place still the one we put there?", and that
    cannot be answered from the original alone."""
    try:
        setattr(owner, attr, wrapper)
    except Exception:  # noqa: BLE001
        return False  # a C extension type: this method goes unwatched
    _patches.append((library, owner, attr, original, wrapper))
    return True


def _install_methods(library: str, owner: Any, names: Tuple[str, ...]) -> bool:
    """Wrap every named method on ``owner`` that we can. True when at least one
    took: a driver we can partly watch is still watched, and the methods that
    would not take are simply never observed."""
    took = False
    for name in names:
        try:
            original = owner.__dict__.get(name) if hasattr(owner, "__dict__") else None
            if original is None:
                original = getattr(owner, name, None)
            if original is None or not callable(original):
                continue
            if getattr(original, "_boosthis_db_wrapper", False):
                # Already ours, from an arming whose bookkeeping was lost.
                # Wrapping our own wrapper would count every call twice.
                took = True
                continue
            is_async = False
            try:
                import inspect

                is_async = inspect.iscoroutinefunction(original)
            except Exception:  # noqa: BLE001
                is_async = False
            wrapper = (
                _make_observed_async_query(original)
                if is_async
                else _make_observed_query(original)
            )
            if _swap(library, owner, name, original, wrapper):
                took = True
        except Exception:  # noqa: BLE001
            continue
    return took


def _install_psycopg() -> bool:
    """Watch psycopg 3 at its cursor's own round-trip layer.

    ``Cursor.execute`` / ``executemany`` is the single point every plain query
    AND every SQLAlchemy or Django ORM query passes through on this driver.
    ``Connection.execute`` is deliberately left alone: it opens a cursor and
    delegates, so watching both would count one query twice.
    """
    mod = _loaded_module("psycopg")
    if mod is None:
        return False
    took = False
    for cls_name in ("Cursor", "ClientCursor", "ServerCursor"):
        cls = getattr(mod, cls_name, None)
        if cls is not None:
            took = _install_methods("psycopg", cls, ("execute", "executemany")) or took
    for cls_name in ("AsyncCursor", "AsyncClientCursor", "AsyncServerCursor"):
        cls = getattr(mod, cls_name, None)
        if cls is not None:
            took = _install_methods("psycopg", cls, ("execute", "executemany")) or took
    if took:
        # psycopg_pool's liveness check runs an empty statement through the very
        # cursor above, on whatever request happens to be borrowing the
        # connection. Driver housekeeping, not the app's query. It lives on the
        # POOL rather than the driver, and the pool is only ever watched if the
        # app imported it — nothing here imports anything.
        pool_mod = _loaded_module("psycopg_pool")
        for owner_name in ("ConnectionPool", "AsyncConnectionPool"):
            owner = getattr(pool_mod, owner_name, None) if pool_mod else None
            if owner is not None:
                _install_housekeeping("psycopg", owner, ("check_connection",))
    return took


def _install_asyncpg() -> bool:
    """Watch asyncpg at its connection's own round-trip methods.

    ``execute``, ``executemany`` and the three fetch helpers are each a distinct
    round trip; none of them is implemented in terms of another public one here,
    and the re-entrancy guard keeps a future version that changes that honest.
    """
    mod = _loaded_module("asyncpg")
    if mod is None:
        return False
    conn_mod = _loaded_module("asyncpg.connection")
    cls = getattr(conn_mod, "Connection", None) if conn_mod else None
    if cls is None:
        cls = getattr(mod, "Connection", None)
    if cls is None:
        return False
    took = _install_methods(
        "asyncpg",
        cls,
        ("execute", "executemany", "fetch", "fetchrow", "fetchval"),
    )
    if took:
        # asyncpg's pool resets every connection as it comes back, and the reset
        # runs a statement of its own through the very method above. It belongs
        # to the driver, not to the request holding the connection.
        _install_housekeeping("asyncpg", cls, ("reset",))
        pool_mod = _loaded_module("asyncpg.pool")
        pool_cls = getattr(pool_mod, "Pool", None) if pool_mod else getattr(mod, "Pool", None)
        if pool_cls is not None:
            _install_pool_touches("asyncpg", pool_cls, ("acquire", "release"))
    return took


_INSTALLERS = {"psycopg": _install_psycopg, "asyncpg": _install_asyncpg}


def _install_pool_observers() -> None:
    """Observe use of already-loaded pools; never imports or constructs one."""
    pool_mod = _loaded_module("psycopg_pool")
    if pool_mod is not None:
        for name in ("ConnectionPool", "AsyncConnectionPool"):
            owner = getattr(pool_mod, name, None)
            if owner is not None:
                _install_pool_touches("psycopg_pool", owner, ("connection", "getconn"))
    base_mod = _loaded_module("sqlalchemy.engine.base")
    engine = getattr(base_mod, "Engine", None) if base_mod else None
    if engine is not None:
        _install_pool_touches("sqlalchemy", engine, ("connect", "begin"))


# ═══════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═══════════════════════════════════════════════════════════════════════════
def arm_db_clients() -> None:
    """Arm database observation, if the host has imported a driver we can watch.

    Called from the request boundary (the same place the outbound observer arms)
    so importing the kit costs nothing until the app actually serves traffic,
    and so a driver imported lazily on the first request is still caught. The
    module-table scan is throttled, and the whole thing is a no-op once every
    known driver is watched. Never raises.
    """
    global _arm_attempted, _active, _last_arm_at
    if is_boosthis_disabled():
        return
    try:
        now = time.monotonic()
        if _arm_attempted and now - _last_arm_at < ARM_RETRY_SEC:
            return
        # One installer at a time; the loser leaves immediately rather than
        # putting our bookkeeping in front of the app's request.
        if not _arm_lock.acquire(blocking=False):
            return
        try:
            if _arm_attempted and now - _last_arm_at < ARM_RETRY_SEC:
                return
            _last_arm_at = now
            _arm_attempted = True
            _active = True
            for name in WATCHABLE_DB_CLIENTS:
                if _installed.get(name):
                    continue
                installer = _INSTALLERS.get(name)
                if installer is None:
                    continue
                _installed[name] = bool(installer())
            _install_pool_observers()
        finally:
            try:
                _arm_lock.release()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass  # arming must never disturb the host


def unpatch_db_clients() -> None:
    """Put back every method we replaced and switch the feed off.

    A method is restored only while OUR wrapper is still the one in place: if a
    host library patched on top of us since, writing back what we captured at
    arming time would silently delete THEIR work, so our wrapper stays where it
    is instead — inert, because the feed is switched off first. Never raises.
    """
    global _active
    _active = False
    try:
        pending, _patches[:] = list(_patches), []
        for library, owner, attr, original, wrapper in pending:
            try:
                if owner.__dict__.get(attr) is wrapper or getattr(owner, attr, None) is wrapper:
                    setattr(owner, attr, original)
            except Exception:  # noqa: BLE001
                pass  # best-effort
            _installed[library] = False
    except Exception:  # noqa: BLE001
        pass


# ═══════════════════════════════════════════════════════════════════════════
# Reading
# ═══════════════════════════════════════════════════════════════════════════
def unwatched_db_client_count() -> int:
    """How many database libraries this app imported that the kit cannot watch.

    A blind spot only exists while the kit is actually watching: it means "we are
    reporting on this app's database work, and some of it we cannot see". So
    whenever observation is not running this reports ZERO rather than a blind
    spot — the axis is then simply absent, which is the honest, pre-existing
    answer.

    Counts only: a library NAME never leaves this process. Never raises.
    """
    if not _arm_attempted or not _active or is_boosthis_disabled():
        return 0
    n = 0
    try:
        for name in WATCHABLE_DB_CLIENTS:
            # Imported but our patch did not take — a watchable driver we are
            # blind to.
            if not _installed.get(name) and _loaded_module(name) is not None:
                n += 1
        for name in UNWATCHABLE_DB_CLIENTS:
            if _loaded_module(name) is not None:
                n += 1
    except Exception:  # noqa: BLE001
        return 0
    return n


def get_db_work_stats() -> Dict[str, Any]:
    """Session totals for the ``dbWork`` axis. Numbers only — field for field
    the shape the Node reader emits."""
    try:
        with _lock:
            return {
                "watchedRequests": _watched_requests,
                "watchedRequestMs": _watched_request_ms,
                "dbMs": _db_ms,
                "callCount": _call_count,
                "hostedCalls": _hosted_calls,
                "repeatWorst": _repeat_worst,
                "repeatRequests": _repeat_requests,
                "repeatMs": _repeat_ms,
                "rowsWorst": _rows_worst,
                "unwatchedClients": unwatched_db_client_count(),
            }
    except Exception:  # noqa: BLE001
        return {
            "watchedRequests": 0,
            "watchedRequestMs": 0.0,
            "dbMs": 0.0,
            "callCount": 0,
            "hostedCalls": 0,
            "repeatWorst": 0,
            "repeatRequests": 0,
            "repeatMs": 0.0,
            "rowsWorst": 0,
            "unwatchedClients": 0,
        }


def get_db_sequencing_stats() -> Dict[str, Any]:
    try:
        with _lock:
            return {
                "waveRequests": _wave_requests,
                "wavesTotal": _waves_total,
                "wavesWorst": _waves_worst,
                "spanMs": _wave_span_ms,
                "sumMs": _wave_sum_ms,
                "unwatchedClients": unwatched_db_client_count(),
            }
    except Exception:  # noqa: BLE001
        return {
            "waveRequests": 0, "wavesTotal": 0, "wavesWorst": 0,
            "spanMs": 0.0, "sumMs": 0.0, "unwatchedClients": 0,
        }


def get_db_row_volume_stats() -> Dict[str, Any]:
    try:
        with _lock:
            return {
                "watchedRequests": _watched_requests,
                "rowsWorst": _rows_worst,
                "rowsSum": _rows_sum,
                "rowsCalls": _rows_calls,
                "unwatchedClients": unwatched_db_client_count(),
            }
    except Exception:  # noqa: BLE001
        return {
            "watchedRequests": 0, "rowsWorst": 0, "rowsSum": 0,
            "rowsCalls": 0, "unwatchedClients": 0,
        }


def get_db_pool_pressure() -> Optional[Dict[str, int]]:
    """Read one live pool's own occupancy counters at snapshot time."""
    try:
        with _lock:
            pools = [ref() for ref in _pool_refs]
            _pool_refs[:] = [ref for ref in _pool_refs if ref() is not None]
        # The most pressured positively-held pool is the conservative reading.
        readings = []
        for pool in pools:
            if pool is None:
                continue
            try:
                if callable(getattr(pool, "get_stats", None)):
                    stats = pool.get_stats()
                    size = int(stats["pool_size"])
                    idle = int(stats["pool_available"])
                    waiting = int(stats.get("requests_waiting", 0))
                elif callable(getattr(pool, "get_size", None)) and callable(
                    getattr(pool, "get_idle_size", None)
                ):
                    size = int(pool.get_size())
                    idle = int(pool.get_idle_size())
                    waiting = None
                else:
                    candidate = getattr(pool, "pool", pool)
                    checkedin = getattr(candidate, "checkedin", None)
                    checkedout = getattr(candidate, "checkedout", None)
                    if not callable(checkedin) or not callable(checkedout):
                        continue
                    idle, busy = int(checkedin()), int(checkedout())
                    size = idle + busy
                    waiting = None
                size, idle = max(0, size), max(0, idle)
                idle = min(idle, size)
                reading = {"busy": size - idle, "idle": idle, "size": size}
                # Unknown waiter occupancy is not zero: only a pool with a real
                # waiter counter may put this field on the wire.
                if waiting is not None:
                    reading["waiting"] = max(0, waiting)
                readings.append(reading)
            except Exception:  # noqa: BLE001
                continue
        if not readings:
            return None
        return max(
            readings,
            key=lambda r: (
                (r["busy"] / r["size"]) if r["size"] else 0.0,
                r.get("waiting", 0),
            ),
        )
    except Exception:  # noqa: BLE001
        return None


def clear_db_work() -> None:
    """Wipe all state and detach every observation (wired into ``forget()`` so
    nothing Boosthis-shaped keeps counting after erasure). Idempotent."""
    global _arm_attempted, _last_arm_at, _watched_requests, _watched_request_ms
    global _db_ms, _call_count, _hosted_calls, _repeat_worst, _repeat_requests
    global _repeat_ms, _rows_worst, _rows_sum, _rows_calls
    global _wave_requests, _waves_total, _waves_worst, _wave_span_ms, _wave_sum_ms
    unpatch_db_clients()
    try:
        with _lock:
            _arm_attempted = False
            _last_arm_at = 0.0
            _watched_requests = 0
            _watched_request_ms = 0.0
            _db_ms = 0.0
            _call_count = 0
            _hosted_calls = 0
            _repeat_worst = 0
            _repeat_requests = 0
            _repeat_ms = 0.0
            _rows_worst = 0
            _rows_sum = 0
            _rows_calls = 0
            _wave_requests = 0
            _waves_total = 0
            _waves_worst = 0
            _wave_span_ms = 0.0
            _wave_sum_ms = 0.0
            _pool_refs.clear()
            _installed.clear()
    except Exception:  # noqa: BLE001
        pass


#: @internal test hooks.
_db_work_internals = {
    "MAX_DISTINCT_STATEMENTS": MAX_DISTINCT_STATEMENTS,
    "MAX_SQL_CHARS": MAX_SQL_CHARS,
    "DB_WAVE_MAX_CALLS": DB_WAVE_MAX_CALLS,
    "make_observed_query": _make_observed_query,
    "make_observed_async_query": _make_observed_async_query,
    "install_methods": _install_methods,
    "statement_identity": statement_identity,
    "row_count_of": _row_count_of,
    "sweep_db_waves": _sweep_db_waves,
    "remember_pool": _remember_pool,
    "is_armed": lambda: _active,
    "is_installed": lambda name: _installed.get(name) is True,
    "set_active": None,  # filled below
}


def _set_active_for_tests(value: bool) -> None:
    """@internal — arm the wrapper feed without a real driver present."""
    global _active, _arm_attempted
    _active = bool(value)
    if value:
        _arm_attempted = True


_db_work_internals["set_active"] = _set_active_for_tests


__all__ = [
    "DbRequestTally",
    "HOSTED_DB_HOST_SUFFIXES",
    "HOSTED_DB_PATH_PREFIXES",
    "UNWATCHABLE_DB_CLIENTS",
    "WATCHABLE_DB_CLIENTS",
    "WATCHABLE_DB_POOLS",
    "arm_db_clients",
    "begin_db_work",
    "clear_db_work",
    "current_db_ms",
    "current_db_scope",
    "end_db_work",
    "get_db_work_stats",
    "get_db_sequencing_stats",
    "get_db_row_volume_stats",
    "get_db_pool_pressure",
    "is_hosted_database_call",
    "is_hosted_db_target",
    "note_db_call",
    "note_db_call_in",
    "note_hosted_db_call",
    "statement_identity",
    "unpatch_db_clients",
    "unwatched_db_client_count",
]
