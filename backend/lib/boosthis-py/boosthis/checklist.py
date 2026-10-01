"""The Boosthis checklist — Python edition.

92 Python-specific rules covering the most common server-side, async, ORM,
and operational pitfalls. Each entry mirrors the shape of `@boosthis/checklist`
in JavaScript::

    {
        "id": str,           # kebab-case stable identifier
        "title": str,        # one-line summary
        "when_to_apply": str,
        "evidence": list[str],
        "category": "case-study" | "industry" | "operational",
        "languages": ["python"],
    }
"""

from __future__ import annotations

from typing import Final, Literal, TypedDict

Category = Literal["case-study", "industry", "operational"]


class ChecklistEntry(TypedDict):
    id: str
    title: str
    when_to_apply: str
    evidence: list[str]
    category: Category
    languages: list[str]


BOOSTHIS_CHECKLIST: Final[list[ChecklistEntry]] = [
    # ─────────── Case Study (10) — patterns from real Python prod incidents ───────────
    {
        "id": "py-registration-idempotent-single-winner",
        "title": "First-contact/registration endpoints must be idempotent single-winner",
        "when_to_apply": "A 'first contact' endpoint that mints or returns a credential (install/device registration, enrollment, activation, first token mint, fresh-consent handshake) writes with an UPSERT / `ON CONFLICT DO UPDATE` / `session.merge()` / `update_or_create()` on the identity key — so two racing calls (client double-fire, retry after a timed-out-but-succeeded request, at-least-once queue redelivery) both 'win' and the second write clobbers the credential the first already issued. Symptom: a client that registered fine suddenly 401-loops because a duplicate call silently rotated or blanked its token; the corruption is invisible under single-threaded testing and only appears under concurrency/retries. Fix shape: INSERT with `ON CONFLICT DO NOTHING ... RETURNING` (or `INSERT ... IGNORE` + re-select) so exactly one caller mints the credential; the loser gets a credential-less success and the client MUST keep its existing credentials on such a response rather than treating an empty credential field as 'revoked'.",
        "evidence": [
            "Boosthis production incident (Jul 2026) — a racing double-fire on the fresh-consent registration endpoint upserted over the already-issued token, 401-looping a project that had registered cleanly",
            "PostgreSQL docs — INSERT ... ON CONFLICT DO NOTHING / RETURNING",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "py-install-id-reuse",
        "title": "Never reuse a registration/install ID across installs — mint a fresh UUID each time",
        "when_to_apply": "An install / device / instance identifier is hard-coded, copied from docs or another service, or persisted once and reused verbatim across re-installs (a constant in settings, an env var baked into the image, an ID read back from a shared config file and never regenerated). Symptom: once an original install is orphaned — consented but its token was lost — every LATER install that presents the same ID gets a token-less success forever, an unbreakable deadlock no server-side lever can clear because the server correctly sees 'this identity already exists'. Rule: mint a FRESH `uuid.uuid4()` per logical install/instance at first run and persist THAT; never reuse an ID from documentation, another app, or a prior install. Treat the ID as identity, not as configuration to be templated or shared.",
        "evidence": [
            "Boosthis production incident (Jul 2026) — a reused hard-coded install ID left every fresh install inheriting an orphaned identity that could never obtain a token",
            "RFC 4122 — a Universally Unique IDentifier (UUID) per instance",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "py-session-cookie-payment-redirect",
        "title": "Never rely on session cookies surviving a payment/3DS redirect",
        "when_to_apply": "A payment return / confirm / callback endpoint (`/payment/return`, `/checkout/confirm`, a webhook-adjacent GET the bank redirects the user back to) reads `request.session` / a login-required decorator / `current_user` to identify the checkout. The 3DS or gateway hop is a CROSS-SITE redirect, so a `SameSite=Lax`/`Strict` (or otherwise dropped) session cookie does NOT come back — the endpoint sees an anonymous request and strands a customer who ALREADY PAID at a login page with no order recorded. Rule: payment return/confirm legs must be session-free by design — identify and verify the payment SERVER-TO-SERVER with the provider using the payment-intent/session IDs carried in the URL/callback params, not the user session; give the leg its own rate-limit bucket (it's unauthenticated by design); and run a background sweep that reconciles paid-but-unstitched checkouts so a dropped cookie never loses a sale.",
        "evidence": [
            "Boosthis production incident (Jul 2026) — the 3DS redirect dropped the SameSite session cookie, stranding a paid customer at a login page with no order record",
            "MDN — SameSite cookies and cross-site redirects",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "py-background-work-needs-scheduler",
        "title": "Scheduled/background work must never depend on page visits",
        "when_to_apply": "Work that MUST happen on a schedule — subscription renewals, data-retention/GDPR purges, expiry sweeps, digest emails, reconciliation jobs — is triggered from inside a request handler (often an admin/dashboard view, a health-check route, or a middleware that runs 'once in a while when traffic hits'). Symptom: the moment nobody visits that page — quiet weekend, admin on holiday, dashboard retired — renewals silently stop charging and legally-required purges silently never run, with no error because nothing ran at all. Rule: anything with a deadline needs a REAL timer/cron/worker owned by an always-on process — Celery Beat, APScheduler in a dedicated worker, a systemd timer, or a platform scheduled job — never piggybacked on the request path of an optional page. Verify by asking 'what runs this if no human ever opens the page?'; if the answer is 'nothing', it's a silent-failure design.",
        "evidence": [
            "Boosthis production incident (Jul 2026) — renewals and retention purges ran only inside an admin page handler and silently stopped when nobody visited",
            "Celery docs — Periodic Tasks (celery beat)",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "py-registration-retry-no-cooldown",
        "title": "Cool down failed registration/handshake retries — never re-attempt on every flush",
        "when_to_apply": "A one-time 'first contact' call (install/device registration, consent, enrollment, license activation, first token mint) is lazily retried from a hot path — every upload flush, queue drain, timer tick, or request — until it succeeds, with no cool-down and no special handling for HTTP 429. Symptom: a client that cannot finish registering (offline at setup, pending invite, busy server) silently re-attempts dozens of times an hour; behind a shared proxy/NAT egress IP those retries drain the server's per-IP registration rate bucket and starve OTHER clients' genuine first registrations — the failure surfaces on a different machine than the bug. Distinct from requests-retry-storm (a tight loop retried too fast): each attempt here is cadence-driven and looks harmless in isolation, so the hammering hides in normal traffic.",
        "evidence": [
            "Boosthis production incident (Jul 2026) — per-flush consent retries starved a shared per-IP registration rate bucket and blocked new installs",
            "Google SRE Book — Handling Overload (retry amplification)",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "n-plus-one-orm-query",
        "title": "Detect & batch N+1 ORM queries on hot routes",
        "when_to_apply": "A list endpoint serializes a parent row and accesses a related attribute inside the loop (e.g. `for order in orders: serialize(order.customer)`). SQLAlchemy lazy='select' or Django foreign keys without `select_related()` will issue one query per row. Symptom: a list endpoint that's O(1) queries at dev seed becomes O(N) under production data.",
        "evidence": [
            "Django docs — select_related / prefetch_related",
            "SQLAlchemy 2.x — relationship loading techniques",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "sync-io-in-async-handler",
        "title": "Never call blocking I/O from an async handler",
        "when_to_apply": "A FastAPI / Starlette / aiohttp async route calls a sync library (`requests.get`, `psycopg2`, `boto3` client, `redis.Redis` from sync redis-py, `time.sleep`, `subprocess.run`). Symptom: the entire event loop stalls — every concurrent request waits for that one sync call. Throughput collapses under load even at low p50 latency.",
        "evidence": [
            "FastAPI docs — Async vs Sync",
            "PEP 3156 — asyncio rationale",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "pillow-decode-on-request-path",
        "title": "Move Pillow / image decoding off the request thread",
        "when_to_apply": "A route accepts an uploaded image and decodes / resizes / re-encodes it inline (`Image.open(file).resize(...)`). Pillow is CPU-bound and holds the GIL for the entire decode. Symptom: a single 4 MB upload pegs one worker for 200–600ms; concurrent uploads serialize behind it.",
        "evidence": [
            "Pillow docs — performance considerations",
            "Postmark Engineering — image processing pipelines (2024)",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "celery-task-no-timeout",
        "title": "Every Celery task needs both a soft and hard timeout",
        "when_to_apply": "A `@shared_task` definition with no `time_limit` / `soft_time_limit`. Symptom: a single external dependency outage (slow webhook, hung DB connection) leaves workers stuck on tasks that will never complete. The worker pool drains; subsequent tasks queue forever.",
        "evidence": [
            "Celery docs — task time limits",
            "Instagram Engineering — Celery at scale post-mortems",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "gunicorn-cold-fork-startup",
        "title": "Pre-import heavy modules before the gunicorn fork",
        "when_to_apply": "A Django / Flask app served by gunicorn where individual worker boot takes >2s because each forked worker re-imports numpy/pandas/torch/etc. Symptom: rolling deploys show a P99 spike for 30–60s after every restart as new workers warm up.",
        "evidence": [
            "Gunicorn docs — preload_app",
            "Datadog — Python application warmup patterns",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    # ─────────── Industry-mined (7) — Python community consensus ───────────
    {
        "id": "requests-default-no-timeout",
        "title": "Every outbound HTTP call needs an explicit timeout",
        "when_to_apply": "Anywhere you call `requests.get(url)` / `httpx.get(url)` without a `timeout=` kwarg. The default is None — a hung remote will hang YOUR process forever. Symptom: a downstream slowdown turns into a thread-pool exhaustion incident on YOUR side.",
        "evidence": [
            "requests docs — timeouts",
            "Hynek Schlawack — Python HTTP best practices (2023)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "gil-bound-cpu-in-asyncio",
        "title": "Move CPU-bound work off the asyncio event loop",
        "when_to_apply": "An async function does meaningful CPU work inline (JSON parsing > 1 MB, regex against large strings, crypto, image manipulation, ML inference). The GIL means CPU work in the event loop blocks every other coroutine for the duration. Symptom: tail latency for unrelated routes spikes whenever the CPU-heavy route is hit.",
        "evidence": [
            "Python docs — concurrent.futures",
            "Łukasz Langa — Python concurrency demystified (PyCon 2023)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "logging-in-hot-loop",
        "title": "Use lazy %-formatting in logger calls, never f-strings",
        "when_to_apply": "Any `logger.debug(f'foo {expensive()}')` call. The f-string is evaluated BEFORE `logger.debug` checks whether DEBUG is enabled. Symptom: prod CPU profile shows debug-level logs eating real cycles even though nothing is written.",
        "evidence": [
            "Python docs — logging levels and lazy formatting",
            "Sentry blog — Python logging anti-patterns (2024)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "open-files-without-context-manager",
        "title": "Always open files with `with` — leaked FDs crash long-running processes",
        "when_to_apply": "`f = open(path)` followed by manual `.read()` and `.close()`, or worse, `open(path).read()` chains that never close. Symptom: ResourceWarnings in dev, `OSError: [Errno 24] Too many open files` in prod after hours/days of uptime.",
        "evidence": [
            "Python docs — context managers",
            "PEP 343 — the with statement",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "pandas-iterrows-anti-pattern",
        "title": "Replace `.iterrows()` / `.apply(axis=1)` with vectorized ops",
        "when_to_apply": "Any pandas code iterating over rows with `for idx, row in df.iterrows():` or `df.apply(fn, axis=1)`. Symptom: ETL jobs that should run in seconds take minutes; profiler shows pandas internal overhead dominating the wall time.",
        "evidence": [
            "pandas docs — Enhancing performance",
            "Wes McKinney — Python for Data Analysis (3e)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "asyncio-create-task-no-reference",
        "title": "Keep a reference to every `asyncio.create_task()` you fire",
        "when_to_apply": "Fire-and-forget code like `asyncio.create_task(send_email(...))` without storing the returned task. asyncio holds tasks via weak references — yours can be garbage-collected mid-execution. Symptom: tasks silently disappear under load, no traceback, no log.",
        "evidence": [
            "Python docs — asyncio.create_task (warning section)",
            "Łukasz Langa — Why TaskGroup matters (PyCon 2023)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "json-vs-orjson-throughput",
        "title": "Swap `json` for `orjson` on serialization-heavy endpoints",
        "when_to_apply": "An API route returns large JSON payloads (>50KB) using stdlib `json.dumps()`. Symptom: serialization shows up in flame graphs as a meaningful fraction of request time; CPU is the bottleneck on the response path.",
        "evidence": [
            "orjson README — benchmarks",
            "FastAPI docs — Custom Response classes",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    # ─────────── Operational (3) — how to use Boosthis itself in Python ───────────
    {
        "id": "wrap-every-route-with-track-perf",
        "title": "Decorate every route handler with `@track_perf` — not middleware",
        "when_to_apply": "An ASGI middleware that times every request and reports under one bucket. The middleware times the whole-pipeline duration but loses per-route attribution. Symptom: you know the API is slow, but not which route. Same lesson as RN's `tracker-on-every-screen` rule.",
        "evidence": [
            "boosthis Python README — FastAPI / Django examples",
            "Mirrors the RN rule: tracker-on-every-screen",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "per-route-budgets-not-global",
        "title": "Set per-route budgets in `boosthis.config.json` — not one global target",
        "when_to_apply": "Treating all Python routes with the same 500ms screen budget. A `/health` endpoint shouldn't have the same target as `/reports/generate`. Symptom: either everything passes (budget too loose for /health) or everything fails (too tight for /reports).",
        "evidence": [
            "boosthis config schema — budgets",
            "Shopify Engineering — Five Years of React Native",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "log-scrub-applies-to-server-too",
        "title": "Run `assert_no_pii()` on every payload BEFORE it leaves the process",
        "when_to_apply": "Server-side code that sends perf samples to an ingest endpoint, an APM, or an analytics backend. Easy to assume 'the server is trusted' and skip the PII guard — but a leaked email in a perf sample is still a leak. Symptom: request bodies, user emails, IPs show up in your APM/analytics dashboards.",
        "evidence": [
            "boosthis Python — pii.py PII_DENYLIST",
            "Mirrors the RN rule: ingest-token-is-public-by-design",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    # ─────────── v0.2 expansion (10 rules) — FastAPI/SQLA/asyncio/Pandas/etc ───────────
    {
        "id": "fastapi-sync-route-blocks-event-loop",
        "title": "Don't declare a `def` route in FastAPI if the handler does any work",
        "when_to_apply": "A FastAPI route is declared with plain `def` (not `async def`). FastAPI dispatches `def` routes to a small thread pool — fine for trivial handlers but catastrophic if the handler does CPU work or any I/O that *could* have been async. Symptom: throughput collapses at modest concurrency because every request is serialized behind the thread pool.",
        "evidence": [
            "FastAPI docs — Concurrency and async/await",
            "Tiangolo blog — when to use async def vs def",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "sqlalchemy-session-per-request-leak",
        "title": "Scope your SQLAlchemy `Session` per-request — never module-level",
        "when_to_apply": "A SQLAlchemy `Session` is created once at module import (`session = Session()`) and reused across requests. The identity map grows unboundedly, hidden references prevent GC, and one bad query taints every subsequent request. Symptom: worker RSS climbs steadily over hours until OOM, even though traffic is flat.",
        "evidence": [
            "SQLAlchemy docs — Session lifespan and scoping",
            "Miguel Grinberg — common SQLAlchemy mistakes",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "numpy-python-loop-over-array",
        "title": "Vectorize NumPy/Pandas operations — Python `for` loops over arrays are 50–1000× slower",
        "when_to_apply": "Iterating a NumPy array or Pandas Series with a Python `for` loop (`for x in arr: result.append(x*2)`). NumPy's whole value is vectorization — looping element-by-element gives you the worst of both worlds: NumPy's memory model with Python's interpreter cost. Symptom: ML/data scripts that should be milliseconds take seconds or minutes.",
        "evidence": [
            "NumPy docs — Why is NumPy fast",
            "Pandas Performance — Enhancing performance with vectorization",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "redis-pipeline-not-used-for-bulk",
        "title": "Use `pipeline()` when issuing more than 2 Redis commands in a row",
        "when_to_apply": "Code that calls `redis.set` / `redis.get` / `redis.hset` repeatedly in a loop or function body. Each call is a full network round-trip — at 1ms RTT and 100 keys, that's 100ms just in network. Symptom: a 'cache lookup' takes longer than the database query it was meant to avoid.",
        "evidence": [
            "redis-py docs — pipelines",
            "Redis docs — pipelining for high performance",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "pydantic-v2-model-validate-in-hot-path",
        "title": "Don't re-validate already-validated Pydantic v2 models in inner loops",
        "when_to_apply": "Pydantic v2 code that calls `Model.model_validate(data)` inside a hot loop where `data` is already a `Model` instance, or where the data was just validated upstream. Pydantic v2 is fast for the first validation but re-validation in a loop adds up. Symptom: a batch processor or serializer that's CPU-bound but no single line stands out in a profile.",
        "evidence": [
            "Pydantic v2 docs — model_construct vs model_validate",
            "Pydantic-core benchmarks (2024)",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "django-only-defer-on-large-rows",
        "title": "Use `.only()` / `.defer()` when a Django model has wide TEXT/JSONB columns",
        "when_to_apply": "A Django queryset returns full rows (`Article.objects.all()`) when the page only needs a few columns, and the model has wide columns (TEXT bodies, JSONB metadata, blob fields). Symptom: a list endpoint transfers MBs of data over the wire per request when it only needs IDs + titles, and serialization CPU is dominated by columns you never read.",
        "evidence": [
            "Django docs — QuerySet.only() and defer()",
            "Django performance handbook — query optimization",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "asyncio-gather-without-return-exceptions",
        "title": "Call `asyncio.gather(..., return_exceptions=True)` unless you mean to cancel siblings",
        "when_to_apply": "`await asyncio.gather(*tasks)` with no `return_exceptions=True`. By default, the first exception cancels every other task and propagates. For 'fetch 10 things in parallel, tolerate partial failures' this is the wrong behavior. Symptom: one flaky downstream call kills the entire batch, and the error you see in logs is for a task that wasn't even the root cause.",
        "evidence": [
            "Python docs — asyncio.gather",
            "Python 3.11 release notes — asyncio.TaskGroup",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "requests-no-session-reuse",
        "title": "Reuse `requests.Session()` / `httpx.AsyncClient()` — don't construct one per call",
        "when_to_apply": "Code that calls `requests.get(url)` (or `httpx.AsyncClient().get(url)`) in a loop, or fresh in every function call. Each call pays TCP handshake + TLS handshake + DNS, even to the same host. Symptom: an integration that should take 50ms takes 200ms+, and the latency comes from connection setup, not the remote service.",
        "evidence": [
            "requests docs — Session objects",
            "httpx docs — Async clients and connection pooling",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "dict-membership-on-list",
        "title": "Use a `set` for membership tests — `x in list` is O(n)",
        "when_to_apply": "Code that checks `if x in some_list` where `some_list` has more than a handful of items and is reused. Hits hardest in filters and joins (`[a for a in items if a.id in allowed_ids]`). Symptom: a function that should be O(n) becomes O(n·m); profile shows time in `__contains__`.",
        "evidence": [
            "Python docs — TimeComplexity wiki",
            "Raymond Hettinger — Beyond PEP 8 (set vs list lookups)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "boosthis-context-not-mounted-in-mcp",
        "title": "Wire Boosthis into your AI editor's MCP config so the agent can read live perf state",
        "when_to_apply": "Project has `boosthis` installed but no MCP entry in `.cursor/mcp.json`, `claude_desktop_config.json`, or the Replit AI MCP config. The whole point of Boosthis on Python is that AI agents can query rules + recent samples + match snippets. Without the MCP wire-up, that channel doesn't exist and the agent has to be told manually each time.",
        "evidence": [
            "boosthis README — AI integration",
            "Model Context Protocol — spec.modelcontextprotocol.io",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    # ─────────── Cross-runtime parity (2) — retry storm + idle burn ───────────
    {
        "id": "requests-retry-storm",
        "title": "Cap outbound retries with exponential backoff + jitter, never a tight retry loop",
        "when_to_apply": "An outbound call (`requests`, `httpx`, `aiohttp`, a DB/Redis/boto3 client) is retried on failure inside a `while`/`for` loop or a recursive except with no exponential backoff, no jitter, and no max-attempts ceiling. Symptom: the moment an upstream slows or returns 5xx, every worker hammers it as fast as the GIL allows — a self-inflicted thundering herd that turns a brief blip into a sustained outage and pegs CPU on retry bookkeeping. Boosthis flags it as a burst of repeated same-target outbound calls clustered in time on one route.",
        "evidence": [
            "AWS Architecture Blog — Exponential Backoff And Jitter",
            "urllib3 docs — Retry with backoff_factor",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "asyncio-idle-poll-burn",
        "title": "Park or back off background polling tasks when there is no work",
        "when_to_apply": "A background `asyncio` task, `threading.Timer`, or scheduler loop polls on a fixed short cadence (cache refresh, queue drain, heartbeat, metrics flush) and keeps spinning even when idle — `while True: poll(); await asyncio.sleep(0)` or a tight `time.sleep(0.01)` with no work-gated wait. Symptom: the worker never goes quiet between requests, CPU stays warm, containers never scale to zero, and credits/battery drain doing nothing. Boosthis flags steady periodic wake-ups with no correlated request or queue activity.",
        "evidence": [
            "Python docs — asyncio.Event for work-gated waits",
            "Python docs — Don't busy-wait; sleep proportionally to work",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    # ─────────── v0.3 expansion (6) — ReDoS / pooling / pagination / pandas / streaming / DB timeout ───────────
    {
        "id": "python-regex-redos",
        "title": "Block catastrophic backtracking in `re` on user input (ReDoS)",
        "when_to_apply": "A view / handler validates or parses user-controlled input (query param, header, form / JSON field) with a `re` pattern containing nested quantifiers (`(a+)+`, `(.*)*`, `(\\d+)*`) or overlapping alternation. Python's `re` is a backtracking engine, so a crafted ~30-character input can peg one CPU core for seconds; under the GIL that stalls the whole worker (and every coroutine sharing its event loop). Symptom: a single request spikes CPU to 100% and times out while the process serves nothing else.",
        "evidence": [
            "OWASP — Regular expression Denial of Service (ReDoS)",
            "Python docs — re performance; google-re2 (pyre2) linear engine",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "sqlalchemy-pool-not-sized",
        "title": "Size the SQLAlchemy engine pool for your concurrency",
        "when_to_apply": "`create_engine(url)` is called with no `pool_size` / `max_overflow` / `pool_timeout` / `pool_pre_ping`, relying on the QueuePool defaults (5 + 10 overflow). Symptom: under modest concurrency — or with Gunicorn × N workers each holding its own pool — connections multiply past Postgres's `max_connections` and requests raise `TimeoutError: QueuePool limit ... overflow ... reached`, while connections stale after a DB restart raise `OperationalError`. Boosthis flags request latency that cliffs the moment concurrency exceeds the pool.",
        "evidence": [
            "SQLAlchemy docs — Engine Configuration (pool_size, max_overflow)",
            "SQLAlchemy docs — Dealing with Disconnects (pool_pre_ping)",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "unbounded-queryset-no-pagination",
        "title": "Paginate list endpoints — never return a whole table",
        "when_to_apply": "A list endpoint materializes an entire table with `Model.objects.all()` / `session.query(Model).all()` / `list(queryset)` and serializes it with no LIMIT, slice, or pagination. Symptom: O(1) on dev seed data, but as rows grow the DB read, ORM hydration, serialization, and response body all scale linearly until one request loads hundreds of MB and times out. Distinct from the only() / defer() rule — that trims COLUMNS, this caps ROW count.",
        "evidence": [
            "Django docs — Pagination / QuerySet.iterator()",
            "DRF docs — Pagination (PageNumberPagination, CursorPagination)",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "pandas-concat-in-loop",
        "title": "Build DataFrames once — don't `pd.concat` / `append` in a loop",
        "when_to_apply": "A loop grows a DataFrame each iteration — `df = pd.concat([df, row])` or the deprecated `df = df.append(...)` inside `for`. Each call copies the ENTIRE accumulated frame, so building N rows is O(N**2) in time and memory. Symptom: a job that's instant for 100 rows takes minutes and spikes RSS for 100k; CPU is pegged in `concat` / memory-copy, not real work.",
        "evidence": [
            "pandas docs — Concatenating objects (avoid in a loop)",
            "pandas docs — append removed in 2.0",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "read-whole-file-into-memory",
        "title": "Stream large files — don't `.read()` the whole thing into memory",
        "when_to_apply": "Code loads an entire file / upload / download into memory at once — `data = open(path).read()`, `f.readlines()`, `content = await upload.read()`, or `requests.get(url).content` for a large body — then processes it. Symptom: RSS scales with file size × concurrency; a few large files in flight OOM the worker, and time-to-first-byte is bad because nothing happens until the whole file is buffered. Distinct from the file-handle / context-manager rule (that's FD leaks; this is memory).",
        "evidence": [
            "Python docs — iterate a file object line by line / read(size)",
            "requests docs — stream=True and iter_content",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "db-statement-timeout-missing",
        "title": "Set a database statement timeout so one query can't hang a worker",
        "when_to_apply": "DB connections are opened with no statement / query timeout — psycopg / psycopg2 `connect()` without `options='-c statement_timeout=5000'`, SQLAlchemy with no `connect_args` timeout, Django without `OPTIONS: {'options': '-c statement_timeout=5000'}`. Symptom: a slow or lock-blocked query holds its connection (and, in sync apps, its worker) indefinitely; a few of them drain the pool / worker set and the whole app stops responding while the query runs unbounded.",
        "evidence": [
            "PostgreSQL docs — statement_timeout",
            "psycopg docs — connection options / statement_timeout",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    # ─────────── v0.4 expansion (4) — deepcopy / lru_cache / executor / read_sql ───────────
    {
        "id": "deepcopy-on-hot-path",
        "title": "Avoid `copy.deepcopy()` on the request path — it is reflection-heavy and slow",
        "when_to_apply": "A handler / serializer clones a nontrivial structure with `copy.deepcopy(obj)` on every request — duplicating a config dict, a default payload template, an ORM object, or request context before mutating it. `deepcopy` walks the whole object graph through the `copy` protocol with a memo dict and is often 10–100× slower than a purpose-built copy. Symptom: a route that does 'no real work' still burns measurable CPU, and a profile shows time inside `copy.deepcopy` / `_reconstruct`.",
        "evidence": [
            "Python docs — copy.deepcopy semantics & cost",
            "CPython issue tracker — deepcopy performance discussions",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "unbounded-lru-cache",
        "title": "Bound `functools.lru_cache` — `maxsize=None` over many keys leaks memory",
        "when_to_apply": "`@functools.lru_cache(maxsize=None)` (or `@functools.cache`) decorates a function whose argument space is effectively unbounded — per-user id, per-URL, per-timestamp — or it decorates an instance method (which also pins every `self` alive forever). The cache only ever grows. Symptom: worker RSS climbs steadily for the life of the process until OOM with no correlated traffic increase — the classic 'memory leak' that is really an unbounded cache. Distinct from the SQLAlchemy session leak (that's the identity map): this is the stdlib cache decorator.",
        "evidence": [
            "Python docs — functools.lru_cache (maxsize, cache_info)",
            "Python docs — lru_cache on methods keeps instances alive",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "executor-created-per-call",
        "title": "Reuse one `ThreadPoolExecutor` / `ProcessPoolExecutor` — don't build one per call",
        "when_to_apply": "Code constructs a `ThreadPoolExecutor()` / `ProcessPoolExecutor()` inside a handler or function body (often `with ThreadPoolExecutor() as ex:` per request) instead of a shared module-level pool. Each construction spins up worker threads — or forks / spawns processes, very expensive on the 'spawn' start method — then tears them down again. Symptom: thread / process churn shows up as setup latency and, for process pools, repeated interpreter startup; under load the constant create / destroy dwarfs the actual work. Distinct from the requests-session and DB-pool rules: this is the concurrency executor itself.",
        "evidence": [
            "Python docs — concurrent.futures Executor lifecycle",
            "Python docs — ProcessPoolExecutor start methods (spawn cost)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "pandas-read-sql-no-chunksize",
        "title": "Stream big result sets with `read_sql(..., chunksize=)` — don't load a whole table into a DataFrame",
        "when_to_apply": "`pd.read_sql(query, conn)` / `pd.read_sql_table(...)` materializes an entire table or unbounded query into one DataFrame with no `chunksize=`. The full result set is buffered in memory and copied into pandas' columnar blocks at once. Symptom: RSS scales with row count × column width; a report that's fine on dev seed data OOMs the worker once the table grows, and nothing is processed until the whole frame is built. Distinct from the ORM unbounded-queryset rule (the web ORM path) and from pandas-concat-in-loop (O(N**2) growth): this is the DB → DataFrame read step.",
        "evidence": [
            "pandas docs — read_sql chunksize / iterator",
            "pandas docs — scaling to large datasets",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    # ─────────── Expansion (4) — global leak / asyncio fan-out / cache stampede / bulk writes ───────────
    {
        "id": "module-level-accumulation-leak",
        "title": "Bound module-level collections — a global list/dict that only grows leaks until OOM",
        "when_to_apply": "A module-level (global) `list` / `dict` / `set` is appended to or keyed on request data and never bounded — an in-process cache with no eviction, a `_seen = []` audit list, a metrics dict keyed by user/URL, memoization keyed by unbounded inputs. Because the module object lives for the whole process, the collection grows forever. Symptom: worker RSS climbs steadily across hours until the process is OOM-killed and restarted (dropping requests); it never shows in dev because dev restarts constantly. Distinct from the unbounded-lru-cache rule (`functools.lru_cache(maxsize=None)`): this is a hand-rolled global collection.",
        "evidence": [
            "Python docs — module objects live for the process lifetime",
            "Python docs — tracemalloc for finding memory growth",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "asyncio-unbounded-gather-fanout",
        "title": "Cap asyncio fan-out — `gather(*[...])` over an unbounded list floods connections",
        "when_to_apply": "`await asyncio.gather(*[coro(x) for x in items])` (or a comprehension of `create_task`) runs over an unbounded/large list, launching every coroutine at once. Symptom: thousands of simultaneous HTTP/DB calls blow past connection-pool limits, trip downstream rate limits, and spike memory — often failing worse than a serial loop. The asyncio analog of Node's unbounded Promise.all. Distinct from sync-io-in-async (blocking the loop): here the calls are async but unthrottled.",
        "evidence": [
            "Python docs — asyncio.gather has no concurrency limit",
            "Python docs — asyncio.Semaphore for bounded concurrency",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "cache-stampede-no-lock",
        "title": "Coordinate cache recompute — an unlocked hot-key miss dogpiles the origin",
        "when_to_apply": "An expensive value (query result, rendered fragment, external API response, auth token) is recomputed on cache miss with no coordination, so when it expires under load every concurrent request/worker recomputes it simultaneously (cache stampede / dogpile). Symptom: periodic CPU/DB spikes and latency cliffs exactly at expiry, sometimes cascading into an outage, despite the value being 'cached'. Distinct from having no cache at all: the gap is deduping concurrent misses.",
        "evidence": [
            "dogpile.cache docs — stampede protection",
            "Redis docs — distributed locks for recompute coordination",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "orm-bulk-write-not-used",
        "title": "Batch inserts/updates — row-by-row writes in a loop are O(N) round-trips",
        "when_to_apply": "A loop writes rows one at a time — `for row in rows: session.add(Model(**row)); session.commit()`, `Model.objects.create(...)` per iteration, or `cursor.execute(sql, row)` in a loop — instead of one bulk operation. Symptom: N round-trips (often N commits/flushes) make an import or batch job O(N) in DB round-trips; fine for a few rows, it crawls at thousands and holds a transaction open far too long. Distinct from N+1 reads: this is the write path.",
        "evidence": [
            "SQLAlchemy docs — bulk operations / insertmanyvalues / executemany",
            "Django docs — bulk_create / bulk_update",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    # ─────────── Expansion (4) — fd leak / gather / json encode / regex compile ───────────
    {
        "id": "unclosed-file-or-socket-leak",
        "title": "Close files/sockets with a context manager — leaked descriptors exhaust the process",
        "when_to_apply": "Code opens a file, socket, DB cursor, or `requests`/`httpx` client with a bare `open(...)` / constructor and no `with` block (or no explicit `.close()` in a `finally`), especially inside a loop or a request handler. CPython's refcount usually closes them eventually, but under load, exceptions, or PyPy they linger — file descriptors and connections accumulate. Symptom: the process climbs toward its `ulimit -n` and starts raising `OSError: [Errno 24] Too many open files`, or connections pile up until the DB/pool is exhausted. Distinct from the module-level leak (a growing collection): this leaks OS handles, not heap objects.",
        "evidence": [
            "Python docs — with statement / context managers for resource cleanup",
            "Python docs — EMFILE (too many open files)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "sequential-awaits-not-gathered",
        "title": "Gather independent coroutines — don't `await` them one-by-one",
        "when_to_apply": "An async function `await`s several independent coroutines in sequence — `a = await fetch_a(); b = await fetch_b(); c = await fetch_c()` — where none needs the previous result. Total time becomes the SUM instead of the MAX of the calls. Symptom: an async endpoint's latency is the added-up time of its independent dependencies even though the event loop could run them concurrently. The asyncio analog of serializing independent Promises. Distinct from asyncio-unbounded-gather-fanout (too many at once) and sync-io-in-async (blocking the loop): here a small fixed set is needlessly serial.",
        "evidence": [
            "Python docs — asyncio.gather for concurrent coroutines",
            "Python docs — asyncio Tasks and concurrency",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "large-json-serialize-blocking",
        "title": "Stream or offload large JSON responses — `json.dumps` of a big object blocks the worker",
        "when_to_apply": "A handler builds a large response by serializing a big list/dict with `json.dumps` (or returns a huge dict the framework serializes) in one synchronous call. Serialization is CPU-bound and holds the GIL, so a multi-MB response stalls the worker (and, in async apps, the event loop) for the whole encode. Symptom: endpoints that return large exports/reports spike latency and pin a CPU; concurrent requests queue behind the encode. Distinct from the N+1 / query rules (fetching the data): this is the cost of *encoding* it. Mirrors Node's large-json-serialize-sync.",
        "evidence": [
            "Python docs — json module performance",
            "orjson / ujson — faster JSON encoders",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "regex-recompiled-per-call-py",
        "title": "Compile regexes once at module scope — recompiling per call burns CPU on hot paths",
        "when_to_apply": "A hot function calls `re.match` / `re.search` / `re.sub` with a string-literal (or dynamically-built) pattern, or calls `re.compile(...)` inside the function/loop, so the pattern is recompiled on every call. `re`'s internal cache is small (512 entries) and is bypassed entirely when the pattern is built dynamically. Symptom: a validation/parse/routing function that runs per request shows measurable CPU in `re._compile`; throughput drops under load. Distinct from ReDoS (a catastrophic pattern): this is the compile cost of an otherwise-fine regex. Mirrors Node's regex-recompiled-per-call.",
        "evidence": [
            "Python docs — re.compile and the module-level pattern cache",
            "Python docs — compile patterns used more than once",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    # ─────────── Cross-runtime parity (3) — body limits / graceful shutdown / HTTP caching ───────────
    {
        "id": "unbounded-request-body",
        "title": "Cap request body size — an unbounded upload can exhaust memory",
        "when_to_apply": (
            "A FastAPI / Starlette / Flask / Django endpoint reads the whole request body "
            "into memory (`await request.body()`, `await request.json()`, `request.data`, "
            "`request.files`) with no size limit, and there is no body-size cap at the proxy "
            "either. FastAPI/Starlette do NOT cap body size by default. Symptom: a single large "
            "POST — or a slow drip that never ends — inflates worker RSS until the process is "
            "OOM-killed, taking every concurrent request on that worker down with it. Distinct "
            "from a slow handler: the danger is memory, and one request can trigger it."
        ),
        "evidence": [
            "Starlette docs — request body / streaming",
            "OWASP — unrestricted upload / resource exhaustion",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "asgi-no-graceful-shutdown",
        "title": "Drain in-flight requests on shutdown — don't drop them on SIGTERM",
        "when_to_apply": (
            "An ASGI/WSGI service (uvicorn/gunicorn/hypercorn behind FastAPI/Starlette/Django) "
            "has no graceful-shutdown handling: on deploy or autoscale-down the orchestrator sends "
            "SIGTERM and the process exits immediately, cutting active requests and background "
            "`asyncio` tasks mid-flight and closing DB/Redis pools abruptly. Symptom: every deploy "
            "produces a burst of 502s and half-finished jobs; users see errors during rollouts even "
            "though nothing is actually broken. Mirrors Node's no-graceful-shutdown rule."
        ),
        "evidence": [
            "uvicorn docs — timeout-graceful-shutdown",
            "Kubernetes docs — termination lifecycle / SIGTERM grace period",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "python-missing-cache-headers",
        "title": "Send Cache-Control + ETag on cacheable GETs so clients stop re-fetching",
        "when_to_apply": (
            "A GET endpoint returns data that rarely changes (config, catalog, reference/public "
            "content) but sends no `Cache-Control`, `ETag`, or `Last-Modified`, so browsers, CDNs, "
            "and proxies re-request and re-serialize it on every view. Symptom: origin request "
            "volume and CPU scale with traffic even for near-static responses, and conditional "
            "requests can never return 304. Distinct from in-process caching (`lru_cache`, stampede "
            "protection): this is HTTP response caching at the client/CDN edge, and it is a real "
            "Python gap because the existing Python cache rules are in-process only."
        ),
        "evidence": [
            "MDN — HTTP caching (Cache-Control, ETag, 304)",
            "RFC 9111 — HTTP Caching",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    # ─────────── Circuit lens — graph/wiring fault detectors ───────────
    {
        "id": "py-retry-redirect-loop",
        "title": "Break retry/redirect cycles — a request path that round-trips to itself is an oscillating loop",
        "when_to_apply": (
            "A request path forms a closed cycle: a view returns a redirect to a URL whose own "
            "guard redirects straight back (Django `LOGIN_URL` pointing at a page that itself "
            "requires login, FastAPI/Starlette middleware bouncing between two routes whose "
            "conditions never settle), two services retry INTO each other on failure (A's "
            "fallback calls B, B's fallback calls A via `tenacity`/celery retries), or a client "
            "hammers the same endpoint after every non-2xx while the handler's own upstream "
            "retry does the same — multiplying attempts per lap. Symptom: bursts of identical or "
            "alternating requests in tight succession, redirect chains ending in the browser's "
            "too-many-redirects error, load that spikes exactly when an upstream degrades, and "
            "duplicate side effects when a non-idempotent view runs once per lap. Distinct from "
            "plain retry amplification (one call retried too hard): this is a loop that feeds "
            "itself. Boosthis sees the repeated hop pattern in the cross-runtime trace and flags "
            "the cycle (route labels + counts only)."
        ),
        "evidence": [
            "Google SRE Book — cascading failures and retry amplification",
            "RFC 9110 — 3xx redirection and redirect-loop behavior",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-fanout-overload",
        "title": "Bound per-request fan-out — one request driving N concurrent downstream calls is over-current",
        "when_to_apply": (
            "A single incoming request fans out to an anomalously high number of concurrent "
            "downstream calls: `asyncio.gather(*(client.get(...) for item in items))` with "
            "unbounded N (the service-layer N+1), an aggregator endpoint that calls every "
            "internal service on each hit, or a celery task spawning one HTTP call per row. "
            "Symptom: downstream services see your traffic multiplied N×, the connection pool "
            "and file descriptors exhaust, p99 tracks the slowest of the N calls, and one hot "
            "endpoint can effectively DoS your own internal APIs. Distinct from "
            "n-plus-one-orm-query (database queries inside a loop): this is service-to-service "
            "fan-out at the request layer. Boosthis counts concurrent outbound spans per request "
            "in the trace and flags outlier fan-out (counts + coarse buckets only)."
        ),
        "evidence": [
            "Google SRE Book — Handling Overload (fan-out and load amplification)",
            "asyncio docs — bounding concurrency with Semaphore / TaskGroup",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-highest-leverage-node",
        "title": "Fix the highest-leverage route first — centrality × latency beats absolute-slowest",
        "when_to_apply": (
            "You must pick the next performance fix and several endpoints look slow. One route "
            "usually carries far more of the aggregate experience than any other — the gateway "
            "view every page calls, the auth dependency in every FastAPI router — and fixing a "
            "mildly slow route that EVERY flow crosses moves overall p75 more than perfecting "
            "the absolute-slowest, rarely-hit endpoint. Symptom of getting it wrong: "
            "optimization effort poured into the slowest endpoint on the dashboard while "
            "user-perceived latency barely moves, because the traffic-weighted constraint went "
            "untouched. Boosthis ranks each route by how many traced flows cross it (centrality) "
            "combined with its latency/failure contribution, naming the single highest-leverage "
            "fix — a ranking layer over spans already stored, no new collection."
        ),
        "evidence": [
            "Theory of Constraints — throughput is set by the constraint; improve the constraint first",
            "Betweenness centrality (graph theory) — the node on the most paths dominates aggregate latency",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-cut-vertex-spof",
        "title": "Know your cut-vertex routes — one route every flow funnels through is a single point of failure",
        "when_to_apply": (
            "One route or internal service is an articulation point of your request graph: "
            "every traced flow funnels through it (a shared gateway view, a single auth "
            "dependency, one internal API every feature calls). If it degrades, everything "
            "behind it is blocked at once — and per-route latency meters give no warning, "
            "because the risk is structural concentration, not current slowness. Symptom: a "
            "single deploy or dependency blip takes out most user flows simultaneously, and the "
            "postmortem discovers 'everything goes through X'. Boosthis finds cut vertices in "
            "the cross-runtime trace graph and flags the concentration risk (route labels + "
            "structure only)."
        ),
        "evidence": [
            "Graph theory — articulation points / cut vertices disconnect the graph when removed",
            "Power-grid N-1 contingency planning: no single element's loss may take down the network",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-crash-cascade",
        "title": "Trace the crash cascade — 'when A raises, B fails next' is one fault, not two",
        "when_to_apply": (
            "Exception/crash signatures fire in correlated sequences across your service graph: "
            "when upstream view A starts raising, downstream consumer B's own signature fires "
            "next — half-written state, a malformed fallback response, or timeouts surfacing as "
            "exceptions one hop later. Treating each signature in isolation hides the blast "
            "radius, so you patch the downstream symptom while the upstream trip keeps firing. "
            "Symptom: 'independent' error signatures that always spike together in the same "
            "window, in the same order. Boosthis correlates code-derived crash signatures along "
            "trace edges — 'when A fires, B tends to follow' — and surfaces the directional "
            "pair (signatures + counts only, never raw messages)."
        ),
        "evidence": [
            "Power-grid cascading-failure analysis — one trip overloads and trips the next element",
            "Boosthis crash-risk feed (code-derived signatures) — this rule adds the along-the-trace correlation",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    # ─────────── Cross-language wave 1 (6) — concepts proven in other runtimes ───────────
    {
        "id": "py-cancel-not-remote",
        "title": "Cancelling a task or timing out a request does not undo the remote operation",
        "when_to_apply": "Async code cancels an asyncio task wrapping an HTTP call (or an httpx/aiohttp/requests timeout fires) and the caller behaves as if the operation never happened — immediately retrying a mutation, or skipping cleanup because the task 'was cancelled'. Cancellation raises CancelledError at the next await point on the CLIENT; the server usually completes the work it already received, and a swallowed CancelledError (a bare `except:` or `except Exception:` around the await) can leave the response/connection unclosed. Symptom: timeout-then-retry double-writes upstream (duplicate orders, duplicate rows), and cancelled requests leak pooled connections until the pool exhausts.",
        "evidence": [
            "Python docs — asyncio Task.cancel raises CancelledError at the next await; it is client-side only",
            "httpx docs — timeouts raise; always close responses (context managers) so pooled connections are returned",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-bootstrap-self-gate",
        "title": "Never gate first-run registration on state only the registration call can clear",
        "when_to_apply": "Service or worker startup suppresses the first registration/handshake/enrollment call behind a flag that only that same call's success would set — `if not settings.registered: return` before the register call, a readiness gate satisfied only by the bootstrap response, or a first-contact call queued behind a credential it would itself mint. Symptom: a fresh deployment runs forever unregistered with no error and no outbound attempt — the gate can never open without the call it blocks — and the deadlock hides in development where a leftover credential file satisfies the guard.",
        "evidence": [
            "Boosthis production incident (Jul 2026) — a first-run call gated on its own result left fresh installs permanently unregistered",
            "Control-systems deadlock: a guard whose only unlocking event is the action it blocks",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-remote-data-null-guard",
        "title": "Check external dict/object data before nested access — None and missing keys are the normal case",
        "when_to_apply": "A handler dereferences nested structure straight off external data — `request.json()['user']['email']`, `payload['items'][0]['id']` from an upstream API, an ORM/cache result assumed present (`row.settings.plan` when the query returned None). Request bodies can be absent or non-JSON, upstream payloads go partial on errors and version skew, and .get()/queries return None on misses. Symptom: KeyError / TypeError ('NoneType' object is not subscriptable) 500s that appear only for malformed clients, upstream degradation, or empty query results — each an unhandled path an attacker can hit at will.",
        "evidence": [
            "Python docs — dict.get and EAFP vs LBYL at data boundaries",
            "pydantic docs — validate external payloads into typed models at the edge instead of raw dict access",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-unsafe-payload-parse",
        "title": "Decode untrusted payloads defensively — json.loads and response.json() raise on real-world bodies",
        "when_to_apply": "`json.loads(...)` or `response.json()` runs on untrusted content with no exception handling and no status/content-type check — a webhook body, a queue message, a file read, or an upstream API that returns an HTML error page on 502/maintenance. Symptom: one malformed payload raises json.JSONDecodeError inside the handler (a 500), the log shows the parse error instead of the upstream failure the body was actually describing, and a single poison message can lock a queue consumer into a crash-redeliver loop.",
        "evidence": [
            "Python docs — json.loads raises JSONDecodeError on malformed input",
            "requests/httpx docs — check response.status_code and content type before .json()",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-transport-must-throw",
        "title": "Retrying HTTP clients must raise on transport failure — never return a synthetic response",
        "when_to_apply": "A retry/timeout wrapper around requests/httpx/aiohttp catches transport-level exceptions (ConnectionError, ConnectTimeout, ReadTimeout, TLS errors) and RETURNS a synthetic response object or None instead of re-raising. Since the retry path only fires on an exception, the synthetic value is treated as a final answer and the recovery logic silently never engages. Symptom: retries never trigger on exactly the failures they exist for — one connection reset becomes a permanent failure with the retry counter at zero, and downstream code branches on a fabricated status it was never designed for.",
        "evidence": [
            "Boosthis production incident (Jul 2026) — a transport adapter returned a synthetic status-0 response, skipping the retry-over-dead-socket path entirely",
            "urllib3 docs — Retry operates on raised connection/read errors; there is no HTTP status for a failed transport",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-single-event-perf-logging",
        "title": "Write related timings as ONE structured log/metric event per request or task",
        "when_to_apply": "Per-request instrumentation emits separate log records for each phase — one for DB time, one for the external API call, one for total — so no single record holds the full latency breakdown of one request. Symptom: correlating 'requests where the DB was slow AND the total was slow' requires joining interleaved log lines on a request id; records get sampled or dropped independently, and the join breaks exactly during incidents when volume spikes. One structured record per request (all durations as fields) makes every such question a single filter.",
        "evidence": [
            "structlog docs — bind per-request context and emit one wide event with all fields",
            "Honeycomb — wide events: one event per request with many fields beats many narrow events",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-vendored-copy-drifts-from-source",
        "title": "Regenerate generated/vendored artefacts in CI and fail on diff — never hand-edit the copy",
        "when_to_apply": "The repo embeds a GENERATED or COPIED artefact that duplicates a source of truth — a committed migration generated from models, a `requirements.txt`/`poetry.lock`/`pip-compile` lockfile, generated protobuf/gRPC stubs or OpenAPI clients, a vendored package under a local `vendor/`/`_vendor` dir, or a duplicated settings file — and code edits the SOURCE while the embedded copy keeps its old bytes. Symptom: nothing raises and tests pass, but imports resolve the STALE committed copy (an old generated stub, a lockfile pinning a version the code no longer expects), so behaviour silently diverges from the source. Both files are valid Python/text, so the drift never errors — it just serves the past.",
        "evidence": [
            "Boosthis production incident (Aug 2026) — a generated client edited at source kept serving old bytes because the committed copy was not regenerated in CI",
            "pip-tools / Alembic docs — generated lockfiles and migrations must be produced by their generator and diffed in CI, never hand-edited",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "py-restart-old-process-holds-port",
        "title": "A restart must fail loud on address-in-use and prove the new workers own the port",
        "when_to_apply": "A restart/reload of a WSGI/ASGI server (Gunicorn, Uvicorn, `--reload`) stops the old master by name or signal but an OLD worker/master survives — an orphaned Gunicorn worker, a detached `uvicorn --reload`, or a supervisor respawn that misses the previous pid. Symptom: the new master dies with 'Address already in use' with `SO_REUSEADDR`/`SO_REUSEPORT` OFF, OR — with reuseport ON or a fallback to another port — the OLD workers keep accepting connections and serving the PREVIOUS code while the deploy reports success. Health checks pass because the socket answers; the version running is not the version deployed.",
        "evidence": [
            "Boosthis production incident (Aug 2026) — a reload left an orphaned Gunicorn worker serving stale code on the same reuseport socket after a deploy",
            "Gunicorn docs — graceful reload (HUP/USR2) and reaping old workers; a bind failure must abort the boot, not silently rebind",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "py-observer-never-attached",
        "title": "Assert middleware/instrumentation is registered at boot and prove one real request moves the metric",
        "when_to_apply": "An ASGI/WSGI middleware, a Django/Flask/FastAPI request hook, a `prometheus_client` Counter/Histogram wrapper, or an OpenTelemetry instrumentation is written and imports fine, but nothing wires it into the app — the middleware is never added to `MIDDLEWARE`/`app.add_middleware`, the signal receiver is never connected, or the instrumentor's `.instrument()` is never called. Symptom: the metric stays at zero forever, its dashboard tile is empty or perpetually warming, and every unit test passes because the middleware/handler is exercised directly with a fake request instead of a real one routed through the assembled app.",
        "evidence": [
            "Boosthis production incident (Aug 2026) — a latency middleware was written and unit-tested but never added to the app, so its metric read zero in prod",
            "prometheus_client / OpenTelemetry docs — an instrument only moves when it is registered and observed on the live request path",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    # ─────────── v0.4.2 — fixed-ms timing assert (cross-runtime) ───────────
    {
        "id": "py-fixed-ms-timing-assert",
        "title": "Poll to a deadline instead of `time.sleep` plus a hardcoded millisecond assert",
        "when_to_apply": "A test, gate, health check, or readiness probe hinges on a FIXED number of milliseconds — it `time.sleep(0.1)`s and then asserts a side effect happened, or measures a `perf_counter()`/`monotonic()` delta and asserts `elapsed < 0.05`, or a liveness/readiness `periodSeconds`/`timeoutSeconds` is tuned to one machine. Wall-clock time measures the MACHINE, not the code: on a shared CI runner, a throttled container, or a laptop mid-build the same code runs slower, so the check SCREAMS when nothing is wrong; on an idle box it stays quiet even after the code got slower. Both directions are bugs. Symptom: a green suite that goes red only on a loaded runner (or under `pytest-xdist` parallelism), plus a `pytest-timeout` used as a speed grader rather than a hang catcher — flakiness blamed on 'CI being slow' that is really the assertion measuring the box.",
        "evidence": [
            "Boosthis production incident (Aug 2026) — a gate asserted a finish-time by reading the clock the instant it ran instead of waiting for it with a bounded poll, so it failed on a loaded runner while the code was correct",
            "pytest-timeout / freezegun docs — a timeout is a HANG bound in seconds, not a latency assertion; freeze or monkeypatch the clock instead of sleeping real time",
        ],
        "category": "case-study",
        "languages": ["python"],
    },
    {
        "id": "py-credentialed-refusal-signin-challenge",
        "title": "Do not challenge a caller whose credential was understood",
        "when_to_apply": "In Django FastAPI or Flask middleware, a request that carried an authorization header receives status 401 plus an authenticate challenge even though the credential was understood but expired out of scope or forbidden for this resource. The caller discards a usable identity and treats the fast refusal as an outage.",
        "evidence": [
            "Django FastAPI and Flask authentication middleware documentation",
            "HTTP authentication status and challenge semantics",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "py-generic-refusal-message",
        "title": "Give every refusal a distinct actionable code and message",
        "when_to_apply": "In Django FastAPI or Flask middleware, different refusal causes return byte identical bodies with no stable machine code. Callers cannot distinguish expired credentials missing scope throttling and policy denial, so every refusal looks like the same outage.",
        "evidence": [
            "Django REST Framework and FastAPI exception handler documentation",
            "HTTP problem detail and stable error code guidance",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "py-strict-credential-scheme-parsing",
        "title": "Normalize credential schemes and token whitespace before parsing",
        "when_to_apply": "In Python authentication middleware, credential parsing compares the scheme with exact letter case or splits on one literal space and leaves surrounding token whitespace quotes or line breaks intact. A harmless paste variation is refused as if the service were down.",
        "evidence": [
            "Django FastAPI and Flask request header documentation",
            "HTTP authentication scheme parsing guidance",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "py-shared-allowance-no-caller-key",
        "title": "Partition shared allowances by caller and keep a global backstop",
        "when_to_apply": "In Python, django ratelimit SlowAPI or a Redis counter uses one process wide or route wide counter for every caller. One noisy caller exhausts the allowance and unrelated callers receive refusals until the shared window resets.",
        "evidence": [
            "Django ratelimit and SlowAPI key function documentation",
            "OWASP denial of service and rate limiting guidance",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "py-silent-protection-fail-open",
        "title": "Surface every degraded protection even when it fails open",
        "when_to_apply": "In Python, an authorization quota validation or rate limit gate catches a backing store timeout missing configuration or dependency failure and allows the request with no metric log or health signal. The protection disappears while the service still looks healthy.",
        "evidence": [
            "Django FastAPI and Flask middleware error handling documentation",
            "Security control degraded mode observability guidance",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "py-validated-name-resolved-again",
        "title": "Do not reconnect by name after validating an address",
        "when_to_apply": "Code accepts a webhook callback import or proxy URL, resolves its hostname with socket.getaddrinfo and checks the resulting ipaddress values, then passes the original hostname to requests httpx urllib3 or aiohttp, or enables automatic redirects. The client performs another DNS lookup or follows a redirect without the same check, so the socket can reach localhost a private range or a cloud metadata address that validation rejected.",
        "evidence": [
            "Python socket.getaddrinfo and ipaddress documentation",
            "urllib3 and aiohttp connection and redirect documentation",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-logging-hook-replaced",
        "title": "Do not replace host logging and exception hooks",
        "when_to_apply": "A library or bootstrap calls logging.basicConfig with force enabled, clears logging.getLogger root handlers, calls logging.setLogRecordFactory without wrapping the previous factory, or assigns sys.excepthook or threading.excepthook without invoking the prior hook. Host levels formatters destinations redaction or uncaught exception reporting silently stop applying.",
        "evidence": [
            "Python logging basicConfig and setLogRecordFactory documentation",
            "Python sys.excepthook and threading.excepthook documentation",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "py-nonreentrant-lock-reacquired",
        "title": "Do not reacquire a nonreentrant lock while holding it",
        "when_to_apply": "Code enters threading.Lock or asyncio.Lock and while still inside that critical section calls a helper callback property observer or coroutine that acquires the same lock. These primitives are not reentrant, so the owning thread or task waits on itself forever even though no competing worker exists.",
        "evidence": [
            "Python threading Lock and RLock documentation",
            "Python asyncio synchronization primitive documentation",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "py-retry-leaves-attempt-running",
        "title": "Do not retry while the abandoned attempt is still running",
        "when_to_apply": "Code uses asyncio.wait with a timeout, shields a task from asyncio.wait_for cancellation, or times out a concurrent.futures Future and immediately starts another attempt without cancelling and awaiting the first task or closing its response. The old task thread socket or pooled connection remains live, so each retry adds work instead of replacing it.",
        "evidence": [
            "Python asyncio task cancellation and wait documentation",
            "Python concurrent futures cancellation documentation",
            "httpx and aiohttp response lifecycle documentation",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "ai-call-no-timeout",
        "title": "Put a deadline on every AI provider call",
        "when_to_apply": "A request handler calls openai-python, anthropic-python, or an AI endpoint through httpx without a client/request `timeout=` or an enclosing `asyncio.timeout()`. AI generation can take minutes or stall, so one slow answer keeps the request, connection, and worker or coroutine open indefinitely.",
        "evidence": [
            "HTTPX docs — timeouts",
            "OpenAI Python SDK — timeout and max_retries options",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "ai-retry-ignores-retry-after",
        "title": "Honor the AI provider's Retry-After before retrying",
        "when_to_apply": "Code catches an openai-python, anthropic-python, or httpx 429/overload error and sleeps for its own delay without first reading `Retry-After` from the response headers. Retrying before the provider's published deadline sustains the rate limit and spends attempts on guaranteed refusals.",
        "evidence": [
            "RFC 9110 — Retry-After",
            "OpenAI API docs — rate limits",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "ai-calls-serial",
        "title": "Run independent AI calls concurrently with asyncio.gather",
        "when_to_apply": "Independent openai-python, anthropic-python, or httpx AI calls are awaited one after another in the same async request even though no call consumes another call's result. Since each takes seconds, serial awaits add their latencies; create the coroutines together and await `asyncio.gather(...)`.",
        "evidence": [
            "Python docs — asyncio.gather",
            "OpenAI Python SDK — async client",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "ai-duplicate-prompt",
        "title": "Send each logical AI prompt only once per request",
        "when_to_apply": "One incoming request sends identical messages or prompt input to the same model more than once through openai-python, anthropic-python, or httpx, commonly through duplicate helper paths or a retry after success. The duplicate pays for the same tokens twice and usually discards one answer.",
        "evidence": [
            "OpenAI API docs — token usage",
            "Anthropic API docs — usage fields",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "ai-prompt-cache-cold",
        "title": "Keep large shared prompt prefixes stable so provider caching hits",
        "when_to_apply": "Large system instructions, tool schemas, or retrieved documents are repeatedly sent with openai-python or anthropic-python but cached-input usage remains zero. Timestamps, random ordering, or request-specific text before the shared material prevent prompt-cache reuse; keep the reusable prefix stable and put changing content last.",
        "evidence": [
            "OpenAI API docs — prompt caching",
            "Anthropic docs — prompt caching",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "ai-stream-usage-not-requested",
        "title": "Request usage metadata on every streamed AI response",
        "when_to_apply": "An OpenAI-family stream created with openai-python or raw httpx omits `stream_options={\"include_usage\": True}`. The application receives text chunks but no final usage block, so it cannot measure or attribute the stream's input, output, cached tokens, or cost.",
        "evidence": [
            "OpenAI API reference — stream_options.include_usage",
            "OpenAI Python SDK — streaming responses",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    # ───────── Outside services (4) — the dependency reading's own fixes ────
    # The four shapes the outside-services meter can actually see, named here
    # so a founder who learns "sign-in is what is slow" is handed a repair
    # rather than a worry. Generic rules already cover a missing deadline and
    # serialized awaits; these stay separate because the reading that raises
    # them is about a DEPENDENCY — sign-in, uploads, payments, messaging,
    # stored-knowledge search — and the advice differs when the slow thing is
    # somebody else's service you cannot make faster.
    {
        "id": "dependency-call-no-timeout",
        "title": "Put a deadline on every outside-service call — sign-in first",
        "when_to_apply": "A handler calls an outside service (an identity provider, an object store, a payment gateway, an email/SMS sender, a vector or search service) through httpx, aiohttp, requests, or a vendor SDK with no `timeout=` and no enclosing `asyncio.timeout()`. requests and httpx both default to waiting FOREVER, so the omission is silent. Symptom: the dependency degrades rather than fails, and because nothing gives up, your own request never ends either — under a synchronous worker model each stalled call also holds a whole worker, so a handful of them take the service down. Sign-in is the worst place for this: it sits in front of every session, so a stalling identity provider makes the product look down before a page renders.",
        "evidence": [
            "HTTPX docs — timeouts (a request with timeout=None waits forever)",
            "Google SRE Book — Addressing Cascading Failures (a dependency that is slow is more dangerous than one that is down)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "dependency-retry-uncapped",
        "title": "Cap retries against an outside service — an unbounded retry turns their bad minute into your outage",
        "when_to_apply": "Code retries a failed outside-service call with no attempt ceiling, no total-elapsed budget, no jitter, or no handling for 429 and `Retry-After` — a bare `while True` around a request, an urllib3 `Retry` with a high total, or a tenacity decorator with no stop condition. Symptom: the dependency has a bad minute, every in-flight request starts retrying at once, and the retries become the load that keeps it down, while each request's latency quietly multiplies by the attempt count. Boosthis sees this as a kind whose call count far exceeds its request count with clustered failures.",
        "evidence": [
            "RFC 9110 — Retry-After",
            "Google SRE Book — Handling Overload (retry amplification)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "dependency-calls-serial",
        "title": "Start independent outside-service calls together, not one after another",
        "when_to_apply": "One request awaits several outside-service calls in sequence — verify the session, then fetch the avatar from object storage, then look up the customer at the payment gateway — where no call consumes the previous call's result. Symptom: the request's wait is the SUM of every dependency's latency instead of the slowest one, so a page that should wait 200ms waits 700ms and no single service looks slow enough to blame. In a synchronous handler the calls cannot be gathered at all, which is itself the finding: the work belongs on an async path or in a thread pool.",
        "evidence": [
            "Python docs — asyncio.gather",
            "Google SRE Book — Latency (serial dependencies add, parallel dependencies max)",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    {
        "id": "dependency-on-visitor-path",
        "title": "Take a dependency off the visitor's path when the visitor does not need its answer",
        "when_to_apply": "A request the user is waiting on calls an outside service whose result it never returns — sending a welcome email or an SMS, writing an audit event to a logging service, syncing a record to a CRM, warming a search index, uploading a derived thumbnail. Symptom: the visitor pays the full latency (and inherits the full failure) of work whose answer they will never see, so a slow email provider makes signup slow and a broken one makes signup fail. Distinct from py-background-work-needs-scheduler (work with no runner at all): this work already has a runner — the visitor's own request — and that is exactly the problem.",
        "evidence": [
            "Google SRE Book — Addressing Cascading Failures (shed non-critical work from the serving path)",
            "Boosthis outside-services reading — a dependency whose kind never contributes to the response body",
        ],
        "category": "industry",
        "languages": ["python"],
    },
    # ───────── Background work (4) — jobs, queues and schedules ─────────
    # The four shapes the background-work meter can actually see. Each is
    # named here because a number with no fix beside it is a complaint, not a
    # repair: a developer told their jobs overlap and nothing else has been
    # given a worry. Deliberately about the JOB, never about slowness — a
    # nightly import is supposed to take an hour.
    {
        "id": "py-job-without-time-limit",
        "title": "Give every background task a deadline — a hung run is invisible without one",
        "when_to_apply": "A Celery task, an RQ job, an APScheduler job or a bare thread loop runs with no time limit of its own: no Celery time_limit/soft_time_limit, no RQ job_timeout, no asyncio.timeout or requests/httpx timeout on the calls inside it. Symptom: a run that hangs on a socket that never answers, a lock never granted, or a third-party call with no timeout occupies its worker slot forever. Nothing raises, nothing is logged, and no error tracker fires, because from the worker's point of view the task is still working. Concurrency silently drops by one with each hung run until the queue stops draining; the first visible sign is a backlog with a healthy-looking worker attached. Distinct from a slow task: a slow task finishes.",
        "evidence": [
            "Celery documentation — time_limit and soft_time_limit; a task with neither runs until the worker is restarted",
            "Python requests documentation — a request with no timeout can hang indefinitely, and the default is no timeout",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "py-job-retries-forever",
        "title": "Cap task retries and send the last failure somewhere",
        "when_to_apply": "A task is declared with unlimited retries, or the handler catches its own failure and re-queues itself, with no attempt ceiling and no dead-letter destination: Celery autoretry_for with max_retries=None (or an explicit self.retry() with no ceiling), an RQ job re-enqueued from inside its own except block, an APScheduler job that reschedules itself on failure. Symptom: a task that can never succeed — a deleted row, a permanently rejected payload, a revoked credential — is retried for the life of the system, consuming a worker slot on every cycle and costing money at every external call. Queue depth stays flat so a backlog alarm never fires, and the same log line appears so often nobody reads it. The bug is not the failure; it is that the failure has no end and no destination.",
        "evidence": [
            "Celery documentation — max_retries, retry_backoff and the fact that max_retries=None retries forever",
            "RQ documentation — Retry(max=...) and the failed job registry as a destination",
            "AWS Architecture guidance — dead-letter queues exist so a poison message leaves the main queue",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "py-scheduled-job-overlaps-itself",
        "title": "Stop a scheduled task starting again while the last run is still going",
        "when_to_apply": "A recurring task fires on a fixed cadence — APScheduler with default max_instances, a Celery beat schedule, a cron entry, a threading.Timer loop — with no guard against a run that has not finished. Symptom: the moment one run takes longer than the interval, two copies run at once, then three; they compete for the same rows, the same database pool and the same external rate limit, so each is slower than the last and the overlap compounds. The classic damage is duplication rather than slowness: the same email sent twice, the same charge attempted twice, a counter incremented by two runs that both read the old value. Note that a module-level flag or a threading.Lock guards ONE process — the moment a second worker or a second web process runs the same schedule, each has its own copy and both believe they are alone.",
        "evidence": [
            "APScheduler documentation — max_instances defaults to 1 per job but misfire_grace_time and multiple scheduler processes each run their own copy",
            "Boosthis background-work meter (Aug 2026) — overlapping runs are counted per job name precisely because they are invisible in any per-run log",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    {
        "id": "py-queue-falling-behind",
        "title": "Measure how long a task WAITED, not just how long it ran",
        "when_to_apply": "A worker records how long each task took but never how long it sat in the queue first — no comparison against Celery's task.request or the message's published timestamp, no RQ enqueued_at/started_at gap, no queue-length reading anywhere. Symptom: every run is fast and every dashboard is green while users wait minutes for work the system reports as instant. The service is not slow, it is late, and the two are measured in different places. A queue falling behind is only visible in the gap between enqueue and start, which nobody is watching — and when it is finally noticed the reflex is to add workers, which cannot help when the real cause is one task class saturating a downstream limit.",
        "evidence": [
            "RQ documentation — a job carries enqueued_at and started_at, so the wait is available separately from the run",
            "Google SRE Book — Monitoring Distributed Systems: saturation and latency are separate signals; a fast handler on a growing backlog is a saturated system",
        ],
        "category": "operational",
        "languages": ["python"],
    },
    # ─────────── Account-size scaling (1) — page cost that grows with the customer ───────────
    {
        "id": "py-page-cost-scales-with-account",
        "title": "Keep a view at a fixed query count as the account grows — check it against a large account, not the dev fixture",
        "when_to_apply": "A Django view, DRF serializer, Flask or FastAPI endpoint does more work the more data the account holds: a related attribute is touched inside the loop that builds the response so the ORM emits one SELECT per row, or the page headline is a .count() or func.sum() over the account's whole history recomputed on every request. Symptom: the endpoint is instant against the dev fixture with three rows, and no individual query is slow in production either, yet the page is visibly slower for the customer with the largest workspace and gets slower every month. Per-query monitoring cannot see it because every query is fast; what grows is how many of them run and how many rows they scan. Distinct from the N+1 rule, which adds select_related/prefetch_related to one named relation, and from the unbounded-query rule, which caps the rows a single query returns — this asks whether the WHOLE view holds a fixed budget as the account grows, and whether anyone has rendered it against a large account to find out.",
        "evidence": [
            "Django documentation — QuerySet.annotate with Count/Sum computes per-parent aggregates inside the parent query rather than one query per row",
            "Boosthis dashboard audit (Sep 2026) — a read inside a render loop and a whole-history aggregate made page cost track account size while every individual query stayed fast",
        ],
        "category": "industry",
        "languages": ["python"],
    },
]
CHECKLIST_VERSION: Final[str] = "0.4.3"
CHECKLIST_COUNT: Final[int] = len(BOOSTHIS_CHECKLIST)


def get_checklist_entry(rule_id: str) -> ChecklistEntry | None:
    """Look up a single rule by ID; returns None if not found."""
    for entry in BOOSTHIS_CHECKLIST:
        if entry["id"] == rule_id:
            return entry
    return None


def list_checklist_ids() -> list[str]:
    """Return all rule IDs in declaration order."""
    return [entry["id"] for entry in BOOSTHIS_CHECKLIST]
