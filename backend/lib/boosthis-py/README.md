# One codebase can hold more than one project

Set `BOOSTHIS_PROJECT_KEY_PY=<YOUR_PROJECT_KEY>` when the Python app must
report to a different project from another runtime in the same repository.
`BOOSTHIS_PROJECT_KEY_PYTHON` is also accepted. Resolution is: the Python
variable, the key passed in code, the saved Python config key,
`BOOSTHIS_PROJECT_KEY`, then `BOOSTHIS_INVITE_KEY`. Run
`boosthis verify --project-key <YOUR_PROJECT_KEY>`; verify prints only a masked
value and hash prefix and fails when the effective key differs.

# boosthis (Python)

Current runtime version `1.0.0a154`.

<!-- KIT_CHANGELOG
1.0.0a154 — Recurring AI findings now merge safely across workers and
count a returned release only once. Release counts describe separate builds,
not necessarily consecutive ones.

1.0.0a153 — An axis band with equal, reversed or non-finite good and poor
constants is now refused with InvertedAxisBandError rather than scored.
This exposes a typo in the kit's own scoring thresholds instead of
reporting a misleading perfect verdict.

1.0.0a152 — This kit now remembers the AI anti-patterns it has
already found in your app. Duplicate prompts, serial AI calls, a
retry without the wait the provider asked for, a call with no time
limit: each of those was recomputed from scratch in every process
and forgotten when it ended, so the same finding arrived as new on
every deploy and nothing could tell a one-off from the fourth
release running. The counts are kept in the store this kit already
uses for its own findings — how many separate runs and how many
separate releases a pattern has appeared in, how long ago it was
first and last seen, and whether this run has seen it at all — and
they ride the AI reading your assistant already receives. A
pattern that has stopped keeps its row, with when it stopped and
nothing about why: this kit cannot tell a fix from a route that
simply went quiet. Nothing new leaves your process — no prompt, no
prompt hash, no hostname, no path, no model name, only counts and
the name of the pattern. forget() and a withdrawn consent clear
the memory with the rest of the local state, and a host with
nowhere durable to write says so rather than reporting that
nothing repeats.

1.0.0a151 — The auto performance budget — the one reading this kit LEARNS
rather than reads off a typed threshold — no longer compares your
app with itself minutes ago. The baseline is now frozen once and
kept, and the window judged against it is drawn only from samples
taken strictly after that baseline closed, so the two never share
a sample. Before, both were re-derived from the same shared
1000-sample ring on every read: on a busy app the yardstick
drifted upward with the traffic, so a route that got twice as slow
over an hour never tripped. A route's first five samples are now
excluded from the baseline as warm-up, so a cold start is no
longer the standard your warm app is held to. A route the shared
ring dropped now answers "evicted" rather than sitting in
"learning" for ever, and says what would let it be judged. Every
answer carries the name of the algorithm that produced it and both
spans it was drawn over. Resilience no longer scores the
regression component 100 when no route carries a baseline: the
component withdraws and the caption says so.

1.0.0a150 — The install guide now names every place this kit reads a request
body, a response body, a header, a cookie or a query value inside your
process, with the limit on each. It named one of them before — the AI-call
meter's prompt prefix — and called it the only one, which was wrong: on an
MCP endpoint the JSON-RPC envelope is mirrored for the method name, the
bubble buffers an HTML page to write one script tag into it, the cookie check
reads the flags your app sets, and a fixed list of headers is read by name.
None of it is uploaded, and the kit behaves exactly as it did in the release
below.

1.0.0a149 — The notice this kit prints when it starts its reader server told
you to read DISCLAIMER.md and PRIVACY.md — two files the Python kit does not
ship under those names, so there was nothing to open. It now points at
https://www.boosthis.com/privacy, which carries the same policy and is always
the current one. Nothing about what the kit measures, sends or displays
changed.

1.0.0a148 — The guide and README now say plainly what this kit puts into the
app it is installed in, before you ship it rather than after. The floating
bubble is described as on by default, with the setting that hides it named in
the same breath, and no guide now calls it a mandatory setup step. Every
address the kit mounts on the host app is listed with who can reach it, what
it answers and, where it is open to the public, why it is open and what it
deliberately leaves out. Nothing about what the kit measures, sends or
displays changed.

1.0.0a147 — This README no longer tells you to fetch the kit from PyPI.
Boosthis kits are not published on PyPI, npm or any other public package
registry, and the README now says so where you meet the install step: a
registry copy would be an ungated copy of a kit that is meant to arrive
against a project key. Nothing of ours holds the name `boosthis` on PyPI, so
the old instruction pointed at whatever a stranger might publish there. In its
place is how the kit actually arrives — over your Boosthis connection, written
into your project, then made importable with a local path install that
contacts no registry. The Node sibling is named by the path it is written to
rather than by an npm package page that does not exist.

1.0.0a146 — One rule now decides what a part of the app is called — a route
here, a screen on the device kits — and every Boosthis kit takes
it from the same source instead of carrying its own copy of the
idea. The limit is 100 characters, the same bound the server
enforces, so this kit never sends a name the wire would quietly
reshape. A longer name is REFUSED rather than shortened: two
routes whose names differ only past that point used to be stored
as one row with a pooled timing, under a name neither developer
wrote. A name the rule refuses is counted, and where you wrote
it by hand the kit says so once through the warning channel it
already uses, with the reason and an acceptable form — never
echoing the rejected text back. The upload path no longer cuts
every label to 100 characters on the way out; the bound is
enforced where the name is minted.

1.0.0a145 — Adds request-path, route-failure, last-resort error, process-uptime
and timer-health readings across every mounted framework, with honest warm-up
floors, explicit runtime limits and outbound cache/connection observations.

1.0.0a144 — Adds processor consumption, thread footprint, proxy queue time,
worst observed freeze and cross-worker request imbalance. Cumulative CPU time
is anchored and reset-safe, thread growth excludes startup and abstains inside
its own noise, queue time requires a front-door arrival header, and one worker
is never reported as balanced. Thread-pool saturation remains absent because
Python's ThreadPoolExecutor publishes no busy-worker counter.

1.0.0a143 — A second app on the same machine can no longer silence this kit.
The kit kept the last access answer in one fixed file under your home
directory and ignored the state directory you gave it, so an answer left
behind by a neighbouring app — once its grace period had run out — silenced
every kit that started next on that machine. A silenced kit sent nothing at
all, and said so as though the Boosthis API could not be reached. The answer
now follows the state directory, and an install holding no credentials of its
own is never held silent by an answer it did not earn: registration is the
only way back, so it must always be allowed to register.

1.0.0a142 — The memory rows report the current level again, and a heap that
moves too much to call a trend now waits instead of refusing to grade. The
reading had stopped carrying the process's own heap and resident figures, so the
page could show a verdict with no number beside it, and a flat-but-noisy heap
was reported as unscorable rather than as still settling.

1.0.0a141 — The in-app page can now tell a meter that never started from one
that is still warming up. A meter whose collector never ran used to render in
the same grey 'pending' style as one that is a minute from a reading, so a
developer was told to wait for a number that was never coming; the page now has
its own 'never started' style for that state, and the 'not registered' notice
says how many meters never started instead of claiming the kit is measuring
locally.

1.0.0a140 — The in-app panel no longer promises a Resident Memory Growth row
that can warm forever off Linux. That reading comes from `/proc/self/statm`, so
it now appears only when the process can actually provide it. Python's own heap
and garbage-collector rows are unchanged.

1.0.0a139 — Memory and garbage-collector readings now use the shared names,
units and bands: peak and resident-memory trend, memory and allocation per
request, heap fragmentation, collection tax and pause tail, generation balance
and finalizer backlog. Trends now include the standard error they were judged
against and abstain when the movement sits inside it. When Python cannot take
one of these readings, the kit now declares why instead of showing a zero or
leaving it blank.

1.0.0a138 — The in-app panel now warms the Disk Pressure tile, so it shows
the same reading the dashboard promises for Python projects. The kit now
reports disk pressure, storage speed and storage failures for the volume
holding its state directory, scoped to this container when containerised and
otherwise to the whole machine. It also reports system load for the whole
machine. Its I/O pressure, page faults, file-descriptor mix and descriptor
saturation readings now state that they describe this process. If the
environment blocks disk or storage access, the dashboard shows that reason
rather than a zero.

1.0.0a137 — The install guide and this kit now agree about which frameworks
can be mounted, and the refusal tells you what to do next. The served guide
offered "Flask (or any WSGI)" while mount() has only ever matched six
frameworks by name, so an app on any other WSGI framework was promised
support in writing and refused at runtime. The supported set is now stated
once, in mount.py, and every sentence about it is built from that list. The
TypeError now names the object it was handed (the guide always claimed it
did; it did not), names track_perf / perf as the way to measure an app it
cannot mount, and says what that costs. It no longer offers `boosthis serve`
as a "fallback": that command runs a separate Boosthis-only server that
measures nothing of your app, and following it led back to the mount() call
that had just failed. `boosthis serve` and `boosthis init` now say what that
server does and does not do. Also fixed: mount()'s docstring sat below the
first statement of the function, so it was not a docstring at all and
`help(boosthis.mount)` showed nothing.

1.0.0a136 — The README that ships beside this kit now states the version you
are actually running. It had been left at 1.0.0a129 while six releases went
out under it, and the changelog inside it stopped there too, so the one file
that travels with the code described a kit nobody is running. Nothing this kit
measures, sends or shows has changed — the code is identical to 1.0.0a135. It
is a release rather than a quiet correction because a corrected file only
reaches an installed kit when the version moves; at an unchanged version every
consumer is told it is already current, and the fix never arrives.

1.0.0a135 — The kit's own pages now say where your scheduled jobs live. Until
now the in-app page at /_boosthis and the local status page at
/_boosthis/status listed meters and said nothing about jobs at all, which
reads — to a developer and to an AI reading the page — as a report that this
app has none. One assistant did exactly that: asked what the kit showed for
three scheduled jobs, it answered "absent" six times over twenty minutes while
one of those jobs had 61 recorded runs. Jobs stay off these pages, because
each is a local view of one process while a job's run history, rhythm and
lateness are assembled across every install of your project. Both pages now
carry a Scheduled jobs line that is always shown — whether or not this app has
any — saying that this kit reports named job runs and that they are listed on
your dashboard, so nothing missing here is evidence there is nothing there.

1.0.0a134 — A background job with a long name is measured again. The kit
published a job-name limit of 80 characters, and `track_job` — the call the
guides tell you to mark a job with — quietly discarded any name over 60. A
name between those two numbers is INSIDE the published limit, so a developer
who checked before naming a job was told the wrong number and got nothing for
it: no run, no counter, no line in the log, nothing on any page. The job ran
normally in the app and Boosthis never heard of it. There is one limit now,
80, and it is the same number on every door: the constant the kit exports,
`track_job`, `begin_job`, `expect_every` and the server that receives the run.
A name past 80 is still refused rather than shortened, because two names cut
to the same 80 characters would be filed as one job, and the refusal logs one
line naming the length and the rule (never the name itself). If you shortened
a job's name to make it appear, the original works now and will show up as a
second job.

1.0.0a133 — A background job whose name has a space in it is now measured. It
was not: job names were screened by the rule written for route labels, which
refuses every space because a space in a code-defined route label means a
value got pasted into it. A job name is not that — it is a label you write by
hand beside a schedule, so 

1.0.0a131 — The install guide now says what the AI-call meter reads out of an
outbound prompt, in the same words our public pages use: to tell repeated
prompts apart it reads up to the first 4,096 characters of the request body
inside your own process and folds it into a single number, and no body text is
stored, uploaded or recoverable. It was true before this release and it was
stated only where we sell the product, which left the person deciding whether
to ship it — you — reading it last. Nothing measured changed: no reading was
added, removed or reworded.

1.0.0a130 — Three new database readings, alongside the Database Work reading
this kit already took. Database Sequencing shows queries your app issued one
after another when they could have run at the same time. Database Rows shows
how much came back — the largest result and the typical one — so a query that
is fast but hands back fifty thousand rows stops looking healthy. DB Pool
Pressure reads your connection pool's own state: how much of it is in use, and
how many callers are queued where the pool will say. asyncpg, psycopg_pool and
SQLAlchemy pools are read; only psycopg_pool reports queued callers, and for
the other two that figure is left out rather than shown as zero. Every one of
these readings, and Database Work with them, now also reports how many
database libraries in your app it could NOT watch, and names the ones it can —
psycopg and asyncpg — so an empty reading tells you whether your driver is
unsupported instead of looking like an app that never touches a database.

1.0.0a129 — `expect_every()` can no longer do nothing in silence. Five ways
the call could be dropped inside the kit — Boosthis switched off, a job name
the label guard will not take, a name longer than 60 characters, an interval
that is not a number above zero, and a job past the point where this kit folds
extra names together — each now log one line on the `boosthis` logger naming
the cause, the fix, and the fact that the job is NOT being watched. Nothing
raises into your app: a meter that breaks its host is worse than a meter that
is quiet. The unit is stated too. Boosthis stores a rhythm in whole minutes,
from 1 minute to 45 days, so `expect_every("heartbeat", 30_000)` is watched at
one minute — the job IS watched, and the kit says so at the call rather than
letting your number change on the way to us. Declaring a rhythm for a job past
the name cap used to attach it to the everything-else group, watching a bundle
of unrelated jobs under your job's interval; that is refused out loud now.

1.0.0a123 — An app that calls a model this kit does not recognise — a model
you host, a private gateway, a regional endpoint — no longer looks like an app
with no AI in it. A POST whose path is an inference shape is counted, and the
AI Wait reading appears saying it cannot be measured, carrying that count and
nothing else: no address, no path, never timed, scored or priced. Naming the
endpoint in `BOOSTHIS_AI_ENDPOINTS` (or `set_declared_ai_endpoints`) still
turns those calls into a full reading. This build also carries your project's
promises into the kit's own page: your own wording, and whether Boosthis is
checking each one. Nothing is shown there if they cannot be read.

1.0.0a122 — Python gains Data Distance, measured automatically for new stdlib
HTTP/HTTPS connections or explicitly with `record_dependency_connection` when a
client can expose honest setup timing, and Live Connections, measured
automatically for ASGI WebSockets and SSE or explicitly with
`begin_live_connection` for other hosts.

1.0.0a121 — Your framework is now asked for its whole route list when a report
is taken, so the app map can show routes traffic has never reached — marked
“not seen”, never “dead” or “unused”. FastAPI, Starlette, Flask and Django
answer; an unfamiliar shape reports nothing rather than a guess, and no source
file is ever read. Route templates only. Set `BOOSTHIS_ROUTE_LIST=0` to switch
it off.
1.0.0a120 — A model you host yourself can now be measured. The AI reading
recognises a call by its destination, against a list of the public providers,
so an app calling its own model server, a private gateway or a regional
endpoint was not reported as zero and not reported as unknown: it was not
reported at all, and the three AI rows were simply absent — which looks
exactly like an app with no AI in it. Name your endpoints with
`enable_telemetry(ai_endpoints=["llm.internal"])`, or in BOOSTHIS_AI_ENDPOINTS,
and those calls are timed and counted like any other. The address is compared
inside your process and never sent: every declared endpoint, in every install,
reports the same fixed code, so a reading can neither name your endpoint nor
tell two of yours apart. Tokens are counted and the money is deliberately left
blank — a model you run has no list price, and a private gateway's price is
your contract, not a published page — so the kit says how many calls it
declined to price rather than quietly pricing them from a model name it
happens to recognise. A cost your own endpoint reports IS used.

1.0.0a119 — Wording only; nothing the kit measures, sends or decides
changed. A comment in the kit's own source named a number of kits that was
two releases out of date. It now says “every kit”, which cannot go stale.

1.0.0a118 — Boosthis's own uploads are no longer counted as your app's outbound
traffic. If you report outbound calls to us from the same HTTP client your app
uses to reach Boosthis, our registrations and report uploads were landing in
your outbound-reliability reading. Only the calls your app made are counted
now, so a slow Boosthis can no longer move that row.

1.0.0a117 — The panel inside your app now uses the same words for a scoreless
row as your dashboard does. A row that is deliberately never graded reads “no
score by design”, a reading this host cannot take reads “not available here”,
and only a row that really is still gathering reads “pending”. Nothing
measured changed.

1.0.0a116 — Three different silences used to share one word. A rating of
“pending” promises a score is coming, and refusal honesty was saying it too —
an axis that reports its counts and deliberately never grades them, because a
refusal can be right or wrong and header presence cannot tell which. It now
reads “no score by design”, and a reading this host cannot take at all reads
“not available here”. Nothing measured changed.

1.0.0a115 — A call your app makes to another of your own services now carries the
trace with it, so one user action crossing two services shows up as one trace
instead of two. The kit was already reading the trace headers on the way in, and
already standing inside your outgoing HTTP calls to measure them — it just never
wrote the headers on the way out, so every service started its own trace and
nothing that spans a service boundary could ever be seen. This covers requests,
urllib and anything else on http.client, plus httpx (sync and async) and aiohttp.
Where the headers are attached is yours to decide, and the default is
deliberately narrow: only destinations that cannot be on the public internet
(loopback, private ranges, single-label hostnames that are not themselves
public top-level domains, and reserved suffixes like
.internal and .local), so no outside company — no payment provider, no model
vendor — ever receives an identifier we generated.
BOOSTHIS_TRACE_PROPAGATION widens that: a comma-separated list names your own
destinations, and none switches it off. Your kit page shows which
destinations it is active for. Attaching can never alter, delay or fail one of
your calls: a header you already set is left alone, and any failure while
attaching leaves the request exactly as it was.

1.0.0a114 — A fast route can no longer be reported as steady. Requests are
timed in whole milliseconds, so a handler that finishes in under half a
millisecond records as 0ms, and a route whose earlier window is all zeros
leaves the baseline axis nothing to divide by. Those routes were counted as
examined and then dropped in silence, so an app whose sub-millisecond route
suddenly took 100ms still scored 100 and read “steady”. A route that cannot be
divided by is now reported as too fast to compare, the steady count names only
the routes actually compared, and an app where every route was too fast says
it cannot judge rather than scoring full marks. Routes slow enough to compare
are judged exactly as before.

1.0.0a113 — Deliberately held-open streaming and upgraded connections remain
in flight but no longer distort resilience, latency-floor or load-deflection
durations; exclusions stay visible. gcGenerationBalance.ratio now prints the
precision it measured instead of collapsing every install to 0.01.

1.0.0a112 — CPU throttling now reports the window it measured, not the container's
whole life. The kit reads the cgroup CFS bandwidth counters, and both of
those are lifetime totals for the container — dividing one by the other
gave a since-start average, so a container being throttled hard right now
still read a few percent, and every process in the container reported the
same figure. The kit now differences the counters between its own reads,
reports the share over that window with the window's length beside it, and
says nothing at all until it has a window to report — no invented zero on
the first read.

1.0.0a111 — The latency floor is no longer judged against a bar built for web pages.
That axis takes the worst ten-second window's p95 request latency, so one bad
window is never averaged away — but it was then scored on the shared
page-interaction band, which calls 500ms good. A server whose worst window ran
at a p95 of 401ms therefore scored full marks, and four tenths of a second at
the 95th percentile is not a smooth server. The axis now carries its own
server-request band — good at 200ms, poor at 1000ms — declared beside the
axis with the reason written next to it rather than borrowed from a different
kind of measurement. What the meter measures is unchanged; this changes only
what the score says about it.

1.0.0a110 — The kit can now say when it cannot keep its install identity.
The three cause words that notice reports were used but never defined, so
building the message raised NameError inside a catch-all and the warning was
swallowed entirely: a service that mints a fresh id on every restart was
never told, and quietly lost its history. The causes are declared and the
notice is said once at startup, in the wording the other kits already use.
1.0.0a109 — The kit's own addresses now answer for themselves when
`boosthis.mount(app)` was never called. Adding only the middleware still
measures, but the pages it prints lived behind that second call, so visiting one
returned your app's 404 — the same answer as having no kit installed, which
reads as a broken install or a rejected key. Those reads now come back from the
kit: it says it is installed and measuring, names the call that registers the
pages, and lists what works without it. The badge reads and the loopback status
page are among them — the middleware now serves those three itself.

1.0.0a108 — Setting BOOSTHIS_INSTALL_ID now decides which install your service
is, including on the path that actually registers it. The app-scope half used
the variable while telemetry startup went from the saved file to a new id. A
developer who pinned the id saw it honoured in one half and ignored in the
half that matters. One resolver now gives code its precedence, then the
environment, then persisted state, minting only when nothing answered.

1.0.0a107 — A rhythm your own code declares now reaches Boosthis. expect_every()
used to validate what you passed it, keep the value in this process, and stop —
so the side that raises alerts never learned it, and answered "nobody said how
often this should run" about the very job the kit was counting missed runs for.
The declaration now travels with the job runs, survives a restart because
Boosthis holds it, and a job that stops is reported late against the rhythm
Boosthis kept for it — in whole minutes, so a rhythm finer than a minute is
watched at one minute (from 1.0.0a129 the kit says so at the call). If a
declaration cannot be delivered the kit says so on stderr
instead of accepting it in silence, and it no longer counts missed runs against
a rhythm Boosthis never confirmed. A declared rhythm also keeps up with the
dashboard: the kit re-states it about every five minutes on the upload it
already makes and adopts whatever comes back, so correcting the interval or
switching the watch off from your project page reaches a process that is
already running. A re-statement that fails no longer reports a watched job as
unwatched, and when a declaration cannot get through the kit names the real
reason instead of pointing at your network — a project that is locked, unpaid
or not yet registered drops the upload inside the kit, and it says so. That
re-statement no longer depends on your app still reporting job runs — the job
worth watching is the one that has stopped, whose process reports nothing at
all — so declaring a rhythm starts a small clock of its own that asks again on
time whether or not anything is running, retries a declaration that never got
through, and ends itself when the last declaration is forgotten. An app that
declares no rhythm gets no clock.

1.0.0a106 — Restarting your app no longer starts your history again. The kit
keeps its install id in a file so a restart comes back as the same install, and
on hosts where that file could not be written it was silently minting a fresh id
every launch: one project registered eleven times in four days, and because
daily history is only ever written for completed days, no page could hold more
than minutes. It now tries the directory you name in BOOSTHIS_STATE_DIR, then
your home directory, then scratch space, proving each one by actually writing
there and by checking it can be made owner-only — the file holds credentials, so
a directory that cannot be locked down is refused rather than used. Where no
directory works at all, the kit says so on consent instead of going quiet, and
your project page states the condition rather than showing a stream of one-shot
installs. Scratch space is scoped per service, so two apps on one machine can
never pick up each other's install id.

1.0.0a105 — A call your app retried is now reported as every attempt it took. A
failed attempt was counted but never timed, so five failures then a success read
as one measurement — the success alone, with nothing to show that getting there
cost six tries. Every attempt that ended now banks its duration, so the network
reading shows what a flaky dependency really costs; the failure count sits
beside the latency. No wire change.

1.0.0a104 — Two processes of one app no longer overwrite each other's
state. The install config and any buffered crash are now published atomically
— written beside the target and renamed into place — so a gunicorn or uWSGI
worker can never read a half-written file, which the kit would have treated as
an install with no credentials at all.

1.0.0a103 — Cold start now measures how long your app took to be ready to
serve, instead of how long until you switched Boosthis on. The kit watches for
the first socket that starts listening — the one call socketserver, gunicorn,
uvicorn and asyncio all open a port through — so start-up work done after the
install line (a pool connect, a migration, a cache warm) is inside the figure,
and moving the install line no longer moves the number. Where that moment
genuinely cannot be seen — a worker that never binds, a pre-forked child that
inherited an already-listening socket — the reading says which interval it did
measure and carries no score and no rating, so a slow start can never be
called good.

1.0.0a102 — A back end that exits soon after starting now uploads what it
measured. Until now a reading waited for the next one to arrive fifteen seconds
later, so a worker, a scheduled script or a container being recycled registered
and then reported nothing at all, however much it measured — the project page
said "never measured" and nothing explained why. The kit now sends on the way
out: on the exiting thread, within two seconds, never on a background thread the
interpreter discards. It takes SIGTERM only when your app has not handled it
itself, and re-raises so your exit status is unchanged.

1.0.0a101 — A blind spot found after start-up now reaches your project page even
on an install that uploads nothing. Almost none of what the kit can and cannot
watch exists when it registers — a job system is only met when the first task is
enqueued, a framework reading is only refused once a request has gone through it
— and the re-report used to ride an upload the default install never makes. It
now rides ordinary measured work, so the page stops saying "not reporting" for
an app that has been running all day.

1.0.0a100 — The fix your project page names for an unwatched outbound call now
actually imports. `boosthis.record_outbound_attempt` is reachable from the
package: call it around a request made with a client the kit does not watch and
the destination is counted (host only — never the path or the body). Until now
that call did not resolve, so the way out we printed was one nobody could take.

1.0.0a99 — Your project page can now list the parts of your app we are NOT
watching, each with the change that fixes it. The kit reports which surfaces it
reached and which it found but could not attach to — a Dramatiq, Huey, arq,
django_q or schedule worker it has no adapter for, or database work it cannot
yet see. Only surfaces really present in your process are named, and only fixed
words and counts are sent: no module, task or function name of yours travels.

1.0.0a98 — A trace that crosses into this runtime now nests the whole way
through. Each measured leg of an action records which leg called it, and
passes its own identity on to the next service it calls, so a full-stack
action draws as a waterfall instead of going flat at this hop. The
identifiers are random, carry no request data, and nothing new is uploaded.

1.0.0a97 — Your app's database work is now measured, the way the JavaScript kit
already measures it. Until now a Python app could not be told the commonest
thing a developer wants to know about a slow request: was it the database? Each
request now reports how much of its time went waiting on the database, how many
round trips it made, whether it ran the same statement over and over within the
one request and what that repetition cost, and whether a single query handed
back an unbounded result set. A query is credited to the request that ISSUED it,
even when the connection came out of a pool opened during an earlier one, so a
busy app's figures are its own rather than whichever request happened to be
standing there when the answer arrived. The kit watches only a driver your app
has already loaded — psycopg and asyncpg — and never imports, resolves or
installs one; a driver it cannot watch is reported as a counted blind spot and
never as a zero, so "no database work" and "we cannot see your database" can
never look the same. An app with no database shows nothing here at all. Because
the missing half now exists, the share of a request spent waiting on outside
services finally includes database time, and a hosted database reached over the
web is counted once — by this reading — instead of twice. Nothing about a query
travels: no statement text, no values, no table or column names. Counts,
durations, shares and an in-process fingerprint only. Adds the Database Work
meter, which is absent rather than empty where there is nothing to say.

1.0.0a96 — One new rule and nothing else: a view whose query count rises
with the size of the account. Touching a related attribute inside the loop
that builds the response, or recomputing a whole-history count on every
request, never shows against a three-row fixture. The rule says to measure
the view at two account sizes, flatten the per-row read with
select_related, prefetch_related or an annotation, bound or maintain the
aggregate, and hold the number with assertNumQueries. Rule book only — no
new measurement, no new upload.

1.0.0a95 — FastAPI, Starlette and Flask apps now record a timing for every
request they serve. Until now only the Django, Gradio and Streamlit adapters
did, so an app mounted on those three registered, checked in and reported
nothing at all — its project page stayed on "no measurements yet" however much
traffic it served. Each request is named after its route template
(`/orders/{id}`, `/widgets/<id>`), never the URL, and the kit's own pages are
not counted as your app's work.

1.0.0a94 — Requests this kit already timed now reach your project page. They
upload in small batches (100 at most) under the same sharing permission,
privacy guard and credential as the snapshot it already sent, and are dropped
rather than queued for ever when the server cannot be reached. Nothing new is
measured — a project that could only ever say "registered, no measurements
yet" now fills in its own timings.

1.0.0a93 — Housekeeping only: a rule-count line in this kit's own text now
names the right number of rules in the shared checklist it points at. The
Python pack already had all six AI-provider checks; this is rule-book only —
nothing new is measured and nothing new is sent.

1.0.0a92 — A deliberately cut project credential now closes the kit
immediately and preserves the server's reason and guidance. An ordinary 401 or
an unreachable server still leaves the last cached entitlement untouched, so a
recoverable rotated credential is never mistaken for a revocation.

1.0.0a91 — Housekeeping only, with no change to what this kit measures or
reports: a rule-count line in this kit's own text now names the right number
of rules in the shared checklist it points at.

1.0.0a90 — housekeeping only, with no change to what this kit measures or
reports: the shared list of problem names this kit is checked against now also
names the three cache-and-repeat-waste problems raised by server kits, so the
list stays in step with what Boosthis accepts. No code change.

1.0.0a89 — names the outside services your app leans on, one by one, instead of
one blurred figure. Sign-in, file and image storage, payments, messaging and
stored-knowledge search are each recognised by kind from a fixed list — never by
address — and reported with how often they are called, how long they take and
how often they fail. Sign-in is called out on its own, because it sits in front
of every session. Where sign-in runs inside the app — Django's own accounts,
Flask-Login, Authlib and the rest — the project is told exactly that, so it is
never left to conclude it has no sign-in when it plainly has one. Anything
unrecognised stays a visible group of its own rather than being folded into a
kind or guessed at. A dependency that failed is counted apart from one that
never answered, and a service failing some calls while serving others shows as
intermittent. The share of a typical request spent waiting on outside services
now sits beside the app's own time; at that release this kit had no
database-work reader, so that share was left out rather than shown as nothing
(the database half arrived in 1.0.0a97). An app that calls no
outside service shows nothing here rather than a row of zeros. Four new rules
name the fixes: an outside call with no time limit, one that retries without
backing off, calls made one after another that could have run together, and a
dependency on the path a visitor waits for that need not be. Only the kind word
leaves the app: no address, path, header or payload can travel, proved by tests
on both sides.

1.0.0a88 — A reading your framework cannot take is now NAMED, with the reason.
This kit mounts into six Python frameworks and they genuinely differ in what
they can report: a Flask or WSGI Django request has no asyncio loop to time, a
Streamlit rerun never sees an HTTP response to read a status code or a cookie
from, and a Gradio demo that serves itself has no worker processes to be
recycled. Those readings used to sit blank, which looks exactly like a reading
that is broken or still warming up. Each one is now listed with the framework
fact behind it, on this kit's own panel and on your dashboard, and your project
page stops promising a number that could never arrive. The kit also reports
which framework it is mounted in, so your project is named as Flask, Streamlit
or Gradio rather than only as Python. Nothing about how the measurements
themselves are taken has changed.

1.0.0a87 — this kit now reports whether a fix Boosthis suggested actually
helped — the same before-and-after reading the JavaScript kits send — so advice
that does not work can be found and withdrawn instead of being repeated. The
recurring problems it reports also carry a name drawn from one shared list used
by every Boosthis language, so the same problem found in Python and in another
language is grouped as one problem instead of two. What leaves the process is
unchanged: a hashed signature, a bucketed severity and a bucketed count, behind
the same consent — never a route name, a value or any code.

1.0.0a86 — Your project now says WHERE IT RUNS. Alongside the language and kit
version an install already reported, it now names the hosting platform it is
running on, that platform's region where the platform declares one, and whether
this is production, a preview, or a development run. Previews are marked
wherever the project's numbers appear, so a throwaway preview's slow figures
can never be mistaken for the real app's. The kit's own status page shows the
same thing, including when it cannot reach Boosthis. It is recognition, never
guesswork: only what the platform publishes about itself is read, only short
words from fixed lists are sent (no addresses, no configuration, nothing
free-form), a platform we do not know is shown as unrecognised rather than
guessed from a similar one, and a platform that declares nothing says so instead
of leaving a blank. It refreshes when a project moves.

1.0.0a85 — the rule count in this README now matches the rule book it points
at: four new checks landed for long-lived connections, taking the shared set
from 86 to 90. Nothing in this kit's behaviour or measurements changed.


1.0.0a84 — The work that happens with nobody watching is now measured. Task
queues, scheduled jobs and after-response work used to leave a dashboard
looking empty, because everything this kit measured began with a request from a
browser. A worker process with no web server at all is now a project in its own
right: it registers, measures the work it does, shows its own status, and
appears on your dashboard with real numbers. Every run is timed and grouped by
job name — capped, with a visible everything-else group — with failures
separated from slow successes, the time a job spent waiting before it started
wherever the queue will say, how many attempts it took, jobs that overlap
themselves, and a recurring run that should have happened and did not shown as
missing rather than as silence. A job that crashes is reported as clearly as a
crash inside a request. The common task queues and schedulers are attached to on
their own where the app has loaded them itself; where they cannot be, the kit
says so and one line marks a job by hand. Four new rules name the fixes: a job
with no time limit, one that retries forever, one that overlaps itself, and a
queue falling behind.

1.0.0a83 — says what the AI part of an app costs and how long it takes to
answer, matching the Node kit. Calls to a known AI provider are recognised by
name from a fixed list — never by address — and reported apart from the database
and from ordinary outbound calls: the wait before anything arrives, the time to
the first words of a streamed answer, whether the stream stalled or was cut off,
and the total. Reads the provider's own usage numbers where it sends them:
tokens in and out, cached tokens, its own processing time, and the cost per
request and per month. Where a streamed answer carries no usage because the app
never asked for it, the project is told exactly that with the fix instead of
shown a zero. Rate-limit headroom is collected and shown like every other
ceiling, and rate limits, quota refusals, timeouts, cut-off answers and
content-filter refusals are each counted apart. No prompt and no answer can ever
leave, and a project that calls no AI provider shows nothing here at all. Adds
the AI Wait, AI Spend and AI Headroom meters, which never affect the Speed
score.

1.0.0a82 — an app can now report scheduled background work. `track_job` times a
job and records whether it succeeded; `report_job_run` does the same for work
that keeps its own timings. Only the job's name, that it ran, whether it
succeeded and how long it took is sent. With an expected rhythm declared on the
project page, a job that stops running is reported as a missed run.

1.0.0a81 — Django, Streamlit and Gradio apps now install Boosthis the same
one-step way as everything else. Django needed you to decorate your own views
by hand, and Streamlit and Gradio had no way in at all. One mount call now
installs both halves at once on all three — the kit's own pages and the timing
of that app's own unit of work: a request on Django, a script rerun on
Streamlit, an event-handler call on Gradio. The two halves can no longer come
apart on any Python framework: if either cannot go in, neither is installed and
the kit says so on stderr instead of showing a panel with nothing behind it.
Readings that cannot exist on those hosts — an event loop under WSGI, a
response header inside a Streamlit rerun — are now named as not measurable with
the reason, rather than sitting on "warming up" forever.

1.0.0a80 — Python now says one startup line when this host cannot provide one
or more operating-system or interpreter readings. The line names the missing
source and affected meters, is said once even when the app mounts Boosthis more
than once, and makes clear that every other part of Boosthis is unaffected.

1.0.0a79 — httpx and aiohttp calls now reach the retry-storm check, and a streamed
reply is timed until its body finishes. Since 1.0.0a77 both clients fed the
outbound-call and repeated-work tiles, but a burst of retries through them was
still invisible, and a call whose body arrives slowly was filed the moment the
headers came back — so a slow or failing download read as an instant success.
Streamed calls are now filed when the body completes, a body that fails is
recorded as a failure, and a response that is never finished is not left
uncounted.

1.0.0a78 — a refused upload batch is now recorded and shown. Non-2xx replies
and requests that never answer are classified (credentials rejected, batch
rejected, Boosthis failed to store it, no answer), the status page headline
says uploads are not getting through, and the page and panel show the last
failure and how many uploads have been lost.

1.0.0a77 — outbound calls made with httpx or aiohttp now count too. Both open
their own sockets and bypass http.client, so a modern async app using them saw
an empty outbound-call tile and an empty repeated-work tile — the same picture
as an app that makes no calls. A call that follows redirects is counted as the
several round trips it really is, not as one slow call. Same lever, same rules:
durations and coarse outcomes only, Boosthis's own calls never counted. And an
app whose HTTP client cannot be watched now says so on both tiles instead of
reading as an app with no calls.

1.0.0a76 — The Boosthis button now appears before the first successful
  check-in. Until now it stayed hidden until Boosthis had confirmed the
  install, so an app that could not reach Boosthis at all — a preview with no
  outbound network, a rejected or rotated project key, a reinstall — showed
  nothing and looked broken. The button now draws straight away and opens on
  the kit's existing “Not registered yet” notice, with the reason when one is
  known. Everything that hid the button before still hides it: the kill
  switch, the bubble-off directive, the in-code option set false, a tampered
  install and a lapsed offline grace window. Nothing is measured or uploaded
  before the install is confirmed — that is unchanged.

1.0.0a75 — fixes three faults in the outbound-call meter. A reply from a service
that closes the connection after answering (HTTP/1.0, or "Connection: close")
was counted as a failed call, so an app talking to such a service saw a tile made
of failures it never had. Switching the watcher off restored the standard HTTP
methods even when another library had wrapped them afterwards, deleting that
library's work. And two requests arriving together could install the watcher
twice. Also adds four checklist rules: connect to the address actually validated,
leave the host's logging and exception hooks alone, never re-enter a
non-reentrant lock, and cancel an abandoned attempt before retrying it.


1.0.0a74 — the outbound-call meter now reports: how long this app's calls to
other services take, and how often they time out, fail, or end with no answer
at all. Watched through the standard http.client clients (urllib, requests,
urllib3); counts and durations only, and calls to Boosthis are never counted.

1.0.0a73 — adds the repeatedWork meter: the same call made more than once
inside a single request, identified inside this app by hashing the target and
its arguments. Query text, URLs and argument values never leave the process.

1.0.0a72 — the install guide now names every project-key setting this kit
reads and the order they win in. Nothing measured or uploaded changed.

1.0.0a71 — inactive installs now distinguish missing, deliberately omitted,
  revoked, billing-paused, unknown, and otherwise refused project keys; every
  reason reads the same on the console, in the panel and on the status page.

1.0.0a70 — adds the refusalHonesty exposure meter, using presence-only
Authorization and WWW-Authenticate checks at the existing response boundary.

1.0.0a69 — the status page now asks Boosthis and plainly says registered, not
registered, or unable to check; it always shows the install id and only says
nothing is sent when that is known.
1.0.0a68 — startup always names the project-key tail and badge visibility before
registration; malformed, quoted, truncated, or Bearer-prefixed keys are refused.
1.0.0a67 — release notes housekeeping: older entries no longer call the
floating panel development-only; it is shown in every build unless turned off.
1.0.0a66 — floating bubble visibility now follows the shared kill switch,
directive, legacy-alias, code-option, visible-default precedence contract.
-->

The Python sibling of the Node kit, which arrives the same way and is written to
`lib/boosthis-runtime-node` in the project it measures. Same scoring model, same
PII guard, same 102 universal checklist rules — plus 92 Python-specific ones —
exposed as a Python-native decorator + context manager.

## How this kit reaches you

**Boosthis kits are not published on PyPI, npm, or any other public package
registry. That is a decision, not an oversight.** A registry copy would be an
ungated copy of a kit that is meant to arrive against a project key. So there is
nothing of ours on PyPI to find: a search there for `boosthis` returns nothing
we published, and anything that does answer to that name is not ours and should
not be installed.

The kit arrives over your Boosthis connection instead. Your AI assistant asks
Boosthis for it with your project key, and Boosthis hands back every file — this
README included — to write into your project under `lib/boosthis-py`. If you are
reading this file inside your own repository, that step has already happened.

Make the package importable from the folder that was just written (a virtualenv
is recommended):

```bash
pip install -e ./lib/boosthis-py
```

That is a local path install: no download, no registry. It exposes the
`boosthis` import and the `boosthis` CLI.

To connect an assistant that has not done this yet, or to mint a project key,
see <https://www.boosthis.com/docs/connect-your-ai>.

## 30-second setup

```python
from boosthis import track_perf, perf

# 1. Wrap any function (sync OR async)
@track_perf("checkout.process_order")
def process_order(order_id: int):
    ...

# 2. Or instrument an inline block
with perf("db.heavy_query"):
    rows = session.execute(query).all()

# 3. Web apps: mount Boosthis so the floating bubble and its
#    closed pulse endpoint are wired into outgoing HTML pages.
from boosthis import mount
mount(app)
```

The floating bubble is on by default in every environment, including
production. `BOOSTHIS_BUBBLE` set to 0 hides it and the kit carries on
measuring; `BOOSTHIS_DISABLED` stops the kit altogether. Each name is read
from the environment where this runtime has one, and otherwise from a value
of the same name on the host.

An account/settings link in the bubble is an extra, not a replacement for it.
The full first-match visibility precedence:

1. `BOOSTHIS_DISABLED` truthy (`1`, `true`, `yes`, `on`) hides absolutely.
2. `BOOSTHIS_BUBBLE` is tri-state: those truthy values show; `0`, `false`, `no`,
   or `off` hide; empty or unrecognised values fall through.
3. Truthy `BOOSTHIS_NO_BUBBLE` hides, then truthy
   `BOOSTHIS_FORCE_BUBBLE` shows (legacy aliases).
4. The explicit `mount(app, bubble=...)` option, when supplied.
5. Otherwise the bubble is visible everywhere, including production.

Each measurement prints a one-line report to the `boosthis` logger:

```
[boosthis] checkout.process_order · 412ms · good
[boosthis] db.heavy_query · 1620ms · poor
```

…and the rating uses the **same `SCORE_THRESHOLDS` and `RATING_CUTOFFS`** as the React Native runtime, so a "good" Python route and a "good" RN screen mean the same thing.

## Mounting into an app

`boosthis.mount(...)` installs **both halves in one call**: the kit's own pages
and badge, and the timing of that framework's own unit of work. They can never
come apart — if either half cannot go in, neither is left installed and the kit
says so once on stderr, naming the missing half. A project can never end up
with a panel and no measurements.

Six surfaces are supported. None of them asks you to decorate your own views or
handlers.

| Framework | Call | Unit of work |
|---|---|---|
| FastAPI / Starlette | `boosthis.mount(app)` | a request |
| Flask | `boosthis.mount(app)` | a request |
| Django | `boosthis.mount(application)` in `wsgi.py` / `asgi.py` | a request |
| Streamlit | `boosthis.mount(st)` inside `@st.cache_resource` | a script rerun |
| Gradio | `boosthis.mount(demo)` before `launch()` | one event-handler call |

Those six by name, and nothing else: a plain WSGI or ASGI callable is **not**
detected and cannot be mounted. Anything else raises `TypeError` naming what
it was handed, rather than attaching to nothing and looking healthy — and the
error names `track_perf` / `perf` as the way to measure that app, because they
need no adapter. Notebooks (Jupyter, Colab, Marimo) are **not supported**:
there is no unit of work to time. `track_perf` and `perf` still work there,
cell by cell.

`boosthis serve` is not an alternative route to any of this. It starts a
separate Boosthis-only server that shows what has already been recorded on
the machine; it does not host your app and measures none of its requests.

### FastAPI / Starlette / Flask

```python
from fastapi import FastAPI
import boosthis

app = FastAPI()
boosthis.mount(app, bubble=True)
```

Every route is timed under its own pattern. To time something *inside* a route
as well, add `track_perf` / `perf` around it:

```python
@app.get("/orders/{order_id}")
@track_perf("orders.get")  # decorator goes BELOW the route, ABOVE the impl
async def get_order(order_id: int):
    return await load_order(order_id)
```

### Django

Mount the handler in `wsgi.py` (or `asgi.py`), right after it is created — the
one place every request passes through:

```python
# myproject/wsgi.py
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")
application = get_wsgi_application()

import boosthis
application = boosthis.mount(application, bubble=True)
```

Requests are filed under Django's own URL patterns (`/`,
`/api/items/<int:item_id>/`). Nothing in `views.py`, `urls.py` or
`settings.py` changes, and no view is decorated. ASGI is the same call on the
handler from `get_asgi_application()`.

Under WSGI there is no asyncio event loop in the request path, so event-loop
lag, task backlog and blocking-calls-in-async are reported as **not
measurable** with that reason attached, instead of sitting on "warming up"
forever. Under ASGI they are measured normally.

### Streamlit

A Streamlit script re-runs top to bottom on every interaction, so the mount
goes inside `@st.cache_resource` — which Streamlit runs once per process:

```python
import streamlit as st

@st.cache_resource
def _boosthis():
    import boosthis
    boosthis.mount(st)
    return True

_boosthis()   # near the top, before the rest of the script
```

Each rerun — the thing a Streamlit user actually waits for — is timed and filed
as `streamlit.rerun`.

Two honest limits. The page belongs to Streamlit's own front end, so there is
no host HTML to inject a floating bubble into; sign-in and telemetry live on
the kit's own pages instead. And Streamlit executes the script only when a
browser session asks it to, so the kit mounts on the first rerun rather than at
server start. Readings that need an HTTP response of your own — cookie
exposure, refusal honesty, access pressure — are reported as **not measurable**
with the reason.

### Gradio

Mount the `Blocks`/`Interface` object **before** `launch()`:

```python
import gradio as gr
import boosthis

with gr.Blocks() as demo:
    ...

boosthis.mount(demo, bubble=True)
demo.launch()
```

Each event-handler invocation — the click or submit a user waits on — is timed
under the handler's own name (`gradio.slow_echo`). Gradio runs on FastAPI, so
every reading the FastAPI adapter takes is taken here too.

## Status page (for services with no UI)

When your app is a pure JSON API there is nowhere for the floating bubble to
appear. Once you have mounted Boosthis (`boosthis.mount(app)`), open the
**status page** in a browser **on the machine running the app**:

```
http://localhost:<your port>/_boosthis/status
```

It is a plain, self-contained page that says whether the kit registered,
whether anything is being uploaded, and — when nothing is — why not. It is
**local-only**: a request from any other machine gets a 403 and is told nothing
about the install, and the page never shows your install token.

## Extra hot paths inside a view

Mounting already times every request. Use `track_perf` when you want a
particular piece of work timed *as well*:

```python
from django.http import JsonResponse
from boosthis import track_perf

@track_perf("api.user_profile")
def user_profile(request, user_id):
    return JsonResponse(load_profile(user_id))
```

## Celery tasks

```python
from celery import shared_task
from boosthis import perf

@shared_task
def send_welcome_email(user_id):
    with perf("celery.send_welcome_email"):
        ...
```

## PII guard (`assert_no_pii`)

Same denylist as the JS runtime — won't let you accidentally ship `email`, `ip`, `password`, `token`, etc. to an external service:

```python
from boosthis import assert_no_pii, safe_transmit

# Throws PIIDetectedError if any key matches the denylist
assert_no_pii({"screen": "checkout", "ms": 412})

# Or use the all-in-one transmit wrapper
safe_transmit("https://ingest.example.com/perf", {"screen": "checkout", "ms": 412})
```

## Score formula

Identical to the JS runtime:

```
score = TTFF×0.25 + TTI×0.45 + FID×0.30
good ≥ 85 · needs-work ≥ 60 · poor < 60
```

For Python the mapping is:
- **TTFF** → request received → first byte
- **TTI** → request received → response fully written
- **FID** → first downstream blocking call (DB, http, etc.); defaults to 0 if not measured

## Cross-runtime parity

| | `@boosthis/runtime` (npm) | `boosthis` (pip) |
|---|---|---|
| Score thresholds | `SCORE_THRESHOLDS` | `SCORE_THRESHOLDS` |
| Score weights | `SCORE_WEIGHTS` | `SCORE_WEIGHTS` |
| Rating cutoffs | `RATING_CUTOFFS` | `RATING_CUTOFFS` |
| PII guard | `assertNoPII()` | `assert_no_pii()` |
| Transmit chokepoint | `safeTransmit()` | `safe_transmit()` |
| Tracker | `usePerfTracker(name)` | `@track_perf(name)` / `with perf(name)` |
| Checklist | `BOOSTHIS_CHECKLIST` | `BOOSTHIS_CHECKLIST` |

Both packages stay in lock-step on version bumps.
