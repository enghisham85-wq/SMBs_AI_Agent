"""Standing promises the app's own code declares.

BOOSTHIS_PROMISE_IN_CODE: declare_promise

That line is the marker saying this kit ships the in-code promise call, and the
name after the colon is the exact call a developer types. It is read off the
kit's own served bytes so every guide can state, from what the kit actually
contains rather than from a list somebody keeps by hand, whether this call
exists here and what it is called. A kit added later is therefore never
silently missing the answer.

WHY THIS EXISTS. A promise had exactly two doors: the project page, and a
connected AI writing one on the developer's behalf. Neither is beside the code
the promise is about, neither can be reviewed in the pull request that changes
that code, and neither arrives unless somebody remembers the feature exists.
``expect_every()`` already had the third door — a declaration in the app's own
source, carried up on the job-runs upload — and a promise is the same kind of
statement, so it uses the same door.

WHAT IT IS NOT. A way to confirm anything. A promise declared here lands
REMEMBERED ONLY and stays that way until a human confirms the interpretation
on the project page, exactly like an AI-written one. There is no call here to
confirm, watch, unwatch or delete a promise, and no wire field that could
carry one. That line is the whole reason "watched" means something.

WHY THERE IS NO RE-ASKING PACE, UNLIKE A JOB RHYTHM. A declared rhythm is
re-stated every few minutes because the kit judges its OWN missed-run count
against the number the server accepted, so an owner's correction has to reach
a process that is already running. Nothing here is judged locally: a promise is
stated, answered once, and that is the end of the exchange. Worse than useless,
a re-asking pace would raise a promise the owner had just deleted on the
project page, over and over, for as long as the process ran. A statement is
re-sent when it CHANGES, and at no other time.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from .pii import check_route_label
from .runtime_flags import is_boosthis_disabled

#: The server's cap, and the most promises a project may hold.
MAX_PROMISE_DECLARATIONS = 20
#: The server's own wording cap.
MAX_PROMISE_WORDING = 240
#: The server's own subject-label cap.
MAX_PROMISE_SUBJECT_LABEL = 200

#: The four subject kinds the project page offers. No new ones: a kit cannot
#: invent something for the server to measure.
PROMISE_SUBJECT_KINDS = ("route", "issue", "axis", "job")
#: The ten measurements the project page offers. No new ones.
PROMISE_METRICS = (
    "p50_ms",
    "p75_ms",
    "p95_ms",
    "occurrences",
    "rate_per_day",
    "daily_ceiling",
    "daily_flag",
    "job_runs_per_day",
    "job_failures_per_day",
    "job_typical_ms",
)

#: Declared and not yet sent — ``{identity: declaration}``.
_pending: dict[str, dict[str, Any]] = {}
#: Sent, awaiting this upload's answer. Restored to pending if none comes.
_in_flight: dict[str, dict[str, Any]] = {}
#: Answered, with the fingerprint of WHAT was answered. A restatement of the
#: same thing is never re-sent; a changed one is. This is what keeps a restart
#: quiet within a process, and what makes an edited sentence travel.
_acknowledged: dict[str, str] = {}
_undelivered_rounds = 0
_warned_causes: set[str] = set()
_lock = threading.Lock()

_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def promise_identity(wording: str) -> str:
    """The identity the server matches a re-declaration on.

    The same normalisation the store uses, so the kit and the server agree
    about which sentences are one promise without the kit having to be told.
    """
    lowered = wording.lower()
    return _SPACES.sub(" ", _NON_WORD.sub(" ", lowered)).strip()


def _fingerprint(declaration: dict[str, Any]) -> str:
    """Everything a statement says, as one comparable string, so a declaration
    that moves only the LINE still counts as a change."""
    return "|".join(
        [
            promise_identity(str(declaration.get("wording") or "")),
            str(declaration.get("subjectKind") or ""),
            str(declaration.get("subjectLabel") or ""),
            str(declaration.get("metric") or ""),
            ""
            if declaration.get("threshold") is None
            else str(declaration["threshold"]),
        ]
    )


# ── Saying so, out loud, once ───────────────────────────────────────────────
#
# Same contract as the declared rhythms: one ungated warning per cause per
# process, naming the cause AND what the developer changes. A declaration API
# that validates and never reports back is worse than none.


def _warn_once(cause: str, message: str) -> None:
    with _lock:
        if cause in _warned_causes:
            return
        _warned_causes.add(cause)
    try:
        import sys

        print(message, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass  # A broken stream must never take the host down.


def promise_refusal_message(wording: str, reason: Any) -> str:
    """What the developer is told for each refusal the server can name."""
    quoted = f"\u201c{wording}\u201d"
    if reason == "cap":
        return (
            f"[boosthis] This project already holds the most promises it can "
            f"({MAX_PROMISE_DECLARATIONS}), so the promise {quoted} declared "
            f"in this app's code was NOT saved. Delete one on the project's "
            f"promises page to free a slot."
        )
    if reason == "screened":
        return (
            f"[boosthis] Boosthis will not store the promise {quoted} declared "
            f"in this app's code: its wording did not pass the privacy guard, "
            f"so nothing was saved. Write the promise without an address, a "
            f"token or anyone's personal details in it."
        )
    if reason == "unmeasurable":
        return (
            f"[boosthis] Boosthis cannot measure the promise {quoted} as this "
            f"app's code describes it, so nothing was saved. Give subject_kind, "
            f"subject_label, metric and threshold together or leave all four "
            f"out — and check the measurement suits the subject (a load time is "
            f"not something a reported problem has)."
        )
    if reason == "superseded":
        return (
            f"[boosthis] The promise {quoted} was edited or confirmed on this "
            f"project's promises page, so THAT version is in force and this "
            f"app's code did not overwrite it. What the code now says is "
            f"recorded beside it — change it on that page if the code's "
            f"wording is the one you want."
        )
    return (
        f"[boosthis] Boosthis did not save the promise {quoted} declared in "
        f"this app's code. Its project page says why."
    )


def declare_promise(
    wording: str,
    *,
    subject_kind: str | None = None,
    subject_label: str | None = None,
    metric: str | None = None,
    threshold: float | None = None,
) -> None:
    """Declare a standing promise from the app's own code.

    Buffered UNCONDITIONALLY — even with no transport wired yet. This is
    normally called while modules import, routinely before the kit has finished
    starting, and dropping it there would recreate the exact fault it exists to
    fix.

    ALL FOUR OR NONE for the measurement. Naming a subject without a
    measurement, or a measurement without a line, describes nothing anyone
    could judge. A promise with none of them is a sentence the owner can attach
    a measurement to later on the project page, which is the ordinary case and
    needs no keywords at all.

    Restating the same promise is free: it collapses onto the pending entry,
    and once the server has answered it the kit stops re-sending it. Never
    raises, never blocks, never starts anything.
    """
    try:
        words = wording.strip() if isinstance(wording, str) else ""
        if not words:
            return
        if len(words) > MAX_PROMISE_WORDING:
            _warn_once(
                "too_long",
                "[boosthis] A promise declared in this app's code is longer "
                f"than the {MAX_PROMISE_WORDING} characters Boosthis stores, "
                "so it was NOT sent. Shorten it to one plain sentence.",
            )
            return
        declaration: dict[str, Any] = {"wording": words}

        label = subject_label.strip() if isinstance(subject_label, str) else None
        any_given = (
            subject_kind is not None
            or (label is not None and label != "")
            or metric is not None
            or threshold is not None
        )
        if any_given:
            # Refused HERE rather than sent and refused there, because the kit
            # can name the keyword: the server only knows the shape did not
            # add up.
            if (
                subject_kind is None
                or not label
                or metric is None
                or not isinstance(threshold, (int, float))
                or isinstance(threshold, bool)
            ):
                _warn_once(
                    "partial_measure",
                    f"[boosthis] The promise \u201c{words}\u201d declared in "
                    "this app's code names only part of a measurement, so it "
                    "was NOT sent. Give subject_kind, subject_label, metric "
                    "and threshold together, or leave all four out and attach "
                    "the measurement on the project's promises page.",
                )
                return
            if subject_kind not in PROMISE_SUBJECT_KINDS or metric not in PROMISE_METRICS:
                _warn_once(
                    "unknown_measure",
                    f"[boosthis] The promise \u201c{words}\u201d declared in "
                    "this app's code names a subject kind or measurement "
                    "Boosthis does not have, so it was NOT sent. subject_kind "
                    f"is one of {', '.join(PROMISE_SUBJECT_KINDS)}; the "
                    "project's promises page lists the measurements each one "
                    "takes.",
                )
                return
            # A subject label is a reporting label, and rides the same guard
            # every route and screen name does. Dropping the WHOLE declaration
            # is the honest answer: sending the sentence without its
            # measurement would store a promise the developer did not write.
            if check_route_label(label) is not None:
                _warn_once(
                    "local_label",
                    f"[boosthis] The promise \u201c{words}\u201d declared in "
                    "this app's code names a subject that looks like it was "
                    "built from a value, so it was NOT sent. Name routes, "
                    "readings and jobs in code, the way /checkout and "
                    "nightly-billing are written.",
                )
                return
            declaration["subjectKind"] = subject_kind
            declaration["subjectLabel"] = label[:MAX_PROMISE_SUBJECT_LABEL]
            declaration["metric"] = metric
            # Named "threshold", never "thresholdValue": a field name carrying
            # "value" is refused by the server's PII guard, which refuses the
            # WHOLE upload — the finished job runs in the same body included.
            declaration["threshold"] = max(0, int(round(float(threshold))))

        key = promise_identity(words)
        if not key:
            return
        with _lock:
            if _acknowledged.get(key) == _fingerprint(declaration):
                return
            known = set(_pending) | set(_in_flight) | set(_acknowledged)
            if key not in known and len(known) >= MAX_PROMISE_DECLARATIONS:
                over_cap = True
            else:
                over_cap = False
                _pending[key] = declaration
        if over_cap:
            _warn_once(
                "too_many",
                "[boosthis] This app declared more than "
                f"{MAX_PROMISE_DECLARATIONS} promises in code; "
                f"\u201c{words}\u201d was not sent. A project holds at most "
                f"{MAX_PROMISE_DECLARATIONS} promises — declare the ones that "
                "matter.",
            )
    except Exception:  # noqa: BLE001
        pass  # A guest never fails its host.


def has_pending_promise_declarations() -> bool:
    """Whether there is a declaration worth making an upload for.

    A promise declared by an app that reports no job runs at all must still
    travel: this upload is the only door it has.
    """
    with _lock:
        return bool(_pending)


def pending_promise_declaration_count() -> int:
    """How many declarations have NOT been answered. One in this count is a
    promise the project does NOT hold yet."""
    with _lock:
        return len(_pending) + len(_in_flight)


def take_promise_declarations_for_send() -> list[dict[str, Any]]:
    """Take the declarations for one send, moving them out of pending.

    A label that would fail the guard has already been refused at declaration
    time, so what comes out of here is exactly what goes on the wire.

    The caller holds ``_state_lock`` in :mod:`boosthis.job_reporter`; this
    module's own lock is separate and is never held across a submitter call.
    """
    if is_boosthis_disabled():
        return []
    with _lock:
        sending = list(_pending.values())[:MAX_PROMISE_DECLARATIONS]
        for declaration in sending:
            _in_flight[promise_identity(str(declaration["wording"]))] = declaration
        _pending.clear()
        return sending


def parse_promise_declaration_outcomes(body: Any) -> list[dict[str, Any]] | None:
    """Read the outcomes out of an upload reply body.

    ``None`` means the reply said NOTHING about declarations — an older server,
    a body that is not a mapping, a missing field. That is deliberately
    different from ``[]``, a server that answered about none of them.
    """
    try:
        if not isinstance(body, dict):
            return None
        raw = body.get("promises")
        if not isinstance(raw, list):
            return None
        out: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            wording = entry.get("wording")
            if not isinstance(wording, str) or not wording:
                continue
            outcome: dict[str, Any] = {
                "wording": wording,
                "stored": entry.get("stored") is True,
            }
            if isinstance(entry.get("reason"), str):
                outcome["reason"] = entry["reason"]
            out.append(outcome)
        return out
    except Exception:  # noqa: BLE001
        return None


def note_promise_declaration_outcomes(outcomes: list[dict[str, Any]]) -> None:
    """Record what one upload answered.

    A promise the server did not mention is deliberately NOT treated as
    accepted: it goes back on the pending queue and is asked about again. An
    older server that knows nothing of code-declared promises answers nothing,
    so its silence keeps the statement open and eventually says so, rather than
    being read as a yes.

    A REFUSAL is answered all the same — it is a definitive answer, so the
    statement is closed and printed once. Re-sending a refused declaration on
    every flush would turn one refusal into a permanent stream of uploads about
    a promise that is never going to be stored.
    """
    try:
        refusals: list[tuple[str, str, Any]] = []
        with _lock:
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                wording = outcome.get("wording")
                if not isinstance(wording, str):
                    continue
                key = promise_identity(wording)
                asked = _in_flight.pop(key, None)
                if asked is not None:
                    _acknowledged[key] = _fingerprint(asked)
                if outcome.get("stored") is True:
                    continue
                reason = outcome.get("reason")
                refusals.append(
                    (
                        f"refused:{reason if isinstance(reason, str) and reason else 'unknown'}",
                        wording,
                        reason,
                    )
                )
        # Printed OUTSIDE the state lock: a host's stderr can block.
        for cause, wording, reason in refusals:
            _warn_once(cause, promise_refusal_message(wording, reason))
    except Exception:  # noqa: BLE001
        pass  # Reading an answer must never break an upload.


def restore_promise_declarations(
    *,
    delivered: bool,
    sent_count: int,
    rounds_before_warning: int,
    gate: tuple[str, str] | None = None,
) -> None:
    """Put back anything the send did not get an answer for, and say so once
    when it keeps happening.

    ``delivered`` separates the two stories a developer needs told apart: an
    upload that ARRIVED and said nothing about the declarations is an older
    server, and an upload that never arrived is a delivery problem — with
    ``gate`` naming this kit's own gate when it is the thing stopping it, so
    nobody is sent to inspect a network that is working perfectly.
    """
    global _undelivered_rounds
    with _lock:
        if not _in_flight:
            if sent_count > 0:
                _undelivered_rounds = 0
            return
        for key, declaration in _in_flight.items():
            _pending.setdefault(key, declaration)
        _in_flight.clear()
        _undelivered_rounds += 1
        rounds = _undelivered_rounds
        one = len(_pending) == 1
    if delivered and rounds == 1:
        _warn_once(
            "unanswered",
            "[boosthis] Boosthis accepted this app's upload but said nothing "
            f"about the {'promise' if one else 'promises'} declared in its "
            f"code — {'it is' if one else 'they are'} NOT saved yet. This "
            "usually means the Boosthis server is older than this kit; "
            "updating it fixes it, and the declaration keeps being re-sent "
            "meanwhile.",
        )
    elif not delivered and rounds >= rounds_before_warning:
        _warn_once(
            f"undelivered:{gate[0]}" if gate else "undelivered",
            "[boosthis] Boosthis could not deliver the "
            f"{'promise' if one else 'promises'} this app declares in code, "
            f"after {rounds} attempts — {'it is' if one else 'they are'} NOT "
            "saved. "
            + (
                gate[1]
                if gate
                else "Check this app can reach Boosthis; the declaration keeps "
                "being re-sent."
            ),
        )


def clear_promise_declarations() -> None:
    """Forget every declared promise (called by ``forget()`` and test reset)."""
    global _undelivered_rounds
    with _lock:
        _pending.clear()
        _in_flight.clear()
        _acknowledged.clear()
        _warned_causes.clear()
        _undelivered_rounds = 0
