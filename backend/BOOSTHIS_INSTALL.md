A SANDBOX PREVIEW CANNOT SHOW WHAT THIS KIT DOES: a sandboxed preview, canvas, or artifact environment cannot reach what this kit talks to. Those sandboxes have no network and no access to boosthis.com, so the kit always looks broken there — sign-in and telemetry cannot load, hand-copied icon bytes come out scrambled, and the console errors belong to the mock, not the kit. A preview, mock-up or re-implementation shown as "the Boosthis UI" describes the sandbox rather than the product. Three checks say something real: (1) a passing kit verification (verify_kit_install, or the same check over HTTPS when a tool reply is withheld) — which proves the FILES ON DISK and nothing more, never that anything is measured; (2) opening the REAL running host app, with network access, and looking at the kit's own bubble and dashboard there; and (3) asking Boosthis whether this project key has seen a check-in — the only one of the three that proves the install is finished (closing step of this guide). SANDBOX INSTALL CHECK: a blocked or unreachable package registry (registry.npmjs.org, pypi.org, proxy.golang.org, Maven Central, etc.) is almost always a sandboxed environment rather than the developer's real project. Workarounds do not change that: the install only counts in the real project (Replit, Claude Code, Cursor, or their local machine). Even there the install is unfinished until check (3), which answers from inside a sandbox too and names which of the states the project is in.

# Boosthis — Python runtime install

Boosthis is a paid performance product with open signup. This is the
**Python** runtime: it measures your app's real per-route / per-function
latency, rates it against a shared scoring model, and ships a 92-rule Python
performance checklist. A project key issued from your account dashboard is
what this kit needs before it can register, report, or fetch fixes; without
one it measures on-device only. It is a workspace package (not on PyPI); you get it from your own Boosthis account, either as the archive you just unpacked or by value over an AI connection.

## Privacy contract

- Detection runs **entirely in-process**.
- Unregistered installs (no project key) send **nothing**.
- A registered install sends only **code-defined route labels** rated by real
  timings — never user data, query strings, request bodies, headers, or
  source code. Every outbound payload passes the shared PII guard.
- **What the kit reads inside your process.** The kit looks at a short,
  bounded list of things that are not timings, and you are the person who
  decides whether that is acceptable in your project:
  Reading something inside your process and collecting it are two different things, and this is the whole of the first. The one exception is the AI-call meter: to tell repeated prompts apart it reads up to the first 4,096 characters of an outbound request body to an AI provider, inside your own process, and reduces it immediately to a single number — no body text is stored, uploaded or recoverable. Eight other bounded reads work the same way — inside your own process, reduced on the spot to a number, a flag or a label from a fixed list: the same meter reads the provider's reply as it streams back, holding at most 8,192 characters of a partial line at a time, to take the token counts the provider states in it; when a provider refuses a call, the meter takes its own copy of that error reply to say which kind of refusal it was — the Python kit stops accumulating at 8,192 characters, and in Node and the browser the runtime hands the reply over as one string, of which anything longer than 8,192 characters is dropped without being examined; the leak watch reads up to the first 8,192 bytes of an ERROR response your app is about to return, to see whether a stack trace or a secret-shaped string is about to reach one of your users; in the browser, and only if you switch it on, a copy of a JSON answer to one of your app's own calls is read to see whether it is error-shaped despite its 200: the copy is taken only where the server declared a length of 65,536 bytes or less, and because a declared length can understate what arrives, the copy the browser hands back is checked again and dropped unexamined past 65,536 characters; on an MCP endpoint your app serves, up to the first 64 KB of the JSON-RPC request is mirrored as your app reads it, to take the method name out of the envelope — never the arguments, and never a byte of what the app itself receives is changed; the cookie check reads the Set-Cookie headers your app sends — at most 20 per response, and at most 2,048 characters of the attribute tail on each, which is 2,048 bytes in the kits whose strings are bytes — for the Secure, HttpOnly and SameSite flags and the size, never the name and never the value; the Cookie header your users send is not read at all; a fixed list of headers is looked up by name and turned straight into a number or a fixed label: the content type, length and encoding, the Accept header your user's browser sent, whether your reply is marked as a download (Content-Disposition), the cache instructions and validators on your app's own reply (Cache-Control, ETag, Last-Modified), the elapsed-milliseconds offset a previous instrumented hop wrote, the queue-start timestamp a proxy adds, the presence (never the value) of a reverse proxy's forwarding headers (X-Forwarded-For, X-Forwarded-Host, X-Forwarded-Proto, X-Real-IP), a hosting platform's cache verdict, an AI provider's rate-limit headroom, retry delay and service-side processing time, and whether an authorization header or an authentication challenge is present — never its value, and never a header nobody named: the list is the claim, and a kit that starts reading a header outside it fails our own build before it ships; and the query values the kit reads on its way OUT are the ones in URLs the kit itself builds for its own uploads, read key by key so the guard can refuse anything identifier-shaped before it leaves your process; your app's own query strings are cut off before a route label is made and are never read. One bounded read is not a reduction at all, because what it holds is your own page on its way to your own visitor: where the dashboard bubble is switched on, an HTML page is buffered so the one script tag can be written into it — up to 2 MB, 4 MB on .NET, past which the page is passed through untouched. Those bytes are handed back with one script tag added and nothing taken from them. One read is neither narrowed to a label from a fixed list nor limited in length, because the value IS the question being asked of the kit's own endpoint: the kit's own local read endpoints — the panel and the JSON reads it serves under its own path, behind their loopback or key gate — read the query of requests made TO THEM: how many rows to return, and which route name to filter to; the row count becomes a number, the route name is compared, exactly as the caller wrote it and with no limit on its length, against the route names the kit is already holding, so a name matching none of them simply returns nothing; neither value is recorded, and no other query string in your app is read. Nothing checks it against a list first, no cap is put on how long it may be, and nothing is derived from it or kept once the reply has gone. One bounded read does not stay inside your process, and the difference is set out here rather than left to be inferred: on a request arriving at your app, the kits on your servers read two headers for their VALUE rather than for a label — the trace id and the caller's span id that another Boosthis-instrumented service of yours wrote on it. A value is taken only if it is exactly the shape the kits mint (32 hexadecimal characters for the trace, 16 for the span), and anything else is discarded, the trace id replaced with a fresh random one. An adopted id then IS that request's trace id: it is put on the hops your app makes while handling the request, and it is uploaded with that request's timings, which is the only way one action crossing several of your services can be shown as one line rather than several unrelated ones. It is the one thing read here that leaves your process, it is an identifier the kits mint and nothing is derived from it, and no other header's value is read. One read is neither bounded nor reduced to a number, and you ask for it yourself: one endpoint the Python kit's panel serves takes a POST — the rule matcher — and it reads that request's body in full, because the code you paste in IS the question being asked: it is matched against the rule book inside your own process, behind the same loopback or key gate as the rest of the panel, the answer is a list of rule names, and neither the code nor any part of it is stored, logged or uploaded. Apart from a trace id another instrumented service already put on the request, none of what is read here is stored, uploaded or recoverable from what is uploaded, and the guard on the upload path refuses outright any payload carrying a request or response body, a header, a cookie or a query value — and that id is uploaded as a field of its own, never as the header it arrived in.
- `BOOSTHIS_DISABLED=1` silences the entire runtime; `boosthis.forget()`
  erases this install's server data.

## What this kit adds to your app

Decide this before you ship, not after. Two kinds of thing arrive with
an install: something your users can see, and addresses your app starts
answering.

### What your users see

- A floating bubble in the corner of the page: a score, a rating word, and a panel it opens.
- Where: Injected into HTML pages this service itself serves. Pages served by a separate frontend are a different process and get nothing.
- The floating bubble is on by default in every environment, including production. BOOSTHIS_BUBBLE set to 0 hides it and the kit carries on measuring; BOOSTHIS_DISABLED stops the kit altogether. Each name is read from the environment where this runtime has one, and otherwise from a value of the same name on the host.

### What your app starts answering

- `/_boosthis` — The kit's own page: static markup that draws the panel by fetching the panel read below. Both spellings are the same page, because a developer types one or the other and neither habit should 404.
  Who can reach it: anyone who can reach your app.
  Why it is open: It is the page the bubble links to, opened from any page the app serves, so a gate here would 403 a developer whose bubble works. The markup is static — it holds no measurements to protect.
  What it does not contain: No measurements, no route or screen labels, no credential: every number on it arrives afterwards from the panel read.
- `/_boosthis/` — The kit's own page: static markup that draws the panel by fetching the panel read below. Both spellings are the same page, because a developer types one or the other and neither habit should 404.
  Who can reach it: anyone who can reach your app.
  Why it is open: It is the page the bubble links to, opened from any page the app serves, so a gate here would 403 a developer whose bubble works. The markup is static — it holds no measurements to protect.
  What it does not contain: No measurements, no route or screen labels, no credential: every number on it arrives afterwards from the panel read.
- `/_boosthis/pulse` — One coarse reading for the bubble itself: a score, a rating word, and how many samples it came from.
  Who can reach it: anyone who can reach your app.
  Why it is open: The bubble is injected into the app's own pages and polls this from whichever page the visitor is on, so it cannot be limited to local requests without the bubble going blank in production — which is where it is most worth having.
  What it does not contain: No route or screen labels, no durations, no per-route rows, no credential, nothing about a visitor.
- `/_boosthis/panel` — What the bubble's panel draws: per-meter scores, rating words, meter labels and the numeric captions beside them.
  Who can reach it: anyone who can reach your app.
  Why it is open: Same reason as the pulse read above — the panel opens on the visitor's page, not on the developer's machine.
  What it does not contain: No route or screen labels, no URLs, no raw samples, no install token and no account credential. The account read that does carry one is local-only, below.
- `/_boosthis/account` — The sign-in context the bubble's account card needs, including the token that claims this install.
  Who can reach it: local requests only — nothing opens it remotely.
- `/_boosthis/status` — The standalone “is Boosthis working?” page — the surface a back-end service with no UI has instead of a bubble.
  Who can reach it: local requests only — nothing opens it remotely.
- `/_boosthis/dev` — This kit's detailed local view: per-route table, recent samples, the rule checklist and the “Connect your AI” card.
  Who can reach it: local requests only (your `allowRemote` opt-in can open it).
- `/_boosthis/api/context` — The project context an AI client reads, as JSON.
  Who can reach it: local requests only (your `allowRemote` opt-in can open it).
- `/_boosthis/api/rules` — The Python rule checklist, as JSON.
  Who can reach it: local requests only (your `allowRemote` opt-in can open it).
- `/_boosthis/api/rules/` — One rule in full, by id (a prefix route).
  Who can reach it: local requests only (your `allowRemote` opt-in can open it).
- `/_boosthis/api/samples` — Recent individual samples, as JSON.
  Who can reach it: local requests only (your `allowRemote` opt-in can open it).
- `/_boosthis/api/summary` — Per-route timings and ratings, as JSON.
  Who can reach it: local requests only (your `allowRemote` opt-in can open it).
- `/_boosthis/api/match` — A POST read that matches submitted code against the rules.
  Who can reach it: local requests only (your `allowRemote` opt-in can open it).
- `/_boosthis/api/connect` — The install id, a read-only token and a paste-ready AI config.
  Who can reach it: local requests only — nothing opens it remotely.

The same list for every runtime, alongside what each address returns, is
published at https://www.boosthis.com/docs/what-boosthis-adds.

## 0. Check you have the whole kit

These notes reach you two ways and read the same either way: unpacked
from the `boosthis-kit-<runtime>.tar.gz` archive you downloaded from your
Boosthis dashboard, or handed to you by value over an AI connection. Do
the check that matches what you are actually holding — one of them, not
both.

**If you unpacked an archive**, there is a `BOOSTHIS_CHECKSUMS.txt` beside this
file. From the folder you unpacked into, run:

```sh
sha256sum -c BOOSTHIS_CHECKSUMS.txt
```

On macOS without GNU coreutils, `shasum -a 256 -c BOOSTHIS_CHECKSUMS.txt` does the
same thing. Every line must end in `OK`. That is the whole check: it needs
no network, no account and no Boosthis tooling. If a line fails, or the
checksum file is not there, the download was incomplete — fetch the
archive again and unpack it into a clean folder.

**If you received a `files[]` array instead**, compare its length with the
`file_count` the same payload carries. A shorter array means the transport
truncated it: write nothing, and fetch the complete kit from
`kit_download_url` instead.

## 1. Add the files & install the package

Write every file at its **exact** path from the project root, **byte-for-byte**. Reformatting, renaming, reorganizing, merging, splitting or 'improving' a kit file changes its sha256, and so does a run of a formatter or linter: the verify check reports the result as `modified`. Then install it in
editable mode (a virtualenv is recommended). Boosthis Python has **zero
runtime dependencies**.

```sh
pip install -e ./lib/boosthis-py
```

That exposes the `boosthis` import and the `boosthis` CLI.

## 1b. Verify before wiring

A kit file that is not byte-perfect on disk produces wiring that looks
installed and measures nothing, so this check belongs before any change to
the build or app code. Either route below is a complete check on its own,
and at least one of them works in every environment.

**From the unpacked archive — offline, no account needed:**

```sh
sha256sum -c BOOSTHIS_CHECKSUMS.txt
```

`BOOSTHIS_CHECKSUMS.txt` lists every other file the archive carries, so a
run with no `FAILED` and no missing-file line is a complete pass. If it
names a file, unpack the archive over the same folder again and re-run it.
(macOS without GNU coreutils: `shasum -a 256 -c BOOSTHIS_CHECKSUMS.txt`.)

**Over an AI connection, where there is one:** the `verify_kit_install`
tool takes the sha256 of each written file as
`{ runtime:"python", files:[{path,sha256},…] }`, and the same JSON POSTed to
`https://www.boosthis.com/api/kit/python/verify` answers identically. Only a `pass` verdict
says the files are the ones this server shipped. A `fail` names the exact
files in `missing` / `modified`; rewritten from the kit, they verify.

## 2. Measure hot paths

Use the decorator for whole functions or the context manager for a block:

```py
from boosthis import track_perf, perf

@track_perf('checkout')
def checkout(cart):
    ...

def handler():
    with perf('db.query'):
        run_query()
```

## 3. Mount the kit (web apps)

One call serves the in-app page at `/_boosthis` — the same page every
Boosthis kit serves, in every language — AND starts timing the app's own unit
of work. This kit's detailed local view (per-route table, recent samples, the
rule checklist and the "Connect your AI" card) is one level down at
`/_boosthis/dev` and stays localhost-only.

The two halves are installed together and cannot come apart. If either one
cannot go in, neither is left installed and the kit says so once on stderr,
naming the missing half — so a project can never end up with a panel and no
measurements.

**Six Python surfaces are supported**, each with its own verified recipe
below. None of them needs you to decorate the developer's own views or
handlers.

The set is closed — **FastAPI, Starlette, Flask, Django, Streamlit and Gradio**, by name. A plain
WSGI or ASGI callable is **not** detected and cannot be mounted; see
*Anything else* below for what to do instead.

### FastAPI / Starlette / Flask

```py
import boosthis
boosthis.mount(app, bubble=True)
```

Unit of work: **a request**, labelled with the route pattern.

### Django

Mount the WSGI/ASGI handler, in `wsgi.py` (or `asgi.py`), immediately after
it is created — that is the one place every request passes through:

```py
# myproject/wsgi.py
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")
application = get_wsgi_application()

import boosthis
application = boosthis.mount(application, bubble=True)
```

Unit of work: **a request**, labelled with Django's own URL pattern (`/`,
`/api/items/<int:item_id>/`). Nothing in `views.py`, `urls.py` or
`settings.py` changes, and no view is decorated. ASGI is the same call on the
handler from `get_asgi_application()`.

Readings that cannot exist under WSGI — event-loop lag, task backlog,
blocking calls in async — are reported as *not measurable* with the reason,
rather than sitting on "warming up" forever. Under ASGI they are measured
normally.

### Streamlit

A Streamlit script re-runs top to bottom on every interaction, so the mount
goes inside `@st.cache_resource`, which Streamlit runs once per process:

```py
import streamlit as st

@st.cache_resource
def _boosthis():
    import boosthis
    boosthis.mount(st)
    return True

_boosthis()   # near the top, before the rest of the script
```

Unit of work: **a script rerun** — the thing a Streamlit user actually waits
for — filed as `streamlit.rerun`.

Two honest limits. The page belongs to Streamlit's own front end, so there is
no host HTML to inject a floating bubble into; sign-in and telemetry are done
from the kit's own pages instead. And Streamlit executes the script only when
a browser session asks it to, so the kit mounts on the first rerun rather
than at server start. Readings that need an HTTP response of the app's own —
cookie exposure, refusal honesty, access pressure — are reported as *not
measurable* with the reason.

### Gradio

Mount the `Blocks`/`Interface` object **before** `launch()`:

```py
import gradio as gr
import boosthis

with gr.Blocks() as demo:
    ...

boosthis.mount(demo, bubble=True)
demo.launch()
```

Unit of work: **one event-handler invocation** — the click or submit a user
waits on — filed under the handler's own name (`gradio.slow_echo`). Gradio
runs on FastAPI, so every reading the FastAPI adapter takes is taken here
too.

### Anything else

Including a plain WSGI or ASGI app: the six above are matched **by name**,
so there is no generic WSGI or ASGI mount to reach for.

`mount()` raises `TypeError` naming what it was handed rather than attaching
to nothing and looking healthy. Measure such an app with `track_perf` /
`perf` (step 5a) and say plainly that the framework is not one Boosthis
attaches to. What is lost without a mount is the kit's own in-app page and
the automatic per-request timing; what is kept is every reading those named
blocks produce, uploaded and scored exactly the same way. Notebooks
(Jupyter, Colab, Marimo) are **not supported** — there is no unit of work to
time; `track_perf` / `perf` still work cell by cell.

`boosthis serve` is not a way to attach to an app. It starts a separate Boosthis-only server on its own port showing what has already been recorded on that machine: it does not host your app, sees none of its requests, and measures nothing by itself. Reach for it to look at recorded data, never to
get an app measured.

Keep `bubble=True` — the floating bubble is what end developers use to sign
in, set telemetry, and accept terms, and it can be hidden with
`BOOSTHIS_BUBBLE=0` (or `bubble=False`) if unwanted. Streamlit is the one
exception above: it has no host page to draw it on.

## 4. Turn on registered telemetry (optional)

The fastest answer to "is Boosthis working?" is the status page. Start the
app, hit a couple of routes, then open — in a browser ON THE SAME MACHINE:

```
http://localhost:<your port>/_boosthis/status
```

It is a plain HTML page the kit serves on YOUR app's own port, so a back-end
service with no UI (where the floating bubble has nowhere to appear) still
has somewhere to look. It shows the install ID, whether this app registered,
whether sharing is on, the telemetry mode, when the last upload was accepted,
how many requests have been measured, the kit version, and a link to the
dashboard — and when nothing is being sent it says why.

The page is **local-only**: served only to requests whose real TCP peer is
loopback and that carry no proxy/forwarding headers, exactly like
`/_boosthis/account`. A remote caller gets 403 and learns nothing. It shows
no credential — your install token is never rendered there, and
`BOOSTHIS_MOUNT_ALLOW_REMOTE` does not open it.

Call this ONCE at startup. Omit `invite_key` to stay 100% on-device.

```py
import os
import boosthis

boosthis.enable_telemetry(
    invite_key=os.environ.get("BOOSTHIS_INVITE_KEY"),
    endpoint="https://www.boosthis.com/api",
    app_name="My Python Service",
)
```

The full signature is `enable_telemetry(endpoint=None, invite_key=None,
share_meter_with_ai=None, app_name=None)` — **there is no `install_id`
argument.** The kit mints a UUID v4 install id itself and persists it in
`~/.boosthis/config.json`, so normally there is nothing to do. Keep the project
key in an env var.

**Precedence** for each field: the explicit argument to this call wins; then
the environment variable (`BOOSTHIS_INVITE_KEY`, `BOOSTHIS_INGEST_URL`); then
the value already persisted from a previous call; then the default. The
endpoint defaults to `https://www.boosthis.com/api`.

To **pin the install id** across restarts (e.g. one identity per replica), set
the `BOOSTHIS_INSTALL_ID` env var to a real UUID v4 — you do not pass it as an
argument.

### Troubleshooting: `invalid_install_id`

The install id **must be a UUID v4**. The kit generates and persists one for
you, so normally there is nothing to do — but if `~/.boosthis/config.json` was
hand-edited (or `BOOSTHIS_INSTALL_ID` was set to a made-up "stable id" string
like `"my-app-prod"`), the server rejects the registration with HTTP 400
`invalid_install_id`.

When that happens the kit logs **one** warning and the `/_boosthis`
page shows a "Registration rejected — install ID must be a UUID" card
instead of looking connected. It does **not** retry, because re-sending the
same id can never succeed. The fix:

```sh
python3 -c 'import uuid;print(uuid.uuid4())'   # or: uuidgen
```

Set that value as `BOOSTHIS_INSTALL_ID` (or delete `~/.boosthis/config.json` to
let the kit mint one itself), then restart the app and it registers normally.
An invented "stable id" string is not a UUID and is rejected the same way.

## 5. Everything you can call

Every host-callable entry point is importable from the top-level `boosthis`
package. One line each: the exact signature, then its purpose.

### Measure your own work

- `boosthis.track_perf(name)` — decorator that times a function (sync OR
  async). Also usable bare as `@track_perf` (name derived from
  `module.qualname`).
- `boosthis.perf(name)` — context manager that times a `with` block.
- `boosthis.mcp_tool(name)` — decorator for one stdio MCP tool handler, so
  each tool is timed as its own row instead of one opaque `POST /mcp`. HTTP
  MCP servers are relabelled automatically by the trace middleware and need
  no decorator.

### Report a scheduled job so a missed run is noticed

Timing a job says how long it took. It does not say the job was SUPPOSED to
run. Report each run of scheduled work and Boosthis can warn when an expected
run never arrives — the most common way a working app quietly stops working:

```python
import boosthis

# Wrap the work. Your return value and your exceptions pass through
# untouched; a job that raises is reported as a run that failed.
@boosthis.track_job("nightly-billing")
def run_billing() -> None:
    ...

# Or report a run you timed yourself:
boosthis.report_job_run("queue-drain", 812, True)
```

- Job names are developer-authored labels (`nightly-billing`), clamped to 80
  characters and screened like every other label: a name built out of a value
  (an id, an email, a UUID) is dropped rather than recorded.
- Only the name, that it ran, whether it succeeded, and how long it took is
  sent. Nothing about the job's arguments, payload, or data is collected.
- Runs are batched and shipped about every 30s. In a process that exits right
  after the job, call `boosthis.job_reporter.flush_job_runs_now()` before it
  exits.
- Best-effort by design: an unregistered app, the kill-switch, or a failed
  send is silent and never delays or fails the job.
- A warning needs an expectation. On the project page, say how often each job
  should run and how late is too late; until then the job simply shows its
  runs. Boosthis only blames a job while it can still hear the app.

### Join a trace across services

So one user action shows as ONE trace across services, propagate the trace id
on outbound calls:

- `boosthis.trace_headers(trace_id=None)` — returns the outbound header dict
  (`{'x-boosthis-trace': ..., 'x-boosthis-trace-elapsed': ...}`) to attach to
  an outbound HTTP call so the dashboard stitches this request to the next
  hop. Omitted, the argument defaults to the current request's trace. **This is the one call nothing else can make on the app's behalf** — a downstream service that never receives these headers is measured as a separate, disconnected trace.
- `boosthis.BoosthisTraceMiddleware(app)` — pure-ASGI middleware that adopts
  or mints the per-request trace id. `boosthis.mount(app, ...)` installs this
  for you on FastAPI / Starlette apps, so you only add it by hand for an ASGI
  app you do NOT mount: `app.add_middleware(boosthis.BoosthisTraceMiddleware)`.
- `boosthis.set_trace_id(trace_id)` — adopt an out-of-band trace id in a
  worker / CLI job (no ASGI request) so `@track_perf` code picks it up.
  `boosthis.get_trace_id()` reads the current id, or `None`.

### Turn on full sharing (spans + snapshot)

Full sharing has **two independent switches, and either one is enough**: the
consent switch in your Boosthis dashboard, or the in-code flag
`enable_telemetry(share_meter_with_ai=True)`. Leaving the in-code flag out is
**NOT a veto**: if the account already consented in the dashboard, sharing is
on regardless — a private app starts mirroring with no code change once you
press "Connect AI" in the dashboard. Passing `share_meter_with_ai=False`
explicitly turns the in-code opt-in back off; omitting it (the default `None`)
leaves the persisted choice untouched. The flag is **sticky** across restarts.

Until full sharing is on, the kit still measures locally and still reports
crash + issue data (as long as the runtime is enabled, the install is
registered, and it has not been disconnected) — but it does NOT upload the
trace-span waterfalls or the perf snapshot. Those two, and only those two,
move once full sharing is on; ONE upload gate (`enabled AND not disabled AND
(in-code opt-in OR dashboard consent)`) governs both.

### Send everything now (flush)

A mounted web app flushes on its own throttled cadence as requests complete,
so it never needs a manual flush. In a **short-lived process** — a worker, a
CLI job, a one-shot script, or a test — the process can exit before a flush
would otherwise run, so send by hand before returning:

- `boosthis.transmit_samples(samples)` — synchronously POST a batch of sample
  dicts (each `{routeLabel, durationMs, rating}`) and return the count the
  server accepted. This is the blocking flush; it returns only after the send.
  Build the batch from `boosthis.samples.recent()` if you want to ship what
  the process measured.
- `boosthis.span_emitter.flush_spans_now()` — flush any buffered trace spans
  immediately (a no-op unless full sharing is on and spans are buffered).

Neither sends anything when telemetry is disabled, the install is
unregistered, or `BOOSTHIS_DISABLED=1` — nothing leaves the process.

### Check what the kit is doing (diagnostics)

When the dashboard stays empty, read the observed state rather than guessing:

- `boosthis.telemetry_enabled()` — `True` when telemetry is enabled on disk.
- `boosthis.get_telemetry_config()` — the persisted config object (or `None`
  if never enabled): `install_id`, `enabled`, `endpoint`, `consent_at`, and
  whether a delete/read token was issued (registration succeeded).
- `boosthis.is_boosthis_disabled()` — `True` when `BOOSTHIS_DISABLED` is set
  (the whole runtime is silenced).

From a shell, the same picture plus a self-test:

```sh
boosthis telemetry status   # enabled? install id, endpoint, consent time
boosthis verify             # import + PII-guard + samples self-test
```

### Remove the install

BEFORE YOU START — do not remove the Boosthis connection from your AI tool
until removal is confirmed. The erase step runs over that connection while
this app still holds its own delete credential, so if the connection goes
first there is nothing left to erase this app's data on the Boosthis server
and nothing left to check whether it was erased — the owner is left with an
open question instead of an answer. Take the Boosthis connection out LAST,
after the erase step has run.

IF THE CONNECTION WAS ALREADY REMOVED FIRST — nothing is lost and no support
ticket is needed, because removing it never told Boosthis anything. Add the
Boosthis connection back with the same project key, put the erase call back
if the wiring was already stripped, run it once, then confirm. To confirm at
any time, sign in at https://www.boosthis.com/dashboard and read the Data
erasure list: it shows every erase Boosthis actually carried out, with the
date, and says plainly when no erase ever arrived for a project instead of
presenting silence as gone.

- `boosthis.disable_telemetry()` — stop uploading the optional perf snapshot
  and spans. Keeps the install id + token, so re-enabling resumes the same
  identity. NOTE: crash + issue reporting stays on for a registered install
  (that channel is intentionally not user-toggleable); only the two below stop
  it.
- `boosthis.forget()` — GDPR erasure: hands the delete token back to the
  server (which erases this install and everything it uploaded), then deletes
  `~/.boosthis/config.json` and every other local file the kit wrote. Returns
  `True` if a local config existed.
- `BOOSTHIS_DISABLED=1` — the emergency kill-switch: silences the entire
  runtime immediately, without touching stored state.

## 6. Settings (environment variables)

All optional; with no project key the kit stays silent. These are the
settings the kit itself reads (each also accepts the legacy `BOOSTEN_`
prefix). `x-boosthis-trace` / `x-boosthis-trace-elapsed` are HTTP header
names, not settings — do not set them yourself.
If this codebase already reports under another project key, set `BOOSTHIS_PROJECT_KEY_PY` — the same value is also accepted as `BOOSTHIS_PROJECT_KEY_PYTHON`.
`BOOSTHIS_PROJECT_KEY_PY` wins over the key passed in code, which in turn wins over the shared `BOOSTHIS_PROJECT_KEY`.

| Variable | Purpose |
| --- | --- |
| `BOOSTHIS_PROJECT_KEY_PY` (also accepted as `BOOSTHIS_PROJECT_KEY_PYTHON`) | This service's own project key, read before the key passed in code — the one lever that keeps a second Boosthis project in this codebase out of the first one's key. |
| `BOOSTHIS_PROJECT_KEY` (older name: `BOOSTHIS_INVITE_KEY`) | Project key shared by every kit in this codebase; without it the kit stays on-device. Sent as a Bearer token on consent. |
| `BOOSTHIS_INGEST_URL` | Point the kit at a different Boosthis endpoint; defaults to `https://www.boosthis.com/api`. |
| `BOOSTHIS_INSTALL_ID` | Pin the install id (a UUID v4) across restarts; otherwise the kit mints and persists one. |
| `BOOSTHIS_DISABLED` | `1`/truthy turns the whole runtime off, absolutely. |
| `BOOSTHIS_BUBBLE` | `1`/`true`/`yes`/`on` forces the in-page bubble on; `0`/`false`/`no`/`off` forces it off (same as `bubble=False`). |
| `BOOSTHIS_PORT` | Port for the standalone `boosthis serve` dashboard; defaults to `7787`. |
| `BOOSTHIS_DEP_INVENTORY` | `1` (only that exact value) opts in to sending dependency names + counts for the patch-lag meter. |

Advanced, and normally left unset — these feed the local `boosthis mcp` /
`boosthis serve` read path or tune internals; they are not part of app
wiring:

| Variable | Purpose |
| --- | --- |
| `BOOSTHIS_READ_TOKEN` | Read-only token the `boosthis mcp` server uses (with `BOOSTHIS_INSTALL_ID`) to read one install's live meter. |
| `BOOSTHIS_ENDPOINT` | Fallback base URL for the community / rule-fetch and MCP read paths when `BOOSTHIS_INGEST_URL` is unset. |
| `BOOSTHIS_APP_SCOPE` | Force which app the cross-process state belongs to; set only when the kit cannot work it out. |
| `BOOSTHIS_MOUNT_ALLOW_REMOTE` | `1`/truthy lets the mounted dashboard answer non-loopback requests (off by default). |
| `BOOSTHIS_TURBO` | `BOOSTHIS_TURBO=0` (only that exact value) opts out of the first-run upload ramp and uses the steady cadence from boot. |
| `BOOSTHIS_BUILD_COMMIT` / `BOOSTHIS_BUILD_TIME` | Feed the build-identity meter (patch-lag) when the kit cannot auto-detect them. |

## 7. The project key

One `bk_...` project key both **registers** this app and **fetches fixes**. It
cannot mint other keys, so it is safe to keep in the app config. If your AI
connected over the hosted MCP, the key was handed to it automatically; check
the kit response's `invite_key` field. With no key, skip `invite_key` and the
app stays 100% on-device.

## 8. Verify

### Is Boosthis running? Read the badge, then the startup line
Boosthis draws a small badge in the corner the moment it starts - before it reads the project key and before it tries to register. A badge therefore appears even when the key is wrong.
Badge on the page = the kit is running. Read the badge; it says what it is doing.
No badge at all = the kit was never switched on. The key, the network and the preview are all irrelevant until a badge appears.
The kit also says one line the moment it starts, and that line is the anchor for every diagnosis:
    [boosthis] Boosthis starting: project key ...1a2b. Registering next.
No line means the kit never started, whatever anyone believes.

1. THE STARTUP LINE IS THE ANCHOR. The app's console/log output either carries a line beginning "[boosthis] Boosthis starting:" or it does not. Quoted exactly as printed — including the four characters of the key it names — that line settles what the kit is actually configured with; paraphrased, or recalled from memory, it settles nothing. Every other explanation (the key, the network, a sandbox, a preview pane) is guesswork while that line is unread. No line at all means the kit never started, and nothing else is worth fixing until it does.

Start the app (or call a tracked function), hit a couple of routes, then open
`/_boosthis` if you mounted the kit — the in-app page every Boosthis kit
serves. The detailed local view is at `/_boosthis/dev`.
For a registered app the first
run flips it from "connected, awaiting telemetry" to reporting in the
maintainer's dashboard at `https://www.boosthis.com/dashboard`.

## 9. (Optional) local rule-book MCP server

The package ships a local stdio MCP server exposing the 92-rule Python
checklist and live-read tools. Run it with:

```sh
boosthis mcp
```

Point your AI tool's MCP config at that command to browse rules and (with
`BOOSTHIS_INSTALL_ID` + `BOOSTHIS_READ_TOKEN`) read this app's live meter.

## Kit files are vendored, not hand-edited

Boosthis files are vendored. An edit made in place is overwritten by the next re-fetch and shows up as `modified` in the verify check, so re-fetching the kit is what changes Boosthis behaviour. Where drift is suspected, the `verify_kit_install` check names every `missing`/`modified` file and `get_integration_kit` returns clean copies.

## MINTING THE KEY FOR THIS KIT

This kit reads its project key from the environment (the shared `BOOSTHIS_PROJECT_KEY` where nothing more specific is wired in), so the key never travels inside anything a user downloads. A `fix` key covers it — that is what the Project keys page mints by default, and it both registers this project and authenticates the Boosthis AI connection's reads.

The narrower `ship` scope exists for the kits whose key ends up inside a published artifact (the React Native kit, the browser kit, the Flutter kit, the Swift kit and the Kotlin kit). A key in the environment is not one of those, and a `ship` key used here would register perfectly well while costing the AI connection every read it makes.

A key committed to a repository is a key in a public artifact from the moment the repository is one, which is the case the environment setting avoids.

## What Boosthis costs your app

Measured on 2026-09-28, against the same application file with the kit never
imported, installed the way this guide says (boosthis.enable_telemetry(...)
+ boosthis.mount(app) on FastAPI under uvicorn): throughput falls 57.32% and
a request takes 4.31ms longer at the median, 37.28ms longer at the 95th
percentile. The rig's verdict on this run is OVER CEILING: it is above the
5% we hold this kit to, and the breach is recorded here rather than hidden.
Three interleaved rounds placed that figure at ±1.3 points. A process's
FIRST requests pay more, because the kit is loading, registering and warming
while it serves them: 53.51% and 33.67ms at the 95th percentile over that
window.

MEMORY. The same paired arms, with the whole process tree's resident memory
sampled from the kernel while the traffic ran: the kit adds 5.6 MB once the
process has settled (±0.03 MB across the rounds), and 5.63 MB at its largest
single sample — the figure that decides whether a container with a fixed
memory limit survives. Measured 2026-09-25. Resident memory is what the
operating system says the process holds. A runtime that manages its own heap
— the JVM, .NET, the BEAM — answers extra allocation by growing that heap,
so part of a large figure here is the heap responding to the kit's work
rather than the kit's own structures. It is still memory the machine must
have, which is why it is published; it is not a number to read as the size
of the kit.

EVERY RUNTIME, FOR COMPARISON. The same rig, the same workload, the same
kit-absent control, one runtime at a time — steady throughput cost: Browser
(web page) -2.06%, Edge isolates (Deno) 39.8%, Python 57.32%, Java 58.63%,
Node.js 79.71%, Bun 82.29%, Ruby 84.25%, Go 89.82%, Rust 90.09%, .NET
96.45%, Elixir 99.62%, PHP 99.86%. Measured on different days; each
runtime's own date is in the record. No figure yet, each with a written
reason and what would take one: React Native, Flutter, Swift / Apple
platforms, Kotlin / Android, Cloudflare Workers (workerd isolate). Nothing
is estimated for those, and nothing here is an average — an average over
runtimes would describe no app that exists.

WHAT THE KIT DOES AND DOES NOT PROMISE. Every public entry point in this kit
is wrapped so that a bug inside Boosthis returns a value to your code
instead of throwing into it, and each runtime's own test suite has a case
that checks it. That is a discipline we keep and check — it is not a
guarantee that nothing can go wrong inside your process. It cannot cover a
fault that starves your process rather than throwing in it, and it says
nothing at all about speed: the figures above are what this kit costs,
measured, and they are not zero. If you find a sentence in this kit's
release notes saying Boosthis "can never slow or crash your app", that
sentence is superseded by this one — the speed half was never measured and
the figures above contradict it. Boosthis is supplied as is and used at your
own risk — the LICENSE and TERMS shipped inside this kit say so, and nothing
here promises otherwise.


Coverage check before you finish: this is a SERVER kit, so it measures this service and nothing else. Most projects with a back end also have something the person actually looks at — a React Native/Expo app, or browser-served pages — and each of those has its own kit. One kit per runtime, the same project key for all of them — the remaining ones come from this same boosthis.get_integration_kit tool, and its `runtimes` list takes every runtime this project still needs in ONE further call; `runtime` on its own is the single-runtime form, for a project deliberately spanning one. The names: "flutter", "kotlin", "rn", "swift" for an app on a device, "web" for browser-served pages, and "bun", "dotnet", "edge", "elixir", "go", "java", "node", "php", "python", "ruby", "rust" for a back end. Worth a quick look around the whole repo before calling this done (package.json with expo or react-native → the "rn" kit; package.json with react/next/vite/vue/svelte/angular → the "web" kit). Boosthis serves a small shell script at https://www.boosthis.com/scan to a caller presenting this project's key: it reads manifest file names only, uploads nothing, walks monorepo subdirectories, and names every runtime the project spans. With only a server kit installed the developer sees no bubble anywhere (see the note above), which reads exactly like a failed install. If a runtime is genuinely out of scope here, telling the developer so up front saves the confusion.

STILL UNANSWERED: WHAT ELSE THIS PROJECT SPANS: the python kit covers the python runtime and no other, and nothing here establishes which other runtimes this project has — so this delivery answers one runtime out of an unknown number rather than finishing the install. One further call settles it: boosthis.which_kits reads the manifest file NAMES already visible in the project (nothing downloaded, nothing executed, no source code read) and names the kit each one implies; boosthis.get_integration_kit then takes every runtime it named in a single `runtimes` list. A host that cannot list files, or a developer who says the project is this one runtime, is served exactly the same kit either way — what is left unestablished is what the project spans, and saying that plainly is more use to the developer than an install reported as complete. What is already registered under this project key is deliberately not named in this file: every reader of this kit gets the same bytes, and the kit's own verification compares them. The kit reply's `project_span`, boosthis.connection_status and the dashboard each name it for THIS key.

COMPLETE INSTALL PATH: the archive at kit_archive_url (or every manifest file downloaded by hand) puts the files on disk. What turns them into an install is this kit's version-matched INSTALL.md, all the way through its runtime-specific dependency, startup wiring, and first real run. Files on disk alone are not an install.
PROJECT KEY: this kit's project-key option or environment setting takes the served invite_key; the exact runtime-specific name and startup call are in the guide above.
CONTROL HOW MUCH DATA ARRIVES: the guide's issues-only/private-mode switch limits collection to crashes and issues, while its share-with-AI switch opts the meter/span and snapshot mirror back in. Use the exact symbols listed for this runtime above; these are separate choices, not synonyms.
FLUSH: a short-lived worker, CLI, test, or backgrounded app exits with its queue undrained unless the runtime-specific flush call named above runs first. Where this runtime has no separate flush call, its guide says so and names the lifecycle event that drains the queue instead.
DIAGNOSTICS: the runtime's diagnostics/status calls named above, plus the first ungated console line, separate not-started, not-registered, disabled and connected. An unknown state is unknown: it is not evidence of a healthy install.
TURN IT OFF OR FORGET DATA: BOOSTHIS_DISABLED silences collection without deleting stored data. The runtime's forget/remove call named above deletes this install's stored Boosthis data and credentials; these are deliberately different operations.

FILES ON DISK ARE NOT AN INSTALL — THREE THINGS STILL HAVE TO HAPPEN AFTER THE FILES ARE WRITTEN, AND THIS IS THE POINT AT WHICH AN INSTALL IS USUALLY LEFT HALF-FINISHED. A passing file check (verify_kit_install, or its plain-HTTPS twin) says exactly one thing: the bytes written to disk match the bytes we served. It says nothing about whether anything is measured. The work that remains is that the kit is wired into the app's own startup path, that reporting is switched on with the project key, and that the app has actually been run (a back-end service also needs one real request). Files written with none of that is a project that is completely dark, and from the inside it looks exactly like a correct one — which is why the closing step is never a reading of the project from the inside, a screenshot, a preview pane, or a passing file check.
THE FINISHING QUESTION IS WHETHER BOOSTHIS HAS SEEN THE APP CHECK IN. One request answers it from anywhere — a restricted preview, a machine with no browser, a project that has never been published, a session whose tool replies are being screened: GET https://www.boosthis.com/api/connection-status, authenticated with the same project key.
Ordinary HTTPS is the route this guide uses; boosthis.connection_status is the same answer by a more convenient path, from the same builder. The state can change after the app runs, so the answer is only current as of when it was asked.
THERE ARE EXACTLY TWO STATES THIS WORK CAN END IN, and they are not the same: “reporting — Boosthis has seen this app check in”, and “files written but not switched on — nothing is being measured yet”, the second of which leaves something outstanding and someone who has to do it. Written files are not an installed, verified or finished state while Boosthis has not seen a check-in.