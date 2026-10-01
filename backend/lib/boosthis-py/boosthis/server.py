"""Local HTTP server — the AI-facing surface.

Zero deps (stdlib ``http.server``). Default port 7787 (or ``$BOOSTHIS_PORT``).
Bind localhost-only by default; the user has to opt in to 0.0.0.0 to expose
beyond their dev environment.

Endpoints — all return JSON unless noted:
  GET  /healthz         · liveness probe
  GET  /context         · full structured context blob (build_context)
  GET  /context.md      · markdown blob (render_markdown_context)
  GET  /rules           · list all rules (id/title/category/languages)
  GET  /rules/<id>      · single rule with fix_template + when_to_apply
  GET  /samples         · recent perf samples (?limit=, ?name=)
  GET  /summary         · p50/p75/p95/p99 + per-route stats
  POST /match           · body {"code": "..."} → ranked rule hits
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from boosthis import samples
from boosthis.checklist import BOOSTHIS_CHECKLIST, get_checklist_entry
from boosthis.community import fetch_rule_fix
from boosthis.context import build_context, match_rules_for_code, render_markdown_context
from boosthis.pii import PIIDetectedError, assert_no_pii

_DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"

logger = logging.getLogger("boosthis.server")

DEFAULT_PORT = int(os.environ.get("BOOSTHIS_PORT") or os.environ.get("BOOSTEN_PORT", "7787"))

# Default to no CORS — the server is localhost-only, so cross-origin browser
# access is opt-in via --cors. Wildcard CORS on a process that holds live perf
# data is a needless attack surface.
_ALLOW_CORS = False

# Per-server app scope. Set by ``serve(app_id=...)`` when the CLI is invoked
# with ``boosthis serve --app <id>``. Surfaced in /healthz so AI tools can
# confirm which app this server is scoped to.
_APP_ID: str | None = None


def _json(body: Any) -> bytes:
    return json.dumps(body, default=str).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "Boosthis/0.1"

    # silence the noisy default request log; we use our own structured logger
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if _ALLOW_CORS:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"")

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)

        if path == "/healthz":
            body: dict[str, Any] = {"ok": True}
            if _APP_ID is not None:
                body["appId"] = _APP_ID
            return self._send(200, _json(body))

        # Dashboard — same HTML the framework adapters serve. Lets a
        # developer who can't mount into their host app (e.g. a CLI/
        # script-only project) still get a real UI by running
        # `boosthis serve` and opening http://127.0.0.1:7787/.
        if path == "/" or path == "/dashboard":
            try:
                html = _DASHBOARD_HTML.read_text(encoding="utf-8")
            except OSError:
                return self._send(500, _json({"error": "dashboard html missing"}))
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        # The dashboard HTML hits `api/<endpoint>` (relative paths), so
        # support both flat (/context) and prefixed (/api/context) shapes.
        if path == "/api/context" or path == "/context":
            limit = int(params.get("limit", ["25"])[0])
            return self._send(200, _json(build_context(sample_limit=limit)))

        if path == "/context.md":
            return self._send(200, render_markdown_context().encode("utf-8"), "text/markdown")

        if path == "/api/rules" or path == "/rules":
            return self._send(
                200,
                _json(
                    [
                        {
                            "id": r["id"],
                            "title": r["title"],
                            "category": r["category"],
                            "languages": r["languages"],
                        }
                        for r in BOOSTHIS_CHECKLIST
                    ]
                ),
            )

        if path.startswith("/api/rules/") or path.startswith("/rules/"):
            rule_id = path.removeprefix("/api/rules/").removeprefix("/rules/")
            rule = get_checklist_entry(rule_id)
            if rule is None:
                return self._send(404, _json({"error": "rule not found", "id": rule_id}))
            # Detection metadata is local; the prescriptive fix is fetched
            # per-rule from the server (invite-key gated). On any failure the
            # merged result carries fix_available:false + a note.
            fix = fetch_rule_fix(rule_id, "py")
            return self._send(200, _json({**rule, **fix}))

        if path == "/api/samples" or path == "/samples":
            limit = int(params.get("limit", ["100"])[0])
            name = params.get("name", [None])[0]
            return self._send(
                200,
                _json([s.to_dict() for s in samples.recent(limit=limit, name=name)]),
            )

        if path == "/api/summary" or path == "/summary":
            return self._send(200, _json(samples.summary()))

        return self._send(404, _json({"error": "not found", "path": path}))

    def do_POST(self) -> None:  # noqa: N802
        path, _, _ = self.path.partition("?")
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return self._send(400, _json({"error": "invalid JSON"}))

        if path == "/api/match" or path == "/match":
            snippet = str(body.get("code") or "")
            lang = str(body.get("language") or "python")
            if not snippet:
                return self._send(400, _json({"error": "missing 'code' field"}))
            return self._send(200, _json(match_rules_for_code(snippet, language=lang)))

        if path == "/sample":
            # Honor the privacy contract on every ingestion path. Scan the FULL
            # incoming body first — a caller could otherwise smuggle extra keys
            # (`email`, `password`, …) past the in-process tracker's narrower
            # `{"screen": name}` check.
            if not isinstance(body, dict):
                return self._send(400, _json({"error": "expected JSON object"}))
            try:
                assert_no_pii(body)
            except PIIDetectedError as exc:
                return self._send(
                    422,
                    _json(
                        {
                            "error": "PII guard rejected sample",
                            "field": exc.field_name,
                            "matched_fragment": exc.matched_fragment,
                        }
                    ),
                )
            try:
                name = str(body["name"])
                duration_ms = int(body["duration_ms"])
                rating = str(body.get("rating") or "good")
            except (KeyError, TypeError, ValueError):
                return self._send(
                    400,
                    _json({"error": "expected {name, duration_ms, rating?}"}),
                )
            if rating not in ("good", "needs-work", "poor"):
                return self._send(400, _json({"error": "invalid rating"}))
            s = samples.record(name, duration_ms, rating)  # type: ignore[arg-type]
            return self._send(201, _json(s.to_dict()))

        return self._send(404, _json({"error": "not found", "path": path}))


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    allow_cors: bool = False,
    app_id: str | None = None,
) -> None:
    """Start the HTTP server and block. SIGINT cleanly stops it.

    ``allow_cors=True`` emits ``Access-Control-Allow-Origin: *`` — opt in only
    if a browser-based AI tool needs to read the surface. The default refuses
    cross-origin access entirely.

    ``app_id`` is an opaque per-app correlation key from the Boosthis mobile
    app's 'Your apps' list. When set, it is echoed in ``/healthz`` so AI
    tools can confirm which app this server is scoped to.
    """
    global _ALLOW_CORS, _APP_ID
    _ALLOW_CORS = allow_cors
    _APP_ID = app_id
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    server = ThreadingHTTPServer((host, port), _Handler)
    if app_id:
        logger.info("Boosthis HTTP server listening on http://%s:%d (app=%s)", host, port, app_id)
    else:
        logger.info("Boosthis HTTP server listening on http://%s:%d", host, port)
    logger.info("  GET  /                · live dashboard (open in a browser)")
    logger.info("  GET  /api/context     · full context blob for AI agents")
    logger.info("  GET  /api/rules       · all %d rules", len(BOOSTHIS_CHECKLIST))
    logger.info("  POST /api/match       · rank rules against a code snippet")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
