
# Boosthis Web runtime

Current runtime version `1.0.0-alpha.130`.

<!-- KIT_CHANGELOG
1.0.0-alpha.130 — An axis band with reversed good and poor constants, or a
non-finite endpoint, is now refused with a named error instead of being scored.

1.0.0-alpha.129 — The kit now times the whole wait a person sits through after
they click something: from the click until the view it opened produces its own
measurement, rather than stopping at the moment the address changed. Both
halves are reported beside it, the wait before anything appeared and the time
the new view then took. A click that never reached a timed view is counted and
named rather than dropped: one count for a view that opened too late to be
joined, one for a click no view followed, and one for a view that ended with no
measurement of its own. A view with no reading of its own is never given an
earlier view's number.

Making that work in a real browser fixed something older. A browser stops
reporting its main paint measurement at the first thing a person clicks — that
is the measurement's own rule — so a kit waiting for the next paint after a
click waits for one that never comes. That is what this kit was doing: on a
real Chrome its soft-navigation reading produced no number at all, and every
view opened by a click was filed as having no measurement. The browser does
measure those views, and reports the timing on the view-change record itself,
which is where the kit now reads it. Because that measurement begins at the
click, it is used as the whole click-to-screen wait rather than added on top of
the wait already counted, and the two halves still add up to it.

1.0.0-alpha.128 — The guide now names every place this kit reads inside the
tab, and the opt-in body-error check keeps to the limit we publish. The guide
named one read — the AI-call meter's prompt prefix — and called it the only
body the kit reads, which was wrong: the same meter reads the provider's
streamed reply for the token counts it states and takes its own copy of a
refusal to say which kind it was, and a fixed list of headers is looked up by
name for the content type and a hosting platform's cache verdict. The cookie
check other kits ship is not one of them: a tab cannot see the Set-Cookie
lines your server sends, so this kit never reads a cookie at all. None of it
is uploaded. The body-error check asked for
a declared length and then accepted a reply that declared none, because an
absent header reads as zero; it now requires a real declared length of 64 KB
or less before a copy is read, and still drops anything longer once it
arrives. The one behaviour change is that an answer whose length the server
never declared is no longer read, so it can no longer be counted as
error-shaped.

1.0.0-alpha.127 — The kit now says so when the dashboard is overriding the
sharing mode your code asked for. `issuesOnly` is your setting, but a
project's "Full telemetry" switch wins in both directions — on forces full
mode even where the code asked for reduced, off forces reduced even where
the code asked for full — and nothing said so, so a developer could read
their own source and believe something false. The kit now warns once, in
the browser console, naming BOTH what your code asked for and what is
actually in force — including when the switch is flipped mid-visit. It also
reports the mode your code asked for when it registers, so your dashboard
can show the two values side by side rather than leaving you to guess what
turning the switch off would leave behind.

1.0.0-alpha.126 — The guide and README now say plainly what this kit puts into
the app it is installed in, before you ship it rather than after. The floating
bubble is described as on by default, with the setting that hides it named in
the same breath, and no guide now calls it a mandatory setup step. Every
address the kit mounts on the host app is listed with who can reach it, what
it answers and, where it is open to the public, why it is open and what it
deliberately leaves out. Nothing about what the kit measures, sends or
displays changed.

1.0.0-alpha.125 — A crash now says which page it happened on. This kit
captured crashes from the beginning and recorded only a signature and two
correlation ids, so a developer was told the app crashes and left to find
where, while a slow interaction on the very same page arrived labelled. The
page is read at the moment of capture from the address the document is on,
never from the last page seen, and it travels under the same naming rule and
the same privacy screen an interaction's label already goes through. A failure
raised before any page was readable says so in its own word rather than
borrowing one. Recurring problems, swallowed errors, failed outbound calls and
leak findings carry the same field.

1.0.0-alpha.124 — One rule now decides what a page is called, shared with every
other Boosthis kit. This kit already ran its page map, inventory and timings
through one normaliser; what changed is the number and the behaviour. The limit
moves from 120 characters to 100 — the same bound the server enforces, so a
name is never quietly reshaped on the way in — and an over-long name is now
refused rather than truncated, because two pages agreeing for their first
hundred characters were being filed as one, and a truncated name is one nobody
can search their own codebase for.

1.0.0-alpha.123 — Adds separate readings for promise rejections and rejections
handled late, plus an honest view of background work the page can observe.
Worker and service-worker work whose task boundaries stay hidden is counted as
an unattached system instead of being reported as healthy.

1.0.0-alpha.122 — Adds passive scheduling readings for platform-observed long
tasks, slow callbacks seen by the existing timer wrappers, and p95 event-loop
lag sampled on the existing timer-health cadence. Unsupported and warming
sources remain absent rather than reporting zero.

1.0.0-alpha.121 — An expired access answer no longer silences a copy of the
kit that never registered. Registration is the only way back from an expired
answer, and the same check stood in front of it, so an install holding no
credentials could be left unable to register — and it reported that as the
Boosthis API being unreachable when nothing had been sent. An expired answer
now silences only the install that earned it, which can check in and recover.

1.0.0-alpha.120 — Storage Speed and Storage Failures now identify their
readings as belonging to this page's own storage area, so uploaded readings
cannot be mistaken for device- or machine-wide storage measurements.

1.0.0-alpha.119 — Two corrections to what this kit measures and what it says
about a page. First, a page address whose identifier carries a word in front
of it is now replaced before the label is kept. Until now a segment was only
treated as an identifier when it was nothing but digits, a UUID, or a long
run of hex on its own; an address like
/snapshot/mcp-2066268df023bf5e9dd7aeb0 therefore travelled verbatim, because
"mcp-" is not a hex character. A long hex run anywhere inside a segment now
collapses to :id, so two visits to the same page with different identifiers
land on one label instead of two — and the identifier stays on the device.
Second, this kit's own uploads to the Boosthis API are no longer counted as
outbound calls your app made. The browser records every request the page
issues, including ours, so on a site served from the same origin as this
kit's own endpoint our uploads were being measured as your app's network
work and then reported back to you. They are now skipped where the browser
hands them to us. Nothing else changes: page weight still counts the whole
page, this kit included.

1.0.0-alpha.118 — The README that ships beside this kit now states the version
you are actually running. It had been left at 1.0.0-alpha.104 while thirteen
releases went out under it, and the changelog inside it stopped there too, so
the one file that travels with the code described a kit nobody is running.
Nothing this kit measures, sends or shows has changed — the code is identical
to 1.0.0-alpha.117. It is a release rather than a quiet correction because a
corrected file only reaches an installed kit when the version moves; at an
unchanged version every consumer is told it is already current, and the fix
never arrives.

1.0.0-alpha.117 — The badge's count of page views measured can now go past
one. It was read off this page's own in-memory readings, so a full navigation
threw it away and a single-page app never produced a second one: on a live
site, two different pages each showed "1 page view measured". Three things
changed. The kit now closes a page view itself whenever the address changes,
so a single-page app is measured without your app calling flushPageSample() —
keep that call only for screens that change without the address (a drawer, a
wizard step); calling it for a route change the kit already handled is safe
and counts nothing twice. The count is kept in this browser's own storage, so
it spans page loads on this device, and the badge says underneath exactly what
it covers. And the page you are looking at is included once the browser has
measured it, instead of the count always being one page behind.
flushPageSample() also no longer clears this document's paint, layout-shift
and interaction meters, which previously deleted the paint-readiness reading
for the rest of a visit. Nothing new is uploaded: the count is local to your
device and never leaves the page. See PAGE-VIEW-READINGS.md in this kit.

1.0.0-alpha.116 — The kit now says when your app's own shape is why so little
is measured. A reading is taken when a page view ends, so a single-page app is
measured about once a visit however many screens a person moves through: on a
live shop, two storefronts completing the same number of orders produced
hundreds of readings and fewer than ten, and nothing said why. The kit now
reports whether this page was used in place, navigated, sat idle, or could not
be watched — on the badge, in printWebStatus(), and on your project's own
report. It also stops repeating the first page's timing for later screens it
never measured: those are counted as views it could not time, and an app used
in place now uploads on the strength of that activity instead of looking like
an empty page. See PAGE-VIEW-READINGS.md in this kit.

1.0.0-alpha.115 — Housekeeping only: nothing this kit measures, sends or draws
has changed. The shared no-PII vocabulary now carries the screen for
background-job names alongside the one for route labels, so the browser kit's
copy stays byte-identical to the Node kit's and the two can never drift apart
on what counts as a value. This browser kit does not report background jobs,
so nothing here calls it yet.

1.0.0-alpha.114 — The kit no longer says measuring has started just because
Boosthis confirmed the install. A confirmation is permission, not a reading:
on a live shop the kit announced that measuring had begun while our own server
correctly reported that same install as never having measured anything. The
confirmation line now says only what a confirmation proves, and says what
produces a reading: one is taken when a page view ends. If a confirmed install
then goes on measuring nothing, the kit notices by itself and says so, instead
of leaving the earlier claim standing, and it withdraws that line the moment
the first reading is taken.

1.0.0-alpha.113 — The in-page meter panel no longer promises meters this kit
cannot report. Two dozen readings that depend on a browser API, a server
header or page content the visitor never loaded were drawn as rows saying
"measuring…" even where the reading could never arrive, which made a permanent
silence look like a temporary one. Those rows are now drawn only once the
reading actually exists, which is how the meter page on the web dashboard has
always treated them — so the two surfaces list the same meters for the same
app. Nothing new is collected, and no reading changed its value.

1.0.0-alpha.112 — Four readings were saying something about how they were
built rather than about your page. Request Mix no longer counts requests your
page made AFTER it finished loading: a section that refreshes itself every few
seconds, a search box, an infinite list — none of those is how the page was
BUILT, and counting them rated a deliberate live-updating section as bad page
construction. Only resources loaded before the load event are judged now, and
the excluded count is published beside the percentages. Service Worker Control
stops marking every app that simply does not use service workers as needing
work: a page with no registration at all gets no verdict, and the orange one
is kept for the case that is really a finding — a worker registered but not
driving this page. Connection Quality is no longer graded: it describes the
VISITOR'S network, not anything you built, so the round trip and downlink stay
as context beside your results and the score goes. And when the back/forward
cache refuses your page, the kit now names more of the reasons — a request
still in flight, a held connection, a browser-side refusal — and when it
genuinely cannot name one it says so instead of saying "other". A dependency
reading also never publishes a "worst" backend setup while holding a slower
one it did not judge.

1.0.0-alpha.111 — The install guide now says what the AI-call meter reads out
of an outbound prompt, in the same words our public pages use: to tell
repeated prompts apart it reads up to the first 4,096 characters of the
request body — in a browser, inside the visitor's own tab — and folds it into
a single number, with no body text stored, uploaded or recoverable. It was
true before this release and it was stated only where we sell the product,
which left the person deciding whether to ship it reading it last. Nothing
measured changed: no reading was added, removed or reworded.

1.0.0-alpha.110 — Two new readings about your page's own saved data — how long
localStorage and sessionStorage operations take, and what share of them fail.
A browser app has no database to wait on, so a slow or refusing local store is
the nearest honest answer to “what about my data access?”, and a store that
answers instantly and throws on every write looks perfectly healthy if only
its speed is shown. Both are OFF unless you switch them on: set
BOOSTHIS_STORAGE_METER, because this watches your own data path. The kit wraps
get/set/remove, calls your original through it, and re-throws exactly what it
threw — a full quota still reaches your own handler unchanged. Only durations
and outcomes are recorded: never a key, a value or a size. IndexedDB is
deliberately not watched, and the tile says which store it is about. With the
switch off, storage walled off, or too few operations, both readings stay
absent rather than reporting a zero nobody measured. Neither feeds your Speed
score.

1.0.0-alpha.109 — Your project page can now say WHICH control was pressed and
did nothing. This kit has counted dead presses for a whole page since it
shipped — “seven presses did nothing yesterday” — which you could neither find
nor act on. That count is unchanged, and beside it each control now carries
three tallies: presses that changed something, presses that changed nothing,
and presses the kit could not judge either way. It reuses the dead-click
verdict already being computed, so no new listener, no new request and no
extra work on the page. A control that changed something is reported as having
RESPONDED, never as correct — the kit sees that something changed, never
whether it was the right thing. Nothing about the press travels: no
coordinate, no time of day, no order, and still no label, text or value of any
kind. `setControlCensusEnabled(false)` or `BOOSTHIS_CONTROL_CENSUS=0` switches
it off with the rest of the census.

1.0.0-alpha.108 — The floating panel this kit serves inside your app now fits
a phone. It is capped against the screen width instead of a flat 300px, and
shades its top and bottom edge when there is more to scroll — a phone draws a
scrollbar only once you are already swiping, so there was previously nothing
to say the panel continued past the edge. Nothing measured changed: no reading
was added, removed or reworded.

1.0.0-alpha.107 — A registration that is still being retried no longer reads
as one that failed. The kit decides within about half a minute, but it keeps
asking for minutes afterwards — and a real app that registered three minutes
in had already been written up as broken. The status readout now separates
“not registered on this page yet, still trying” from “it never registered, and
here is what stopped it”, the waiting line says how long the kit keeps trying,
and a later success plainly withdraws the earlier line instead of standing
beside it. Registration is durable, so after a reload the readout also says
when this browser last watched Boosthis accept this install — marked as the
browser’s own record, never as Boosthis’s answer. The kit no longer says what
your dashboard has or has not received: it reports what THIS page sent, and
points at your project’s connection check to settle the rest.

1.0.0-alpha.106 — Your project page can now show how many controls a page HAS,
not just the ones somebody happened to press. The kit asks the rendered page
what controls it has — the same trick as reading your router’s route table,
one level down — so a button that exists and has never once been pressed is
visible, and so is one that leads nowhere. A control travels as a HASH OF ITS
POSITION in the page and nothing else: no label, no text, no value, no id,
nothing anyone typed. It is bounded (at most 300 listed, and it says so when a
page is bigger), it rides the readings you already upload, it adds no listener
and no new request, and `setControlCensusEnabled(false)` or
`BOOSTHIS_CONTROL_CENSUS=0` switches it off.

1.0.0-alpha.105 — A page that calls a model this kit does not recognise — a
model you host, a private gateway, a regional endpoint — no longer looks like
a page with no AI in it. A POST whose path is an inference shape is counted,
and the AI Wait row appears saying it cannot be measured, carrying that count
and nothing else: no address, no path, never timed, scored or priced. Naming
the endpoint in `startWebVitals({ aiEndpoints: […] })` still turns those calls
into a full reading.

1.0.0-alpha.104 — A page that calls a model this kit does not recognise —
a model you host, a private gateway, a regional endpoint — no longer looks
like a page with no AI in it. A POST whose path is an inference shape is
counted, and the AI Wait row appears saying it cannot be measured, carrying
that count and nothing else: no address, no path, never timed, scored or
priced. Naming the endpoint in `startWebVitals({ aiEndpoints: […] })` still
turns those calls into a full reading.

1.0.0-alpha.103 — Guide only; nothing this kit measures, sends or decides
changed. A page can already have its own model endpoint measured — a model you
host, a private gateway — by naming it in `startWebVitals({ aiEndpoints: [...]
})`, but the setup guide never mentioned it, so the only place that fact
appeared was a release note nobody reads twice. The settings reference now
carries the option, what happens without it (the AI rows are absent, which
looks exactly like a page with no AI in it), and why there is no environment
flag for it: browser code cannot read a server-side setting.

1.0.0-alpha.102 — The map of your pages can now reach your project page,
but only if you ask for it. Set `sendPageMap: true` and the map the kit
already draws in this browser rides the readings you already upload, so the
paths between your pages appear in the map section of your project page. It
is off by default and nothing we send back can turn it on. Only page names
and pairs of page names travel, each with a count, capped on the device and
screened exactly like the page labels you already send — the control that was
pressed and the gesture that moved someone never leave the browser, and there
is no field on the wire that could carry them. It obeys every gate the upload
it rides obeys: with telemetry off or the kill switch set, no map is sent, and
in issues-only mode it travels only if you have also switched on meter
sharing.

1.0.0-alpha.101 — Hand the kit your router with `registerRouter()` and its
whole route table is read when a report is taken, so the app map can show
pages nobody has visited — marked “not seen”, never “dead” or “unused”. Vue
Router and React Router answer; an unfamiliar shape reports nothing rather
than a guess, and no source file is ever read. Route patterns only.
`setRouteListEnabled(false)` switches it off.
1.0.0-alpha.99 — A correction to the failure counts added in the previous
release. A browser can end one request more than once — an error after a
partial load, a timeout racing a cancel — and older code reuses one request
object for several calls. Either could add a second answer to an address’s
count, or file a new answer against the address of the call before it. Each
send is now counted exactly once, and only against the address it was actually
sent to. Nothing new is watched and nothing new is sent.

1.0.0-alpha.98 — The calls your page makes can now say which ones FAILED, not
only how slow they were. The kit already decided whether a response counted as
a failure and already knew how to build a route label for it; it now keeps the
two together as a count of answers seen and answers failed. A call you
cancelled or that timed out is your page's own doing and is counted as neither.
The labels themselves never leave the browser — only four numbers do — and
nothing new is watched, so the page does no extra work. A route nobody called
reports nothing rather than a zero.

1.0.0-alpha.97 — A page you open for the first time now reaches your
dashboard in seconds instead of waiting out the fifteen-second upload timer.
The kit keeps a small in-memory note of the parts and calls it has already
seen in this page session; the first sight of a new one sends the batch
early, a burst of new pages is coalesced into one send, and it will not fire
more than once every five seconds. Repeat traffic never triggers it, so a
busy production page falls straight back to the ordinary timer. Nothing
extra is sent and no new address is called — the same upload, earlier.

1.0.0-alpha.96 — The map of your site now draws itself as pages get opened.
The kit already watched every page change; it now keeps the paths between
pages as a counted list beside the pages themselves, saves that map in this
browser, and adds to it on every reload — so the picture fills in as the site
gets used, with no code on any page and no router setup. The panel says how
many pages have been seen and how many routes you registered have not been
reached yet, and says when the map is a subset because it hit its cap. Nothing
new is sent: page names and paths stay in the browser exactly as before, and
no map is written while you are navigating — it is saved when the page is
hidden or closed.

1.0.0-alpha.95 — the panel and the status readout now answer “press this, it
opens that”. The click watch that already ran keeps which control was
pressed and joins it to the page that followed, so both surfaces draw the
pairs themselves — WHICH control opened WHICH page — and a control pressed
twice with nothing happening is shown as a dead end. A control is named only
by a code-defined test id (data-testid and friends); its text, accessibility
label, value and position are never read, and a control without one still
draws its arrow, counted as unnamed rather than dropped. An arrow is an
in-page navigation, and a press overtaken by a later one inside the same
window is counted but joined to nothing. All of it stays on the device;
nothing about a control is uploaded.

1.0.0-alpha.94 — Wording only; nothing the kit measures, sends or decides
changed. A comment in the kit's own source named a number of kits that was
two releases out of date. It now says “every kit”, which cannot go stale.

1.0.0-alpha.93 — A row with no score now says which silence it is. A reading
this browser cannot take reads “not available here”, a row that is
deliberately never graded reads “no score by design”, and only a row still
gathering data reads “pending” — which used to be the one word for all three.
Nothing measured changed.

1.0.0-alpha.92 — A fast route can no longer be reported as steady. Requests
are timed in whole milliseconds, so a handler that finishes in under half a
millisecond records as 0ms, and a route whose earlier window is all zeros
leaves the baseline axis nothing to divide by. Those routes were counted as
examined and then dropped in silence, so an app whose sub-millisecond route
suddenly took 100ms still scored 100 and read “steady”. A route that cannot be
divided by is now reported as too fast to compare, the steady count names only
the routes actually compared, and an app where every route was too fast says
it cannot judge rather than scoring full marks. Routes slow enough to compare
are judged exactly as before.

1.0.0-alpha.91 — build age no longer mistakes a server-rendered page's response
time for its build timestamp and calls it good. `document.lastModified` is used
only when distinguishable from navigation; otherwise `patchLag` reports build
age as unknown.

1.0.0-alpha.90 — the machine-wide rule cache is now published atomically —
written beside the target and renamed into place. Every Boosthis process on a
machine writes that cache, and a half-written file read back as no cache at
all, costing everyone a refetch until one process won a complete write.
Nothing about what is measured or sent changes.

1.0.0-alpha.89 — a page with NO build step can now be installed from this kit
alone. The files here are TypeScript modules that need a bundler, and a
project without one — a single index.html, a page written in a chat, a hosted
site builder — used to find nothing in the kit naming the pre-built script
that exists for exactly that case, because that address was only in the
written install guide. QUICKSTART.md now ships beside these files and names
it, this readme leads with it, and both are built from the one place the
address is written down, so no copy can name a different one. Nothing the kit
measures, sends or shows changes.

1.0.0-alpha.88 — a slow page can now name the hop that caused it. A measured
page interaction carries its own identity, and a fetch it makes sends that
identity alongside the trace id it already sent, so the backend hop is
recorded as a child of the page rather than merely alongside it. The dashboard
waterfall nests accordingly and names the hop responsible for the end-to-end
time. Both fields are optional, so a backend running an older kit still
produces a valid, readable trace. The ids are random 16-character tags —
nothing about them is derived from your data.

1.0.0-alpha.87 — this kit no longer announces a registration and then goes
quiet. After the startup line it holds itself to a deadline: if the
registration never happened — refused before anything was sent, thrown away
by an error on the page, or never even scheduled — it says so in one line
naming the cause, keeps trying in the background, and says once when it
finally goes through. A registration that was never actually sent, because
this project is locked or switched off, is no longer reported as registered
on any surface, and the status readout separates "announced, never
registered" from "registered and measuring". A batch of measurements that
never left the page, for that same reason, is no longer counted as delivered
either, and it never clears a real upload failure the page had already
reported. Measurements are held either way, exactly as before.

1.0.0-alpha.86 — a page that calls a model provider itself now gets the same
three AI readings the server kits report: how long the person waited for each
answer, what the answers cost, and how much of the provider's ceiling is left.
Streamed answers report their time to first words and any stall without the
answer ever being held. Where the browser is not allowed to read the
provider's rate-limit headers, the headroom reading says so instead of showing
a comfortable zero, and a provider call made through something the kit does
not wrap is counted as a blind spot. Numbers only: no prompt text, no answer
text, no key.

1.0.0-alpha.85 — a per-hour or per-minute figure is now published only when
the page watched long enough to earn it: at least a twelfth of the unit, so
five minutes for an hourly rate. Below that the count and the window are
still reported — a crash or a rage burst still shows the moment it happens —
but the projection and the rating drawn from it wait. Every published rate
now says how long it watched.

1.0.0-alpha.84 — Six new rules cover browser AI calls: bound `fetch` with
`AbortSignal.timeout`, honor `Retry-After`, start independent generations with
`Promise.all`, share one request-scoped Promise per logical prompt, keep
reusable prompt prefixes byte-stable for provider caching, and request then
consume streamed usage with `stream_options.include_usage`. Rule book only:
nothing new is measured and nothing new is sent.

1.0.0-alpha.83 — A deliberately cut project credential now makes the installed
kit stop and report the server's non-active status immediately. An ordinary
unauthorized response or an unreachable server still preserves the cached
entitlement, so recoverable installs are not stranded.

1.0.0-alpha.82 — Five new rules about calling an outside service from the
page: a fetch with no deadline (sign-in first), retries with no cap,
independent calls made one after another instead of together, a call the
visitor waits on but never sees, and a background-sync retry that never gives
up. Each names the fix, and is written against the browser's own mechanisms —
`AbortSignal.timeout`, a capped retry, `Promise.all`, `sendBeacon` and an
attempt ceiling. Rule book only: nothing new is measured and nothing new is
sent.

1.0.0-alpha.81 — a call the kit cannot honestly place on its trace is now left
out instead of drawn at the start of the action. The kit remembers where each
trace began in a table of fifty; a page with more calls in flight than that
could lose its own trace's start and report the next call at zero — a bar at the
left edge that can out-rank the layer the action really began on. Those calls
are now withheld and counted instead of reported in the wrong place.

1.0.0-alpha.80 — shows how much of what a page loads is reused instead of
fetched: the share served from the browser's own cache, what the rest cost in
bytes and time over the network, and this site's own hosting cache verdict
where a reply declares one. Replies from other origins are left alone —
somebody else's cache is not yours. Sizes, durations and verdict words only,
never an address. Additive: no change to your speed score.

1.0.0-alpha.79 — reports the problems it keeps seeing in real visitors'
browsers: a slow page view, a call that is slow or keeps failing, a burst of
identical calls at once, a live connection that keeps reconnecting or has gone
quiet. Fix outcomes are reported too, so a change that worked can be
recommended to someone else. Nothing new is measured — each report is read back
from a reading this kit already takes. Only a hashed signature travels (kind,
severity band, rough count); never a page address, URL, value or code. Reports
ride the same terms as the kit's other uploads, including surviving the page
being closed.

1.0.0-alpha.78 — the kit's own readme now travels with the code. This kit is
written straight into your project, with no package page or repository link
beside it, so until now the web kit arrived as source files with nothing next
to them saying what it is or what each version changed — the only runtime that
shipped without one. Nothing this kit measures, sends or shows has changed.

1.0.0-alpha.77 — says how far this app is from its own backend, measured on
connection setup alone — opening and securing a new connection, before any
request bytes — so a backend that is merely slow to answer is never called
distant. Own backend only: a browser hides these phases for other services
unless they opt in, so this kit stays silent about them rather than showing a
zero. Reused connections are skipped and several genuinely new ones are needed
before it speaks at all. Only timings leave the page. Pro plans and above.

1.0.0-alpha.76 — connections a page holds open (chat, live dashboards,
presence, notifications, streamed answers) are now measured: how many are open,
how long they last, how they end and how often they come back. A connection
silent far past its own rhythm is reported as likely dead; an ordinary pause is
left alone; where there is no rhythm to judge by the kit says so. Reconnect
storms and reconnect-on-every-screen are named separately. Counts only, never
message content. A page that holds nothing open reports nothing here.


1.0.0-alpha.75 — keeps this kit's privacy guard in step with the new AI-call
reading: three token-count field names are now allowed through by exact name.
Only the Node and Python kits ever send those fields, so nothing this kit
measures, sends or shows changes.

1.0.0-alpha.74 — outbound calls are now watched where they START, so a call that
hangs and never comes back is counted instead of vanishing, and hung, timed out,
aborted and failed calls are told apart. Each call is also filed under a safe
group drawn from a closed vocabulary (never its address), so a project page can
name which backend is slow. Apps where wrapping is impossible keep exactly the
completion-based readings they had.

1.0.0-alpha.73 — startup now names browser capabilities that are unavailable,
so readings omitted for an unsupported browser API no longer disappear without
an explanation.
-->

## Install

There are two installs, and which one you need is decided by one question:
**does this project have a bundler?**

### No build step — a single `index.html`, a page written in a chat, a no-code site

Nothing to download, nothing to compile. Paste one line into every page's
`<head>`:

```html
<script defer src="https://www.boosthis.com/kit.js" data-key="<the project key>" data-install="<a UUID v4 you generate ONCE>" data-project="<friendly name>" onerror="console.error('[boosthis] Boosthis did not load: '+this.src+' never arrived, so nothing is measuring. Check the address, the network, and any Content-Security-Policy.')"></script>
```

`https://www.boosthis.com/kit.js` is the only address there is — the same
runtime as the modules below, served pre-built. Keep the `onerror` handler: it
is the only piece of Boosthis that lives in your own page, so it is the only
thing that can speak if the file never arrives. Generate `data-install` once
and reuse that same UUID on every page.

Full detail — what a working install looks like in the console, the
Content-Security-Policy line, and the attributes that hide the badge or reduce
what is reported — is in `QUICKSTART.md`, which ships beside this file.

<!-- The address above is checked against src/hostedScript.ts, the one place it
     is written down, by artifacts/api-server/src/routes/__tests__/kitTag.test.ts
     and by the kit packer. Change it there, never here. -->

### With a bundler — the module install

`@workspace/boosthis-runtime-web` is this folder's own package name. It is
**not on any registry**, so point your project at the folder you unpacked
before importing it:

```jsonc
// package.json — plain app
"dependencies": {
  "@workspace/boosthis-runtime-web": "file:./lib/boosthis-runtime-web"
}
// …or, inside a pnpm/npm workspace repo:
"dependencies": {
  "@workspace/boosthis-runtime-web": "workspace:*"
}
```

Then mount the floating bubble when wiring Web Vitals, in your app entry
module:

```ts
import { startWebVitals } from "@workspace/boosthis-runtime-web";

startWebVitals({ bubble: true });
```

The floating bubble is on by default in every environment, including
production. `BOOSTHIS_BUBBLE` set to 0 hides it and the kit carries on
measuring; `BOOSTHIS_DISABLED` stops the kit altogether. Each name is read
from the environment where this runtime has one, and otherwise from a value
of the same name on the host.

The full visibility precedence is: `BOOSTHIS_DISABLED`
(fail-closed kill switch), then the trimmed, case-insensitive
`BOOSTHIS_BUBBLE` directive (`1`/`true`/`yes`/`on` shows;
`0`/`false`/`no`/`off` hides), then legacy `BOOSTHIS_NO_BUBBLE` (hide), legacy
`BOOSTHIS_FORCE_BUBBLE` (show), the `bubble` code option, and finally visible.
A settings entry may be added as an extra; it does not replace mounting the
bubble.

## One codebase can hold more than one project

Set `globalThis.__BOOSTHIS_PROJECT_KEY_WEB__ = "<YOUR_PROJECT_KEY>"` before
starting the kit when this browser app must report to a different project from
another runtime in the same repository. The host global wins over the key
passed to `enableTelemetry` (including a bundler-provided value or the script
tag's `data-key`). `getActiveProjectKeyStatus()` reports the masked key and its
source; it never returns the key itself.

## What is this install actually doing?

`printWebStatus()` prints the kit's status readout — the browser equivalent of
the `/_boosthis/status` page a back-end kit serves: kit version, install id,
the masked project key and where that value came from, and whether Boosthis
says this app is registered (`yes`, `Can't tell right now`, or a `no` that says
whose no it is — Boosthis has no such install on file, or Boosthis never
started on this page), plus the reason sentence when something was refused.
`webStatusLines()` returns the same lines as an array. Neither returns the
project key, a token, or any server text.

The `Announced registering` row below it is about THIS page load, and it keeps
two states apart that used to read as one: `not registered on this page yet,
still trying` (the kit is still asking on its own — a page that registers three
or four minutes in is normal, so give it about ten minutes) and `but it never
registered on this page` (nothing will change until somebody changes it, and
the sentence below says what). A registration is durable, so after a reload the
readout also shows `Registered before in this browser` when this browser has
watched Boosthis accept this install id before — that is this browser's own
record, not Boosthis's answer. Where the local rows cannot be conclusive, the
project's connection check on your Boosthis dashboard is what settles it; the
kit never claims what Boosthis has or has not received.
