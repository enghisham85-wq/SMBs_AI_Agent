"""boosthis — Performance toolkit for Python.

The Python sibling of `@boosthis/runtime`. Same scoring model, same PII guard,
same checklist concept — exposed as a Python-native decorator + context manager.
"""

from boosthis import job_reporter, sample_uploader, samples, span_emitter
from boosthis.job_reporter import report_job_run
from boosthis.checklist import (
    BOOSTHIS_CHECKLIST,
    CHECKLIST_COUNT,
    CHECKLIST_VERSION,
    get_checklist_entry,
    list_checklist_ids,
)
from boosthis.pii import (
    PII_DENYLIST,
    PIIDetectedError,
    assert_no_pii,
    check_no_pii,
)
from boosthis.score import (
    compute_score,
    get_rating,
    score_metric,
)
from boosthis.thresholds import (
    RATING_CUTOFFS,
    RUNTIME_VERSION,
    SCORE_THRESHOLDS,
    SCORE_WEIGHTS,
)
from boosthis.tracker import (
    PerfResult,
    perf,
    track_perf,
)
from boosthis.job_work import (
    begin_job,
    clear_job_work,
    expect_every,
    get_job_work_stats,
    note_job_system_attached,
    note_job_system_unattached,
    track_job,
    unattached_job_system_count,
)
from boosthis.job_adapters import arm_job_systems, unpatch_job_systems

# A standing promise, written beside the code it is about.
#
# Until this existed a promise could only be typed on the project's promises
# page or written there by a connected AI — never reviewed in the pull request
# that changes the code it describes, and never arriving unless somebody
# remembered the feature existed. ``declare_promise`` gives it the same door
# ``expect_every`` already uses: stated in the app's own source, carried up on
# the job upload the kit already makes.
#
# It states an intention and nothing more. A promise declared here lands
# REMEMBERED ONLY and stays that way until a human confirms the interpretation
# on the project page, exactly like an AI-written one — and a promise that page
# has since edited or confirmed is never overwritten by this call. There is
# deliberately no way to confirm, watch, unwatch or delete one from code: that
# line is what makes "watched" mean something. Anything Boosthis refuses is
# named on stderr, once, rather than failing silently.
from boosthis.promise_declaration import (
    PROMISE_METRICS,
    PROMISE_SUBJECT_KINDS,
    declare_promise,
)
from boosthis.worker import boosthis_worker, worker, worker_status_text
from boosthis.trace import (
    TRACE_HEADER,
    BoosthisTraceMiddleware,
    current_trace_id,
    get_parent_span_id,
    get_span_id,
    get_trace_id,
    is_valid_trace_id,
    new_trace_id,
    read_trace_id,
    sanitize_trace_id,
    set_trace_id,
    trace_headers,
)
from boosthis.span_scope import (
    MAX_ACTIVE_SPANS,
    PARENT_HEADER,
    SPAN_ID_RE,
    SpanHandle,
    adopt_parent_span_id,
    begin_span,
    current_span_id,
    is_valid_span_id,
    new_span_id,
    run_in_span,
    sanitize_span_id,
)
from boosthis.mcp_measure import (
    MCP_MAX_TOOLS,
    is_mcp_path,
    label_for_tool,
    mcp_auto_label,
    mcp_tool,
    record_mcp_sample,
    register_mcp_tools,
)
from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.transmit import safe_transmit
from boosthis.telemetry import (
    disable_telemetry,
    enable_telemetry,
    forget as forget_telemetry,
    get_config as get_telemetry_config,
    is_enabled as telemetry_enabled,
    transmit_samples,
)
from boosthis.mount import mount
from boosthis.route_inventory import (
    MAX_ROUTE_LIST_ENTRIES,
    get_declared_routes,
    register_app as register_route_app,
    register_routes,
    route_list_report,
)
from boosthis.live_detectors import (
    arm_outbound_http_hook,
    disarm_outbound_http_hook,
    record_outbound_attempt,
)
from boosthis.dependency_distance import record_dependency_connection
from boosthis.live_connections import begin_live_connection
from boosthis.snapshot_mirror import read_now

__version__ = RUNTIME_VERSION

__all__ = [
    # tracker
    "track_perf",
    "perf",
    "PerfResult",
    # background work
    "track_job",
    "begin_job",
    "expect_every",
    "get_job_work_stats",
    "clear_job_work",
    "note_job_system_attached",
    "note_job_system_unattached",
    "unattached_job_system_count",
    "arm_job_systems",
    "unpatch_job_systems",
    # a promise written in code
    "declare_promise",
    "PROMISE_SUBJECT_KINDS",
    "PROMISE_METRICS",
    "worker",
    "boosthis_worker",
    "worker_status_text",
    # outbound HTTP observation (repeated-work identity; armed on the first
    # request, reversible — call disarm to opt out). `record_outbound_attempt`
    # is the BY-HAND mark for a call made with a client this kit does not
    # watch; the coverage answer names it as the way out of that gap, so it has
    # to be reachable as `boosthis.record_outbound_attempt`.
    "arm_outbound_http_hook",
    "disarm_outbound_http_hook",
    "record_outbound_attempt",
    # the whole route list — asked of the running framework by mount(), so the
    # map shows routes traffic has never reached. `register_routes` still
    # declares by hand and the two lists merge; `register_route_app` is for an
    # app this kit measures without mounting on.
    "register_routes",
    "get_declared_routes",
    "register_route_app",
    "route_list_report",
    "MAX_ROUTE_LIST_ENTRIES",
    "record_dependency_connection",
    "begin_live_connection",
    # TAKE A READING NOW, from an app that has served nothing yet. Reachable as
    # `boosthis.read_now()` because a developer who has just installed the kit
    # and is staring at an empty dashboard should not have to find a submodule.
    # See docs/on-demand-reading-contract.md.
    "read_now",
    # full-stack trace tag (Stage 1: propagation)
    "TRACE_HEADER",
    "BoosthisTraceMiddleware",
    "current_trace_id",
    "get_span_id",
    "get_parent_span_id",
    "get_trace_id",
    "is_valid_trace_id",
    "new_trace_id",
    "read_trace_id",
    "sanitize_trace_id",
    "set_trace_id",
    "trace_headers",
    # span parentage
    "PARENT_HEADER",
    "SPAN_ID_RE",
    "MAX_ACTIVE_SPANS",
    "SpanHandle",
    "is_valid_span_id",
    "new_span_id",
    "sanitize_span_id",
    "adopt_parent_span_id",
    "current_span_id",
    "begin_span",
    "run_in_span",
    # scoring
    "compute_score",
    "get_rating",
    "score_metric",
    "SCORE_THRESHOLDS",
    "SCORE_WEIGHTS",
    "RATING_CUTOFFS",
    # pii / transmit
    "assert_no_pii",
    "check_no_pii",
    "safe_transmit",
    "PII_DENYLIST",
    "PIIDetectedError",
    # kill-switch
    "is_boosthis_disabled",
    # telemetry (opt-in)
    "enable_telemetry",
    "disable_telemetry",
    "forget_telemetry",
    "get_telemetry_config",
    "telemetry_enabled",
    "transmit_samples",
    # checklist
    "BOOSTHIS_CHECKLIST",
    "CHECKLIST_COUNT",
    "CHECKLIST_VERSION",
    "get_checklist_entry",
    "list_checklist_ids",
    # AI-facing
    "samples",
    # full-stack trace (Stage 2: span emission)
    "sample_uploader",
    "span_emitter",
    # scheduled jobs — the app telling us its background work ran
    "job_reporter",
    "report_job_run",
    # MCP per-tool measurement
    "mcp_tool",
    "register_mcp_tools",
    "mcp_auto_label",
    "label_for_tool",
    "record_mcp_sample",
    "is_mcp_path",
    "MCP_MAX_TOOLS",
    # in-app dashboard
    "mount",
    # meta
    "RUNTIME_VERSION",
    "__version__",
]
