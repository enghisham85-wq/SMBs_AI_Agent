"""The standalone "is Boosthis working?" page — the one surface a back-end
developer can open when their app has no UI at all.

WHY THIS EXISTS. The floating bubble proves the kit is alive by riding along on
a page the app already serves. An API with no HTML has nowhere to put it, so the
developer installs the kit, sees nothing, and reasonably concludes the product
is broken. This page is the answer: one self-contained HTML document, served by
the kit on the HOST APP'S OWN PORT, that states plainly whether the kit
registered, whether anything is being uploaded, and — when the answer is no —
why not.

CONTRACT (identical in the Node, Python, Go, Java, PHP, .NET and Ruby kits —
change it in one, change it in all):
  - Path:    ``<prefix>/status``, canonically ``/_boosthis/status``. GET only.
  - Guard:   the kit's STRICT loopback guard, the same one ``/account`` uses
             (``mount._strict_loopback_allowed``: direct loopback peer AND no
             proxy/forwarding headers). It is NOT opened by
             ``BOOSTHIS_MOUNT_ALLOW_REMOTE``, and it is NOT a new auth surface —
             a remote request gets 403 and learns nothing.
  - Body:    server-rendered HTML with inline CSS, NO JavaScript, NO network
             calls, NO external assets, NO images. The page has to work in
             exactly the situation where everything else is broken.
  - Secrets: the install id is shown (it is an opaque identifier the dashboard
             also shows); the delete token and read token are NEVER rendered
             here. Credentials stay on ``/account``.
  - Honesty: the headline and explanation come from the SAME ordered state
             machine as the bubble's ``compute_panel_notice`` (rejected ->
             not registered -> sharing off -> measuring), with the same wording,
             so a developer never gets two different stories.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import urlsplit

from boosthis.thresholds import RUNTIME_VERSION
from boosthis.project_identity import (
    INSTALL_ID_LABEL,
    INSTALL_ID_UNKNOWN_TEXT,
    PROJECT_LABEL,
    PROJECT_UNKNOWN_TEXT,
    project_display,
)

#: The status-page path suffix, appended to the mount prefix.
STATUS_SUFFIX = "/status"

#: The canonical standalone status page path.
STATUS_PATH = "/_boosthis/status"

#: Human label for this runtime, shown in the page's "Runtime" row.
RUNTIME_LABEL = "Python"

#: The kit option a developer flips to turn sharing on from code. Spelled the
#: way THIS language spells it — the only value in the honesty wording that
#: legitimately differs between kits.
SHARE_OPTION = "share_meter_with_ai=True"

#: The dashboard link used when no telemetry endpoint exists yet / it will not
#: parse.
DEFAULT_DASHBOARD_URL = "https://www.boosthis.com/dashboard"

# Shared palette — the kit's dark theme, byte-equal to the bubble's.
_BG = "#0b0c10"
_CARD = "#15171c"
_BR = "#262932"
_FG = "#e6e7eb"
_MUT = "#8b8f99"
_PRI = "#f97316"
_GOOD = "#4ade80"
_WARN = "#fbbf24"
_BAD = "#f87171"

StatusState = Literal[
    "install-id-not-uuid", "not-registered", "registration-unknown",
    "sharing-off", "uploads-failing", "measuring"
]


@dataclass
class StatusFacts:
    """Everything the page renders. Collected once, then formatted — so tests
    can assert the facts without parsing HTML."""

    state: StatusState
    install_id: Optional[str]
    registration_verdict: Literal["registered", "unregistered", "unknown"]
    registered: bool
    sharing: bool
    full_telemetry: bool
    last_upload_at: Optional[float]
    requests_measured: int
    # Rows the server accepted the batch for and then refused, cumulative for
    # the life of the process. ``rows_dropped_text`` is the ready-to-show figure
    # (count + cause words); both are 0 / "" for a healthy app or an older
    # server, and the page then shows no row at all.
    rows_dropped: int
    rows_dropped_text: str
    kit_version: str
    runtime: str
    dashboard_url: str
    # Never blank: even a platform that declares nothing has a real answer.
    # The warning bit keeps a throwaway deployment's numbers from reading as
    # though they came from the real app.
    hosting_line: str = "Could not be read"
    hosting_is_preview: bool = False
    project_key_display: Optional[str] = None
    project_key_source: str = "no project key configured"
    project_display: str = PROJECT_UNKNOWN_TEXT
    last_upload_fail_at: Optional[float] = None
    last_upload_fail_reason: Optional[str] = None
    uploads_dropped: int = 0
    # Measurements the KIT itself would not upload — a different hole from the
    # rows the server refused, and one no upload reply can ever mention,
    # because these never left the process. Defaulted so every existing caller
    # keeps building: 0 / "" reads as a healthy app and shows no row at all.
    not_uploaded: int = 0
    not_uploaded_text: str = ""
    # WHERE THIS APP PASSES ITS TRACE ON. The kit attaches the trace headers to
    # the app's own outgoing calls so a chain across two services reads as one
    # trace — but only to destinations the customer's setting permits, and the
    # default permits none that could belong to an outside company. Defaulted
    # to the conservative sentence so every existing caller keeps building.
    trace_propagation: str = (
        "Off — no trace headers are attached to outgoing calls"
    )


def dashboard_url_for(endpoint: Optional[str]) -> str:
    """Derive the project dashboard URL from the telemetry endpoint's origin, so
    a self-hosted or staging endpoint links to ITS dashboard rather than lying
    about where this install's data went. Falls back to the public dashboard."""
    try:
        if not endpoint:
            return DEFAULT_DASHBOARD_URL
        parts = urlsplit(endpoint)
        if not parts.scheme or not parts.netloc:
            return DEFAULT_DASHBOARD_URL
        return f"{parts.scheme}://{parts.netloc}/dashboard"
    except Exception:  # noqa: BLE001
        return DEFAULT_DASHBOARD_URL


def collect_status_facts() -> StatusFacts:
    """Read the kit's live state. Fail-safe: any readout error degrades to the
    most conservative honest answer (nothing registered, nothing uploaded)
    rather than throwing into the host's request handling."""
    install_id: Optional[str] = None
    registered = False
    sharing = False
    full_telemetry = False
    last_upload_at: Optional[float] = None
    requests_measured = 0
    rows_dropped = 0
    rows_dropped_text = ""
    not_uploaded = 0
    not_uploaded_text = ""
    # Read outside the big try below: the propagation policy is pure and cannot
    # reach the network, so a readout failure anywhere else on this page must
    # not cost the developer the one row that says what the kit is adding to
    # their outgoing requests.
    propagation_summary = "Off — no trace headers are attached to outgoing calls"
    try:
        from boosthis.trace_propagation import trace_propagation_summary

        propagation_summary = trace_propagation_summary()
    except Exception:  # noqa: BLE001
        pass
    rejected = False
    endpoint: Optional[str] = None
    project_key_display: Optional[str] = None
    project_key_source = "no project key configured"
    project_text = PROJECT_UNKNOWN_TEXT
    verdict: Literal["registered", "unregistered", "unknown"] = "unknown"
    last_upload_fail_at: Optional[float] = None
    last_upload_fail_reason: Optional[str] = None
    uploads_dropped = 0
    last_attempt_failed = False

    try:
        from boosthis import telemetry as tm

        cfg = tm.get_config()
        tm.ask_registration()
        from boosthis.registration import get_registration_verdict
        verdict = get_registration_verdict()
        if cfg is not None:
            install_id = getattr(cfg, "install_id", None) or None
            endpoint = getattr(cfg, "endpoint", None) or None
            # Registered := the server minted a delete token for this install
            # (same definition compute_panel_notice uses).
            registered = bool(getattr(cfg, "delete_token", None))
            if verdict == "unknown" and registered:
                verdict = "registered"
            # Sharing on := the code opt-in OR a dashboard switch — the SAME
            # effective-share gate the snapshot/span uploaders ride.
            sharing = bool(tm._effective_share(cfg))
            # "Full telemetry" mode: the dashboard's per-install override.
            full_telemetry = bool(getattr(cfg, "server_full", False))
        last_upload_at = tm.get_last_upload_at()
        failure = tm.get_last_upload_failure()
        if failure is not None:
            last_upload_fail_at, last_upload_fail_reason = failure
        uploads_dropped = tm.get_dropped_upload_count()
        last_attempt_failed = tm.is_last_upload_attempt_failed()
        rejected = tm.registration_rejected_reason() == "invalid_install_id"
        resolved_key = tm.get_active_project_key()
        project_key_display = resolved_key.display
        from boosthis.project_key import describe_project_key_source
        project_key_source = describe_project_key_source(resolved_key.source)
        project_text = project_display()
    except Exception:  # noqa: BLE001
        # keep the conservative defaults above
        pass

    try:
        from boosthis import samples

        requests_measured = int(samples.summary().get("total", 0))
    except Exception:  # noqa: BLE001
        requests_measured = 0

    # The size of the hole in the dashboard: rows the server refused after
    # accepting the batch. Read from the drop-report ledger, guarded so a
    # readout error degrades to "nothing dropped" rather than throwing.
    try:
        from boosthis import drop_report

        rows_dropped = int(drop_report.get_dropped_row_count())
        rows_dropped_text = drop_report.drop_summary_text()
    except Exception:  # noqa: BLE001
        rows_dropped = 0
        rows_dropped_text = ""

    # The other hole: measurements this kit refused to put on the wire at all
    # (a route name the privacy guard would not pass, a rating the server does
    # not know). Silence here is how a whole class of measurement — a
    # background job named after the work it does, say — becomes a project page
    # that says "registered, measuring nothing" with nothing to read.
    try:
        from boosthis import sample_uploader

        not_uploaded = int(sample_uploader.local_refusal_count())
        not_uploaded_text = sample_uploader.local_refusal_summary()
    except Exception:  # noqa: BLE001
        not_uploaded = 0
        not_uploaded_text = ""

    # A failed hosting read is not evidence that no platform was declared.
    # Keep that case distinct, and never let this diagnostic page become the
    # request failure a developer was trying to diagnose.
    where_it_runs = "Could not be read"
    hosting_is_preview = False
    try:
        from boosthis import hosting

        hosting_details = hosting.hosting_facts()
        where_it_runs = hosting.hosting_line(hosting_details)
        hosting_is_preview = hosting_details.get("environment") in (
            "preview",
            "development",
        )
    except Exception:  # noqa: BLE001
        where_it_runs = "Could not be read"
        hosting_is_preview = False

    # Same order as the bubble's compute_panel_notice: a server-refused id
    # first, then a kit that never registered, then a registered-but-silent
    # install.
    if rejected:
        state: StatusState = "install-id-not-uuid"
    elif not registered:
        state = (
            "not-registered"
            if verdict == "unregistered"
            else "registration-unknown"
        )
    elif not sharing:
        state = "sharing-off"
    elif last_attempt_failed and last_upload_fail_at is not None:
        state = "uploads-failing"
    else:
        state = "measuring"

    return StatusFacts(
        state=state,
        install_id=install_id,
        registration_verdict=verdict,
        registered=registered,
        sharing=sharing,
        full_telemetry=full_telemetry,
        last_upload_at=last_upload_at,
        requests_measured=requests_measured,
        rows_dropped=rows_dropped,
        rows_dropped_text=rows_dropped_text,
        not_uploaded=not_uploaded,
        not_uploaded_text=not_uploaded_text,
        kit_version=RUNTIME_VERSION,
        runtime=RUNTIME_LABEL,
        dashboard_url=dashboard_url_for(endpoint),
        hosting_line=where_it_runs,
        hosting_is_preview=hosting_is_preview,
        project_key_display=project_key_display,
        project_key_source=project_key_source,
        project_display=project_text,
        last_upload_fail_at=last_upload_fail_at,
        last_upload_fail_reason=last_upload_fail_reason,
        uploads_dropped=uploads_dropped,
        trace_propagation=propagation_summary,
    )


def status_headline(state: StatusState) -> tuple[str, str, str]:
    """Return ``(title, body, tone)`` for a state. The wording is the panel
    notice's, word for word, except that the sharing-off line says
    "measurements stay inside this process" instead of "these meters stay on
    this screen" — this page shows no meters, and it must not describe something
    the reader cannot see. ``tone`` is one of ``"good" | "warn" | "bad"``."""
    if state == "install-id-not-uuid":
        return (
            "Registration rejected",
            "Install ID must be a UUID. Mint a real UUID and restart.",
            "bad",
        )
    if state == "not-registered":
        return (
            "Not registered yet",
            "This app has not registered with Boosthis, so nothing is being "
            "sent. Check the project key and this app's outbound network "
            "access, then restart.",
            "bad",
        )
    if state == "registration-unknown":
        return (
            "Can't check right now",
            "Boosthis could not be reached to confirm whether this app is "
            "registered, so this page cannot say either way yet. This is not a "
            "failed install and it does not mean anything stopped — it settles "
            "by itself once the check goes through. Do not change the install "
            "line's id while this is showing.",
            "warn",
        )
    if state == "sharing-off":
        return (
            "Nothing is being uploaded",
            "This app is registered, but sharing is off: measurements stay "
            "inside this process and your Boosthis dashboard stays empty. Turn "
            f"sharing on there, or start the kit with {SHARE_OPTION}.",
            "warn",
        )
    if state == "uploads-failing":
        return (
            "Uploads are not getting through",
            "This app is registered and sharing is on, but the last batch of measurements did not reach Boosthis, so your dashboard is missing the most recent data. The rows below say what happened and how much has been lost.",
            "bad",
        )
    return (
        "Registered and measuring",
        "This app is registered with Boosthis and its measurements are being "
        "uploaded. Open the dashboard below to see them.",
        "good",
    )


def _esc(v: str) -> str:
    """Escape every dynamic value before it reaches the document."""
    return (
        str(v)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def format_last_upload(at: Optional[float], now: float) -> str:
    """``2026-08-20 09:12:44 UTC (34s ago)`` — absolute so it can be compared
    with a server-side log, relative so "is it alive right now?" needs no
    arithmetic. Formatted identically in every kit. ``at`` and ``now`` are
    epoch-milliseconds."""
    if at is None:
        return "Nothing uploaded yet"
    try:
        secs_epoch = float(at) / 1000.0
    except (TypeError, ValueError):
        return "Nothing uploaded yet"
    if secs_epoch != secs_epoch or secs_epoch in (float("inf"), float("-inf")):
        return "Nothing uploaded yet"
    t = time.gmtime(secs_epoch)
    stamp = (
        f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d} "
        f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} UTC"
    )
    secs = max(0, int((now - at) // 1000))
    if secs < 60:
        age = f"{secs}s ago"
    elif secs < 3600:
        age = f"{secs // 60}m ago"
    else:
        age = f"{secs // 3600}h ago"
    return f"{stamp} ({age})"


def _row(label: str, value: str, color: Optional[str] = None) -> str:
    style = f' style="color:{color}"' if color else ""
    return (
        f'<div class="r"><span class="k">{_esc(label)}</span>'
        f'<span class="v"{style}>{_esc(value)}</span></div>'
    )


def upload_fail_text(reason: str) -> str:
    if reason == "unauthorized":
        return "Refused — credentials rejected"
    if reason == "rejected":
        return "Refused — batch rejected"
    if reason == "server-error":
        return "Boosthis failed to store it"
    return "No answer — timed out or unreachable"


def render_status_page(facts: StatusFacts, now: Optional[float] = None) -> str:
    """Render the page. Pure: everything it shows comes from ``facts`` + ``now``
    (epoch-milliseconds)."""
    if now is None:
        now = time.time() * 1000.0
    title, body, tone = status_headline(facts.state)
    if facts.state == "not-registered":
        try:
            from boosthis.telemetry import get_inactive_notice
            body = get_inactive_notice() or body
        except Exception:  # noqa: BLE001
            pass
    tone_color = _GOOD if tone == "good" else _WARN if tone == "warn" else _BAD
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        "<title>Boosthis status</title><style>"
        f":root{{color-scheme:dark}}body{{margin:0;background:{_BG};color:{_FG};"
        "font:14px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;"
        "padding:32px 20px}main{max-width:620px;margin:0 auto}"
        f".kick{{color:{_PRI};font-size:11px;letter-spacing:.16em;font-weight:700}}"
        "h1{font-size:22px;margin:10px 0 8px;font-weight:650}"
        f"p.lead{{color:{_MUT};margin:0 0 22px}}"
        f".card{{background:{_CARD};border:1px solid {_BR};border-radius:16px;padding:6px 16px}}"
        ".r{display:flex;justify-content:space-between;gap:16px;padding:11px 0;"
        f"border-bottom:1px solid {_BR};margin-bottom:0}}"
        ".r:last-child{border-bottom:none}"
        f".k{{color:{_MUT}}}.v{{font-variant-numeric:tabular-nums;text-align:right;word-break:break-all}}"
        f"a.dash{{display:inline-block;margin-top:20px;background:{_PRI};color:#0b0c10;"
        "font-weight:650;text-decoration:none;padding:11px 18px;border-radius:12px}"
        f"footer{{color:{_MUT};font-size:12px;margin-top:22px}}"
        "</style></head><body><main>"
        '<div class="kick">BOOSTHIS</div>'
        f'<h1 style="color:{tone_color}">{_esc(title)}</h1>'
        f'<p class="lead">{_esc(body)}</p>'
        '<div class="card">'
        + _row(PROJECT_LABEL, facts.project_display or PROJECT_UNKNOWN_TEXT)
        + _row(INSTALL_ID_LABEL, facts.install_id or INSTALL_ID_UNKNOWN_TEXT)
        + _row(
            "Registered",
            (
                "Yes"
                if facts.registration_verdict == "registered"
                else "No"
                if facts.registration_verdict == "unregistered"
                else "Can't tell right now"
            ),
            (
                _GOOD
                if facts.registration_verdict == "registered"
                else _BAD
                if facts.registration_verdict == "unregistered"
                else _WARN
            ),
        )
        + _row(
            "Sharing",
            "On" if facts.sharing else "Off",
            _GOOD if facts.sharing else _WARN,
        )
        + _row(
            "Telemetry mode",
            "Full telemetry" if facts.full_telemetry else "Private (issues only)",
        )
        # WHAT THE KIT ADDS TO THIS APP'S OWN OUTGOING REQUESTS. The one row on
        # this page that is not about data leaving for Boosthis: it is about the
        # app's own calls to its own next service, and it is here because a
        # developer must be able to READ what is being attached to their traffic
        # rather than discover it in a packet capture.
        + _row("Trace passed on to", facts.trace_propagation)
        + _row(
            "Project key",
            (
                f"{facts.project_key_display} — {facts.project_key_source}"
                if facts.project_key_display
                else facts.project_key_source
            ),
        )
        + _row("Last upload", format_last_upload(facts.last_upload_at, now))
        + (
            _row("Last upload failed", f"{format_last_upload(facts.last_upload_fail_at, now)} — {upload_fail_text(facts.last_upload_fail_reason)}", _BAD)
            + _row("Uploads lost", str(facts.uploads_dropped), _BAD)
            if facts.last_upload_fail_at is not None and facts.last_upload_fail_reason is not None
            else ""
        )
        # WHERE SCHEDULED JOBS LIVE. Always shown, never conditional: a page
        # that simply omits jobs reads as a page reporting there are none, and
        # an AI agent read three healthy jobs as absent that way. Same answer
        # as the in-app page's jobs card; see
        # docs/decisions/kit-page-says-where-jobs-live.md.
        + _row(
            "Scheduled jobs",
            "Not listed here. This kit reports named job runs to Boosthis, so they appear on your dashboard — this page showing none is not evidence there are none.",
        )
        + _row("Requests measured", str(facts.requests_measured))
        # Only shown when the server has actually refused rows — a healthy app
        # and an older server show no row at all, exactly as before. The text is
        # kit-owned (count + the web page's own cause words), never server prose.
        + (
            _row("Measurements dropped", facts.rows_dropped_text, _WARN)
            if facts.rows_dropped > 0 and facts.rows_dropped_text
            else ""
        )
        # Refused by this kit before the wire, so the server never saw them and
        # no reply can account for them. Shown only when it has happened.
        + (
            _row("Measurements not sent", facts.not_uploaded_text, _WARN)
            if facts.not_uploaded > 0 and facts.not_uploaded_text
            else ""
        )
        + _row("Kit version", facts.kit_version)
        + _row("Runtime", facts.runtime)
        # Every answer is worth showing, including an ordinary server that
        # declares no platform. Amber prevents numbers from a throwaway copy
        # being mistaken for the real app's.
        + _row(
            "Where it runs",
            facts.hosting_line,
            _WARN if facts.hosting_is_preview else None,
        )
        + "</div>"
        + f'<a class="dash" href="{_esc(facts.dashboard_url)}" target="_blank" rel="noopener">'
        + "Open your Boosthis dashboard</a>"
        + "<footer>This page is served by the Boosthis kit inside your own app, on "
        + "your own port. It is only reachable from the machine running this app, "
        + "and it never shows your install token.</footer>"
        + "</main></body></html>"
    )


def status_page_html(now: Optional[float] = None) -> str:
    """Convenience: collect + render in one call. Never raises."""
    try:
        return render_status_page(collect_status_facts(), now)
    except Exception:  # noqa: BLE001
        return render_status_page(
            StatusFacts(
                state="registration-unknown",
                install_id=None,
                registration_verdict="unknown",
                registered=False,
                sharing=False,
                full_telemetry=False,
                last_upload_at=None,
                requests_measured=0,
                rows_dropped=0,
                rows_dropped_text="",
                not_uploaded=0,
                not_uploaded_text="",
                kit_version=RUNTIME_VERSION,
                runtime=RUNTIME_LABEL,
                dashboard_url=DEFAULT_DASHBOARD_URL,
                # A total readout failure says nothing about the platform, so
                # it cannot honestly be called undeclared or production.
                hosting_line="Could not be read",
                hosting_is_preview=False,
                project_key_display=None,
                project_key_source="no project key configured",
                project_display=PROJECT_UNKNOWN_TEXT,
            ),
            now,
        )


#: The body served to a request that did not come from this machine. Same
#: local-only posture as ``/account``, and it discloses nothing about the
#: install (no install id, no version).
STATUS_FORBIDDEN_HTML = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="robots" content="noindex,nofollow">'
    "<title>Boosthis status</title><style>"
    f"body{{margin:0;background:{_BG};color:{_FG};"
    "font:14px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:32px 20px}"
    f"main{{max-width:620px;margin:0 auto}}h1{{font-size:20px;margin:0 0 8px}}p{{color:{_MUT};margin:0}}"
    "</style></head><body><main>"
    "<h1>Boosthis status is local-only</h1>"
    "<p>This page is served only to requests from the machine running this app. "
    "Open it from that machine.</p>"
    "</main></body></html>"
)
