"""Portless Boosthis entry point for queue consumers and scheduled workers."""

from __future__ import annotations

import threading
from typing import Any

from boosthis.runtime_flags import is_boosthis_disabled
from boosthis.status_page import StatusFacts, collect_status_facts

DEFAULT_ANNOUNCE_MS = 5_000


def worker_status_text(facts: StatusFacts | None = None) -> str:
    """Render the status page's facts as plain text. Never raises."""
    try:
        f = facts or collect_status_facts()
        verdict = {
            "registered": "registered",
            "unregistered": "NOT registered",
        }.get(f.registration_verdict, "cannot tell right now")
        lines = [f"[boosthis] Worker status: {verdict}."]

        def say(key: str, value: Any) -> None:
            lines.append(f"[boosthis]   {key}: {value}")

        say("Project", f.project_display)
        say("Install ID", f.install_id or "not assigned yet")
        key = f.project_key_display
        say(
            "Project key",
            f"{key} — {f.project_key_source}" if key else f.project_key_source,
        )
        say("Sharing", "on" if f.sharing else "off")
        say("Mode", "full telemetry" if f.full_telemetry else "private (issues only)")
        say("Last upload", f.last_upload_at if f.last_upload_at is not None else "none yet")
        if f.last_upload_fail_at is not None and f.last_upload_fail_reason is not None:
            say(
                "Last upload FAILED",
                f"{f.last_upload_fail_at} — {f.last_upload_fail_reason} "
                f"({f.uploads_dropped} lost so far)",
            )
        if f.rows_dropped > 0 and f.rows_dropped_text:
            say("Measurements dropped", f.rows_dropped_text)
        say("Kit version", f.kit_version)
        say("Runtime", f"{f.runtime} · worker")
        say("Dashboard", f.dashboard_url)
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return (
            "[boosthis] Worker status: cannot tell. The kit could not read its "
            "own state, so nothing here should be taken as working or broken."
        )


def worker(
    *,
    endpoint: str | None = None,
    invite_key: str | None = None,
    share_meter_with_ai: bool | None = None,
    app_name: str | None = None,
    announce_status: bool = True,
    announce_after_ms: float = DEFAULT_ANNOUNCE_MS,
) -> Any:
    """Enable the ordinary registration/upload lifecycle without opening a port."""
    from boosthis import telemetry
    from boosthis.job_adapters import arm_job_systems

    telemetry._set_declared_server_kind("worker")
    config = telemetry.enable_telemetry(
        endpoint=endpoint,
        invite_key=invite_key,
        share_meter_with_ai=share_meter_with_ai,
        app_name=app_name,
    )
    arm_job_systems()
    if announce_status and not is_boosthis_disabled():
        try:
            timer = threading.Timer(
                max(0.0, float(announce_after_ms)) / 1000,
                lambda: print(worker_status_text()),
            )
            # The status convenience must never keep a short-lived worker alive.
            timer.daemon = True
            timer.start()
        except Exception:  # noqa: BLE001
            pass
    return config


# Snake-case twin of Node's boosthisWorker; ``worker`` remains the compact,
# Python-native spelling.
boosthis_worker = worker


__all__ = [
    "worker", "boosthis_worker", "worker_status_text", "DEFAULT_ANNOUNCE_MS"
]