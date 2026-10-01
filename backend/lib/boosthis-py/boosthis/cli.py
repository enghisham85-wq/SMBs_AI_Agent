"""`boosthis` CLI — Python edition.

Mirrors the JS CLI commands::

    boosthis list                Print all rule IDs and titles
    boosthis show <id>           Full rule detail
    boosthis fix --finding <id>  LLM-ready patch prompt
    boosthis version             Print package version
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from boosthis.checklist import (
    BOOSTHIS_CHECKLIST,
    CHECKLIST_COUNT,
    CHECKLIST_VERSION,
    get_checklist_entry,
    list_checklist_ids,
)
from boosthis.thresholds import RUNTIME_VERSION
from boosthis.community import fetch_rule_fix


def _cmd_list(_: argparse.Namespace) -> int:
    by_category: dict[str, list] = {}
    for entry in BOOSTHIS_CHECKLIST:
        by_category.setdefault(entry["category"], []).append(entry)
    for category in ("case-study", "industry", "operational"):
        rules = by_category.get(category, [])
        if not rules:
            continue
        print(f"\n  {category.upper()}  ({len(rules)})")
        for r in rules:
            print(f"    · {r['id']:<40} {r['title']}")
    print(f"\n  {CHECKLIST_COUNT} rules total  ·  v{CHECKLIST_VERSION}\n")
    return 0


def _cmd_show(ns: argparse.Namespace) -> int:
    entry = get_checklist_entry(ns.rule_id)
    if entry is None:
        print(f"error: no rule with id '{ns.rule_id}'", file=sys.stderr)
        print(f"hint:  boosthis list", file=sys.stderr)
        return 1
    print(f"\n  {entry['title']}")
    print(f"  id:        {entry['id']}")
    print(f"  category:  {entry['category']}")
    print(f"  languages: {', '.join(entry['languages'])}")
    print(f"\n  WHEN TO APPLY")
    print(f"    {entry['when_to_apply']}")
    # The prescriptive fix is NOT bundled in this package — fetch it per-rule
    # from the server (invite-key gated). Detection above always works offline.
    fix = fetch_rule_fix(ns.rule_id, "py")
    if fix.get("fix_available"):
        print(f"\n  FIX TEMPLATE")
        print(f"    {fix['fix_template']}")
        if fix.get("community_proven"):
            print(
                f"    (community-proven across {fix.get('community_projects', 0)} "
                f"project(s))"
            )
    else:
        print(f"\n  FIX TEMPLATE")
        print(f"    (unavailable) {fix.get('fix_note', '')}")
    if entry["evidence"]:
        print(f"\n  EVIDENCE")
        for e in entry["evidence"]:
            print(f"    · {e}")
    print()
    return 0


def _cmd_fix(ns: argparse.Namespace) -> int:
    entry = get_checklist_entry(ns.finding)
    if entry is None:
        print(f"error: no rule with id '{ns.finding}'", file=sys.stderr)
        return 1
    file_hint = f"\n\nFILE: {ns.file}" if ns.file else ""
    # The prescriptive fix is fetched per-rule from the server (invite-key
    # gated); it is not bundled in this package. Detection still works offline.
    fix = fetch_rule_fix(ns.finding, "py")
    if not fix.get("fix_available"):
        print(
            f"error: the fix template for '{entry['id']}' is served per-rule "
            f"from the Boosthis server and is unavailable.\n  {fix.get('fix_note', '')}",
            file=sys.stderr,
        )
        return 1
    print(
        f"""You are reviewing Python code for a Boosthis performance rule.

RULE: {entry["title"]}
ID: {entry["id"]}

WHEN TO APPLY:
{entry["when_to_apply"]}

FIX TEMPLATE:
{fix["fix_template"]}{file_hint}

TASK: Identify any code that matches the WHEN TO APPLY condition above,
then propose a minimal diff that follows the FIX TEMPLATE. Preserve all
unrelated behaviour. Return the diff in unified format."""
    )
    return 0


def _cmd_version(_: argparse.Namespace) -> int:
    print(f"boosthis {RUNTIME_VERSION}  ·  checklist v{CHECKLIST_VERSION} ({CHECKLIST_COUNT} rules)")
    return 0


def _cmd_verify(ns: argparse.Namespace) -> int:
    """Run a self-test: imports, PII guard, samples buffer, MCP module."""
    checks: list[tuple[str, bool, str]] = []

    def ok(label: str, fn):
        try:
            detail = fn() or ""
            checks.append((label, True, str(detail)))
        except Exception as exc:  # pragma: no cover - diagnostic surface
            checks.append((label, False, f"{type(exc).__name__}: {exc}"))

    ok("package importable", lambda: f"boosthis v{RUNTIME_VERSION}")
    ok("checklist loaded", lambda: f"{CHECKLIST_COUNT} rules, v{CHECKLIST_VERSION}")

    def _samples_check():
        from boosthis import samples as _s
        before = len(_s.recent(limit=1))
        _s.record("__verify__", 1, "good")
        after = len(_s.recent(limit=1, name="__verify__"))
        assert after >= 1, "sample did not land in buffer"
        return f"buffer responsive (had {before} samples)"

    ok("samples buffer", _samples_check)

    def _pii_check():
        from boosthis.pii import check_no_pii
        bad = check_no_pii({"email": "x@y.com"})
        good = check_no_pii({"name": "GET /x", "duration_ms": 10})
        assert bad is not None, "guard failed to detect known-bad payload"
        assert good is None, "guard false-positived on benign payload"
        return "blocks PII, allows perf samples"

    ok("PII guard", _pii_check)

    def _mcp_check():
        from boosthis import mcp as _m
        assert hasattr(_m, "_sanitize_for_mcp"), "MCP sanitizer missing"
        sample = _m._sanitize_for_mcp("/x/<|im_start|>evil<|im_end|>")
        assert "[redacted]" in sample, "sanitizer did not redact injection marker"
        return "sanitizer redacts prompt-injection markers"

    ok("MCP module", _mcp_check)

    def _budgets_check():
        from boosthis import budgets as _b
        statuses = _b.all_statuses()
        return f"{len(statuses)} route(s) tracked"

    ok("budgets module", _budgets_check)

    from hashlib import sha256
    from boosthis import telemetry as _tm
    from boosthis.project_key import describe_project_key_source, resolve_project_key

    cfg = _tm.get_config()
    resolved_key = resolve_project_key(
        configured=cfg.invite_key if cfg is not None else None
    )
    fingerprint = (
        sha256(resolved_key.key.encode()).hexdigest()[:10]
        if resolved_key.key
        else "none"
    )
    key_detail = (
        f"{resolved_key.display} · {describe_project_key_source(resolved_key.source)} "
        f"· fingerprint {fingerprint}"
    )
    expected = getattr(ns, "project_key", None)
    key_matches = not expected or resolved_key.key == expected
    checks.append(("project key", key_matches, key_detail))

    print()
    print("  Boosthis self-test")
    print("  -----------------")
    width = max(len(lbl) for lbl, _, _ in checks)
    for label, passed, detail in checks:
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}]  {label:<{width}}  {detail}")
    print()
    failed = [lbl for lbl, p, _ in checks if not p]
    if failed:
        print(f"  {len(failed)} check(s) failed: {', '.join(failed)}")
        return 1
    print("  All checks passed.")
    return 0


def _cmd_export(ns: argparse.Namespace) -> int:
    """Dump in-process samples to stdout as JSON or CSV."""
    from boosthis import samples as _s

    rows = [s.to_dict() for s in _s.recent(limit=ns.limit)]
    if ns.format == "json":
        import json

        print(json.dumps(rows, indent=2))
        return 0
    # CSV
    import csv

    writer = csv.writer(sys.stdout)
    writer.writerow(["name", "duration_ms", "rating", "timestamp_ms"])
    for r in rows:
        writer.writerow([r["name"], r["duration_ms"], r["rating"], r["timestamp_ms"]])
    return 0


def _cmd_budgets(_: argparse.Namespace) -> int:
    """Print learned perf budgets per route."""
    from boosthis import budgets as _b

    statuses = _b.all_statuses()
    if not statuses:
        print("  No samples recorded yet. Run your app, then re-run `boosthis budgets`.")
        return 0
    print()
    # Name the algorithm and the rule above the rows. The same question is
    # answered by a server-computed window on the phone and browser kits, so
    # a reader must not have to infer which one replied, nor over what span.
    print(f"  Algorithm: {_b.BUDGET_ALGORITHM} (this process's own sample ring)")
    print(
        f"  Baseline: {_b.BUDGET_BASELINE_N} samples per route after "
        f"{_b.BUDGET_WARMUP_N} warm-up samples, frozen — compared against "
        f"{_b.BUDGET_RECENT_N} taken strictly later. The two windows share no sample."
    )
    print()
    print("  Route                                     State        Baseline p95   Recent p95")
    print("  " + "-" * 80)
    for s in statuses:
        route = s["route"][:40]
        state = s["state"]
        baseline = s.get("baseline_p95_ms", "—")
        recent = s.get("recent_p95_ms", "—")
        if state in ("learning", "evicted"):
            extra = f"({s['samples_seen']}/{s['samples_needed']} samples)"
            print(f"  {route:<40}  {state:<10}   {extra}")
        else:
            print(f"  {route:<40}  {state:<10}   {str(baseline)+' ms':<14} {str(recent)+' ms'}")
    print()
    # Every abstention says WHY and what would change it, once per distinct
    # reason rather than once per route.
    for reason in dict.fromkeys(
        s["explanation"] for s in statuses if s["state"] in ("learning", "evicted")
    ):
        print(f"  {reason}")
        print()
    regressed = [s for s in statuses if s["state"] == "regressed"]
    if regressed:
        print(f"  {len(regressed)} route(s) regressed since baseline.")
        return 1
    return 0


def _cmd_context(ns: argparse.Namespace) -> int:
    # Lazy import: keeps `boosthis list/show/fix` fast (no http/json imports)
    from boosthis.context import build_context, render_markdown_context

    if ns.format == "json":
        import json

        print(json.dumps(build_context(), default=str, indent=2))
    else:
        print(render_markdown_context())
    return 0


_CONSENT_BANNER = (
    "boosthis: this server exposes local performance data (route names, "
    "durations, ratings, recent samples) to any tool that connects. "
    "AI-generated fixes derived from this data are suggestions — review "
    "before merging. See https://www.boosthis.com/privacy."
)


def _cmd_serve(ns: argparse.Namespace) -> int:
    from boosthis.server import DEFAULT_PORT, serve

    print(_CONSENT_BANNER, file=sys.stderr)
    # SAY WHAT THIS COMMAND IS NOT.
    #
    # `boosthis.mount()` used to send developers here when it could not attach
    # to their framework, calling it a "fallback" — and a reader who took that
    # as the way to get their app measured spent a cycle finding out it is a
    # separate server that measures nothing of theirs. The mount error no
    # longer offers it that way; this line makes the same fact true from the
    # other end, for anyone who arrives here from an older kit, an older
    # transcript, or a guess. It names no step the reader may have just
    # failed: the exit it points at (track_perf / perf) works in any Python
    # program.
    print(
        "boosthis serve: a SEPARATE Boosthis-only server, on its own port. It "
        "shows the rule checklist and whatever this machine has already "
        "recorded — it does not host your app, sees none of its requests, and "
        "measures nothing by itself. An app is measured from inside its own "
        "process: @track_perf / perf name the work you care about and need no "
        "framework support.",
        file=sys.stderr,
    )
    if getattr(ns, "host", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"boosthis: WARNING — binding to {ns.host} exposes the dashboard "
            f"beyond localhost. Anyone who can reach this host/port can read "
            f"your local perf data.",
            file=sys.stderr,
        )
    serve(
        host=ns.host,
        port=ns.port or DEFAULT_PORT,
        allow_cors=ns.cors,
        app_id=getattr(ns, "app", None),
    )
    return 0


def _cmd_mcp(ns: argparse.Namespace) -> int:
    from boosthis.mcp import serve_stdio

    print(_CONSENT_BANNER, file=sys.stderr)
    serve_stdio(app_id=getattr(ns, "app", None))
    return 0


_REPLIT_MARK_BEGIN = "<!-- BOOSTHIS:BEGIN"
_REPLIT_MARK_END = "<!-- BOOSTHIS:END -->"


def _cmd_telemetry(ns: argparse.Namespace) -> int:
    """Manage opt-in telemetry: enable / disable / forget / status."""
    from boosthis import telemetry as tm

    action = ns.action
    if action == "status":
        cfg = tm.get_config()
        if cfg is None:
            print("telemetry: never enabled")
            print(f"config file: {tm.CONFIG_PATH} (does not exist)")
            return 0
        state = "ENABLED" if cfg.enabled else "disabled"
        print(f"telemetry: {state}")
        print(f"install id: {cfg.install_id}")
        print(f"endpoint:   {cfg.endpoint}")
        print(f"consent at: {cfg.consent_at}")
        return 0

    if action == "enable":
        print("By enabling telemetry you agree to send anonymous performance samples")
        print("to the Boosthis server. See https://www.boosthis.com/privacy for the")
        print("full policy. No PII, no IPs, no source code — just route name +")
        print("duration + rating. Run `boosthis telemetry forget` to delete everything.")
        if not ns.yes:
            ans = input("Continue? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("aborted")
                return 1
        cfg = tm.enable_telemetry(endpoint=ns.endpoint, invite_key=ns.invite_key)
        print(f"telemetry enabled. install id: {cfg.install_id}")
        return 0

    if action == "disable":
        tm.disable_telemetry()
        print("telemetry disabled. Install id retained — re-enable any time.")
        return 0

    if action == "forget":
        deleted = tm.forget()
        if deleted:
            print("forget request sent. local config and remote samples deleted.")
        else:
            print("nothing to forget — telemetry was never enabled.")
        return 0

    if action == "connect-ai":
        from boosthis.mount import build_connect_payload

        payload = build_connect_payload(tm.get_config())
        if not payload.get("connected"):
            print("Not connected yet. To let your AI read this app's live perf:")
            print("  1. Enable telemetry with a project key:")
            print("     boosthis telemetry enable --project-key <YOUR_PROJECT_KEY>")
            print("  2. Run this command again once the server has issued a read token.")
            return 1
        print("Paste this into your AI tool's MCP config (replace <YOUR_PROJECT_KEY>):\n")
        print(payload["mcp_config"])
        print("\nThen give your AI these as tool arguments (read-only — this app only):")
        print(f"  install_id: {payload['install_id']}")
        print(f"  read_token: {payload['read_token']}")
        return 0

    print(f"unknown telemetry action: {action}")
    return 2


def _cmd_feedback(ns: argparse.Namespace) -> int:
    """Inspect the AI → Boosthis feedback corpus.

    This is the human side of the learning loop: AI agents call the
    `record_fix_outcome` / `report_unmatched_pattern` /
    `suggest_rule_improvement` MCP tools; we read what they wrote so we
    can turn it into better rules.
    """
    from boosthis import feedback as fb

    action = ns.action
    if action == "status":
        s = fb.summary()
        print(f"feedback file: {fb.FEEDBACK_PATH}")
        if s["total"] == 0:
            print("no events recorded yet")
            return 0
        print(f"total events:  {s['total']}")
        print(f"  fix outcomes:       {s['by_kind']['fix_outcome']:>4}  "
              f"(helpful {s['fix_outcomes']['helpful']} · unhelpful {s['fix_outcomes']['unhelpful']})")
        print(f"  unmatched patterns: {s['by_kind']['unmatched_pattern']:>4}")
        print(f"  rule improvements:  {s['by_kind']['rule_improvement']:>4}")
        if s["by_rule"]:
            print("\nby rule:")
            for rule_id, counts in sorted(s["by_rule"].items()):
                print(f"  {rule_id:<40} helpful {counts['helpful']:>3} · unhelpful {counts['unhelpful']:>3}")
        return 0

    if action == "tail":
        events = fb.recent(limit=ns.limit, kind=ns.kind)
        if not events:
            print("no events" + (f" of kind '{ns.kind}'" if ns.kind else ""))
            return 0
        import json as _json
        for e in events:
            print(_json.dumps(e.to_dict(), default=str))
        return 0

    if action == "clear":
        if not ns.yes:
            ans = input(f"delete {fb.FEEDBACK_PATH}? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("aborted")
                return 1
        fb.clear()
        print("feedback cleared")
        return 0

    print(f"unknown feedback action: {action}", file=sys.stderr)
    return 2


def _cmd_snapshot(ns: argparse.Namespace) -> int:
    import json
    from boosthis import snapshots

    action = ns.action
    try:
        if action == "save":
            p = snapshots.save(ns.name)
            print(f"saved snapshot {ns.name!r} ({p['summary']['total']} samples)")
            return 0
        if action == "list":
            rows = snapshots.list_snapshots()
            if not rows:
                print("no snapshots yet — run `boosthis snapshot save <name>` first.")
                return 0
            for r in rows:
                print(f"  {r['name']:24s}  {r['total']:>6d} samples")
            return 0
        if action == "show":
            print(json.dumps(snapshots.load(ns.name), indent=2))
            return 0
        if action == "diff":
            d = snapshots.diff(ns.a, ns.b)
            print(f"diff {ns.a!r} → {ns.b!r}")
            print(f"  p50 delta: {d['p50_delta_ms']}  p95 delta: {d['p95_delta_ms']}  p99 delta: {d['p99_delta_ms']}")
            for row in d["rows"]:
                verdict = row["verdict"]
                tag = {"regressed": "REGRESSED", "improved": "IMPROVED",
                       "stable": "stable", "new": "NEW", "gone": "GONE"}.get(verdict, verdict)
                a = row.get("max_ms_a"); b = row.get("max_ms_b")
                print(f"  [{tag:>9}] {row['route']:40s}  {a if a is not None else '-':>6} → {b if b is not None else '-':>6} ms")
            return 0
        if action == "delete":
            removed = snapshots.delete(ns.name)
            print("removed." if removed else "no such snapshot.")
            return 0
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr); return 2
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr); return 2
    return 2


def _cmd_propose_rule(ns: argparse.Namespace) -> int:
    from boosthis import contribute

    try:
        payload = contribute.scaffold(
            rule_id=ns.rule_id,
            title=ns.title,
            when_to_apply=ns.when_to_apply,
            fix_template=ns.fix_template,
            runtime=ns.runtime,
            evidence=ns.evidence,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    path = Path(os.path.expanduser("~/.boosthis/proposed-rules")) / f"{payload['id']}.json"
    print(contribute.PR_INSTRUCTIONS.format(path=path))
    return 0


def _cmd_init(ns: argparse.Namespace) -> int:
    """Wire Boosthis into a Replit project: write/refresh the replit.md block."""
    from boosthis.context import render_markdown_context

    target = Path(ns.target or "replit.md").resolve()
    block = render_markdown_context().rstrip() + "\n"

    if not target.exists():
        target.write_text(block)
        print(f"created {target}")
    else:
        existing = target.read_text()
        if _REPLIT_MARK_BEGIN in existing and _REPLIT_MARK_END in existing:
            updated = re.sub(
                rf"{re.escape(_REPLIT_MARK_BEGIN)}.*?{re.escape(_REPLIT_MARK_END)}",
                block.rstrip(),
                existing,
                count=1,
                flags=re.DOTALL,
            )
            target.write_text(updated)
            print(f"refreshed Boosthis block in {target}")
        else:
            separator = "" if existing.endswith("\n") else "\n"
            target.write_text(existing + separator + "\n" + block)
            print(f"appended Boosthis block to {target}")

    # Wire an invite key, if supplied, into ~/.boosthis/config.json (private;
    # never written into replit.md). No network call — telemetry stays off
    # until the user explicitly opts in.
    invite_key = getattr(ns, "invite_key", None)
    if invite_key:
        from boosthis import telemetry as tm

        tm.set_invite_key(invite_key)
        print()
        print(f"→ Project key wired into {tm.CONFIG_PATH} (full scope: register + fetch fixes).")
        print("  Telemetry stays OFF until you run `boosthis telemetry enable`.")
        print()
        print("→ For the MCP fix-fetch server, use a FIX-SCOPED key instead — it can")
        print("  fetch per-rule fixes but cannot register installs. Mint one:")
        print("    curl -sX POST https://www.boosthis.com/api/keys \\")
        print("      -H 'content-type: application/json' -d '{\"scope\":\"fix\"}'")
        print("  Then expose it to the MCP server:")
        print("    export BOOSTHIS_INVITE_KEY=<fix-scoped-key>")

    # Also drop a hint about the MCP server config
    print()
    print("→ To expose Boosthis to Replit AI as an MCP server, add to your MCP config:")
    print('    { "mcpServers": { "boosthis": { "command": "boosthis", "args": ["mcp"] } } }')
    # The supported set is never typed here — it comes from mount.py, the one
    # place that decides it. Imported at call time: mount.py pulls in the rest
    # of the kit, and `boosthis list/show/fix` must stay fast.
    from boosthis.mount import supported_frameworks_sentence

    print()
    print("→ To get the live perf dashboard inside your own web app, add ONE line")
    print("  next to where you create your app:")
    print("    import boosthis; boosthis.mount(app)")
    print("  Then open http://<your-app>/_boosthis/ in a browser.")
    print(f"  mount() attaches to {supported_frameworks_sentence(False)} — those")
    print("  by name, and nothing else. On any other framework (including a plain")
    print("  WSGI or ASGI app) it refuses rather than attaching to nothing: measure")
    print("  that app with @track_perf / perf, which need no framework support.")
    print("  No web app at all? `boosthis serve` opens a SEPARATE Boosthis-only")
    print("  server on http://127.0.0.1:7787/ that shows what has already been")
    print("  recorded; it measures nothing itself, so it is not a way to attach.")

    # Optional auto-patch — only runs if the user asked for it.
    if getattr(ns, "patch", None):
        return _auto_patch_app_file(Path(ns.patch), dry_run=bool(getattr(ns, "dry_run", False)))
    return 0


# Constructor patterns we recognise as "this is the host app". Order matters —
# more specific first. We intentionally match on common variable assignments
# (`app = FastAPI(...)`, `application = Flask(...)`) instead of doing real
# AST analysis, because that covers ~95% of real-world entry points without
# pulling ast/libcst as a dependency.
_APP_PATTERNS = [
    (re.compile(r"^(\s*)(\w+)\s*(?::\s*\w+\s*)?=\s*FastAPI\("), "FastAPI"),
    (re.compile(r"^(\s*)(\w+)\s*(?::\s*\w+\s*)?=\s*Flask\("), "Flask"),
    (re.compile(r"^(\s*)(\w+)\s*(?::\s*\w+\s*)?=\s*Starlette\("), "Starlette"),
]


def _auto_patch_app_file(path: Path, *, dry_run: bool) -> int:
    """Insert `import boosthis` + `boosthis.mount(<app>)` into ``path``.

    Conservative on purpose: refuses if it can't find a single unambiguous
    constructor, refuses if Boosthis is already mounted, writes a .bak file
    so the user can revert with one shell command.
    """
    if not path.exists():
        print(f"--patch: file not found: {path}")
        return 2
    src = path.read_text()

    if "boosthis.mount(" in src:
        print(f"--patch: {path} already calls boosthis.mount() — nothing to do")
        return 0

    matches: list[tuple[int, str, str, str]] = []  # (line_idx, indent, var_name, framework)
    lines = src.splitlines()
    for i, line in enumerate(lines):
        for pat, framework in _APP_PATTERNS:
            m = pat.match(line)
            if m:
                matches.append((i, m.group(1), m.group(2), framework))
                break

    if not matches:
        # Name the supported set rather than the three constructors this
        # patcher scans for: a reader told only "add the call by hand" on a
        # framework mount() cannot take is being sent at a step that will
        # raise. The set comes from mount.py, never typed here.
        from boosthis.mount import supported_frameworks_sentence

        print(
            f"--patch: couldn't find a FastAPI / Flask / Starlette app in {path}.\n"
            f"  If this app is one of {supported_frameworks_sentence(False)}, add\n"
            f"  `import boosthis; boosthis.mount(app)` by hand next to where the app\n"
            f"  is created. On any other framework mount() refuses — measure that\n"
            f"  app with @track_perf / perf instead, which need no adapter."
        )
        return 2
    if len(matches) > 1:
        names = ", ".join(f"{m[2]}={m[3]}" for m in matches)
        print(
            f"--patch: found multiple candidate apps in {path} ({names}).\n"
            f"  Refusing to guess — add `boosthis.mount(<app>)` by hand."
        )
        return 2

    line_idx, indent, var_name, framework = matches[0]

    # Walk forward from the constructor line tracking paren depth so we can
    # correctly handle multi-line constructors like:
    #   app = FastAPI(
    #       title="x",
    #       version="1",
    #   )
    # The mount call must land AFTER the closing paren — never inside it.
    end_idx = line_idx
    depth = 0
    seen_open = False
    for k in range(line_idx, len(lines)):
        for ch in lines[k]:
            if ch == "(":
                depth += 1
                seen_open = True
            elif ch == ")":
                depth -= 1
        if seen_open and depth == 0:
            end_idx = k
            break
    else:
        print(
            f"--patch: constructor on line {line_idx + 1} of {path} has unbalanced parens — refusing to patch."
        )
        return 2

    # Decide whether the file already has a usable `boosthis` import binding.
    # Match real import statements at line level, ignoring comments. We
    # accept the canonical `import boosthis` (no alias) and `from boosthis ...`
    # which both expose the `boosthis` name. Aliased forms like
    # `import boosthis as bst` deliberately fall through to inserting a fresh
    # canonical import so the generated `boosthis.mount(app)` call resolves.
    canonical_import_re = re.compile(r"^\s*import\s+boosthis(\s+as\s+boosthis)?\s*(#.*)?$")
    from_import_re = re.compile(r"^\s*from\s+boosthis\b")
    has_canonical_import = any(
        canonical_import_re.match(line) or from_import_re.match(line) for line in lines
    )

    if not has_canonical_import:
        # Insert `import boosthis` after the last contiguous header line
        # (imports / blank lines / comments / module docstring lines).
        insert_at = 0
        in_docstring = False
        docstring_quote = ""
        for j, line in enumerate(lines[:line_idx]):
            stripped = line.strip()
            if in_docstring:
                insert_at = j + 1
                if docstring_quote in stripped:
                    in_docstring = False
                continue
            if stripped.startswith(('"""', "'''")):
                quote = stripped[:3]
                # Single-line docstring closes on the same line
                if stripped.count(quote) >= 2 and len(stripped) > 3:
                    insert_at = j + 1
                    continue
                in_docstring = True
                docstring_quote = quote
                insert_at = j + 1
                continue
            if stripped.startswith(("import ", "from ")) or stripped == "" or stripped.startswith("#"):
                insert_at = j + 1
            else:
                break
        lines.insert(insert_at, "import boosthis")
        end_idx += 1  # we shifted the constructor down by one

    mount_line = f"{indent}boosthis.mount({var_name})"
    lines.insert(end_idx + 1, mount_line)

    patched = "\n".join(lines)
    if not patched.endswith("\n") and src.endswith("\n"):
        patched += "\n"

    if dry_run:
        print(f"--- {path} (dry-run, no write) ---")
        print(patched)
        return 0

    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(src)
    path.write_text(patched)
    print(f"--patch: patched {path} (backup at {backup})")
    print(f"         + import boosthis")
    print(f"         + boosthis.mount({var_name})  # right after {framework} constructor")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Handle `boosthis --version` / `boosthis -V` BEFORE subparser dispatch,
    # since argparse subparsers with required=True would error first otherwise.
    raw = list(argv) if argv is not None else None
    raw_args = raw if raw is not None else __import__("sys").argv[1:]
    if any(a in ("--version", "-V") for a in raw_args):
        return _cmd_version(argparse.Namespace())

    parser = argparse.ArgumentParser(prog="boosthis", description="Boosthis — Python performance toolkit")
    parser.add_argument("--version", "-V", action="store_true", help="print package version and exit")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="Print all rule IDs and titles")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="Print a single rule in full")
    p_show.add_argument("rule_id", help="rule id, e.g. n-plus-one-orm-query")
    p_show.set_defaults(func=_cmd_show)

    p_fix = sub.add_parser("fix", help="Emit an LLM-ready patch prompt for a rule")
    p_fix.add_argument("--finding", required=True, help="rule id")
    p_fix.add_argument("--file", default=None, help="optional path to include as file context")
    p_fix.set_defaults(func=_cmd_fix)

    p_ver = sub.add_parser("version", help="Print package version")
    p_ver.set_defaults(func=_cmd_version)

    p_verify = sub.add_parser(
        "verify",
        help="Self-test the install (imports, PII guard, samples, MCP, budgets)",
    )
    p_verify.set_defaults(func=_cmd_verify)
    p_verify.add_argument(
        "--project-key",
        default=None,
        help="expected effective project key (prints only its fingerprint)",
    )

    p_export = sub.add_parser(
        "export",
        help="Dump recorded perf samples to stdout (CSV or JSON)",
    )
    p_export.add_argument(
        "--format",
        choices=("csv", "json"),
        default="csv",
    )
    p_export.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="max samples to dump (newest first). Default 1000.",
    )
    p_export.set_defaults(func=_cmd_export)

    p_bud = sub.add_parser(
        "budgets",
        help="Print learned perf budgets per route (regression detector)",
    )
    p_bud.set_defaults(func=_cmd_budgets)

    p_ctx = sub.add_parser(
        "context",
        help="Print the AI context blob (markdown by default, --format json for JSON)",
    )
    p_ctx.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
    )
    p_ctx.set_defaults(func=_cmd_context)

    p_serve = sub.add_parser("serve", help="Run the local HTTP server (default :7787)")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BOOSTHIS_PORT") or os.environ.get("BOOSTEN_PORT", "7787")),
    )
    p_serve.add_argument(
        "--cors",
        action="store_true",
        help="Allow cross-origin browser access (opt-in; off by default)",
    )
    p_serve.add_argument(
        "--app",
        default=None,
        metavar="APP_ID",
        help="Scope this server to a specific registered app id. The id comes "
        "from the Boosthis mobile app's 'Your apps' list and is surfaced in "
        "logs + the /healthz response so AI tools can confirm scoping.",
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_mcp = sub.add_parser(
        "mcp",
        help="Run the stdio MCP server (point Replit AI / Claude / Cursor at this)",
    )
    p_mcp.add_argument(
        "--app",
        default=None,
        metavar="APP_ID",
        help="Scope this MCP server to a specific registered app id. The id "
        "comes from the Boosthis mobile app's 'Your apps' list and is surfaced "
        "in serverInfo so AI tools can confirm scoping.",
    )
    p_mcp.set_defaults(func=_cmd_mcp)

    p_init = sub.add_parser(
        "init",
        help="Write/refresh the Boosthis block in replit.md so AI agents discover it",
    )
    p_init.add_argument(
        "--target",
        default=None,
        help="Path to the file to update. Defaults to ./replit.md",
    )
    p_init.add_argument(
        "--patch",
        default=None,
        metavar="APP_FILE",
        help="Also auto-insert `import boosthis; boosthis.mount(app)` into the "
        "given Python file (must contain a FastAPI/Flask/Starlette app). "
        "Writes a .bak backup. Use --dry-run to preview.",
    )
    p_init.add_argument(
        "--dry-run",
        action="store_true",
        help="With --patch, print the proposed diff without writing.",
    )
    p_init.add_argument(
        "--project-key",
        "--invite-key",  # back-compat alias for older guides/scripts
        dest="invite_key",
        default=None,
        help="Wire a project key into ~/.boosthis/config.json (no network, "
        "telemetry stays off). Use a fix-scoped key for the MCP server.",
    )
    p_init.set_defaults(func=_cmd_init)

    p_tm = sub.add_parser(
        "telemetry",
        help="Manage opt-in telemetry (default: OFF). See /privacy.",
    )
    p_tm.add_argument(
        "action",
        choices=("status", "enable", "disable", "forget", "connect-ai"),
        help="what to do ('connect-ai' prints a paste-ready hosted-MCP config + "
        "this app's read-only credentials so your own AI can read its live perf)",
    )
    p_tm.add_argument(
        "--endpoint",
        default=None,
        help="override ingest URL (used by 'enable'). Defaults to the public Boosthis server or $BOOSTHIS_INGEST_URL.",
    )
    p_tm.add_argument(
        "--project-key",
        "--invite-key",  # back-compat alias for older guides/scripts
        dest="invite_key",
        default=None,
        help="project key (used by 'enable'). Sent as a Bearer token on consent. Defaults to $BOOSTHIS_INVITE_KEY.",
    )
    p_tm.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the interactive consent prompt (used by 'enable')",
    )
    p_tm.set_defaults(func=_cmd_telemetry)

    p_fb = sub.add_parser(
        "feedback",
        help="Inspect what AI agents have written back via the MCP write tools",
    )
    fb_sub = p_fb.add_subparsers(dest="action", required=True)
    fb_status = fb_sub.add_parser("status", help="Counts + per-rule helpful/unhelpful tallies")
    fb_status.set_defaults(func=_cmd_feedback, action="status")
    fb_tail = fb_sub.add_parser("tail", help="Dump recent events as JSONL (newest first)")
    fb_tail.add_argument("--limit", type=int, default=20)
    fb_tail.add_argument(
        "--kind",
        choices=("fix_outcome", "unmatched_pattern", "rule_improvement"),
        default=None,
    )
    fb_tail.set_defaults(func=_cmd_feedback, action="tail")
    fb_clear = fb_sub.add_parser("clear", help="Delete the local feedback file")
    fb_clear.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    fb_clear.set_defaults(func=_cmd_feedback, action="clear")

    p_snap = sub.add_parser("snapshot", help="Save / compare named perf snapshots")
    snap_sub = p_snap.add_subparsers(dest="action", required=True)
    s_save = snap_sub.add_parser("save", help="Capture current samples to a named snapshot")
    s_save.add_argument("name")
    s_save.set_defaults(func=_cmd_snapshot, action="save")
    s_list = snap_sub.add_parser("list", help="List saved snapshots")
    s_list.set_defaults(func=_cmd_snapshot, action="list")
    s_show = snap_sub.add_parser("show", help="Print a saved snapshot")
    s_show.add_argument("name")
    s_show.set_defaults(func=_cmd_snapshot, action="show")
    s_diff = snap_sub.add_parser(
        "diff",
        help="Compare two snapshots; per-route deltas use max_ms (see snapshots.diff docstring)",
    )
    s_diff.add_argument("a")
    s_diff.add_argument("b")
    s_diff.set_defaults(func=_cmd_snapshot, action="diff")
    s_del = snap_sub.add_parser("delete", help="Remove a saved snapshot")
    s_del.add_argument("name")
    s_del.set_defaults(func=_cmd_snapshot, action="delete")

    p_prop = sub.add_parser(
        "propose-rule",
        help="Scaffold a new Boosthis rule (writes a draft to ~/.boosthis/proposed-rules/)",
    )
    p_prop.add_argument("--id", dest="rule_id", required=True, help="kebab-case rule id")
    p_prop.add_argument("--title", required=True)
    p_prop.add_argument("--when-to-apply", dest="when_to_apply", required=True)
    p_prop.add_argument("--fix-template", dest="fix_template", required=True)
    p_prop.add_argument(
        "--runtime", choices=("python", "react-native", "node"), default="python",
    )
    p_prop.add_argument(
        "--evidence", action="append", default=None,
        help="Real commit/file where you saw this regression. Repeatable.",
    )
    p_prop.set_defaults(func=_cmd_propose_rule)

    args = parser.parse_args(raw)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
