"""Escalating payment reminders and promise tracking (FR-027).

Level 1 is polite, level 3 firm. Customers with a late-payment history get a friendly note before
the due date and the next levels sooner. An open promise to pay pauses reminders until the
promised date; a broken promise moves straight to the next level.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from app.db.types import Money

LATE_HISTORY = 0.3
# Days relative to the due date when each level is scheduled.
SCHEDULE = {1: 1, 2: 8, 3: 22}
SCHEDULE_LATE_HISTORY = {1: -3, 2: 5, 3: 15}
MIN_GAP_DAYS = 5  # between two reminders to the same customer


@dataclass(frozen=True)
class PromiseState:
    promised_date: date
    kept: bool | None  # None while the date has not passed


@dataclass(frozen=True)
class NextReminder:
    level: int
    scheduled_for: date
    reason: str  # schedule | broken_promise


def schedule_for(level: int, due: date, late_score: float) -> date:
    offsets = SCHEDULE_LATE_HISTORY if late_score >= LATE_HISTORY else SCHEDULE
    return due + timedelta(days=offsets[level])


def next_reminder(due: date, late_score: float, sent: dict[int, date], today: date,
                  promise: PromiseState | None) -> NextReminder | None:
    """The next reminder to schedule for an unpaid invoice, or None (all sent, or paused by a promise)."""
    level = max(sent, default=0) + 1
    if level > 3:
        return None
    if promise is not None and promise.kept is None and promise.promised_date >= today:
        return None  # wait for the promised date
    last_sent = max(sent.values(), default=None)
    earliest = last_sent + timedelta(days=MIN_GAP_DAYS) if last_sent else date.min
    if promise is not None and promise.kept is False and promise.promised_date < today:
        return NextReminder(level, max(today, earliest), "broken_promise")
    return NextReminder(level, max(schedule_for(level, due, late_score), earliest), "schedule")


def texts(level: int, customer: str, number: str, owed: Money, due: date, business_name: str) -> tuple[str, str]:
    d_en, d_ar = due.strftime("%d %b %Y"), due.strftime("%d/%m/%Y")
    amt_en, amt_ar = owed.to_display(), owed.to_display("ar")
    if level == 1:
        return (f"Dear {customer}, a friendly reminder that invoice {number} for {amt_en} is due on {d_en}. "
                f"If you have already paid, thank you and please ignore this message. - {business_name}",
                f"عزيزنا {customer}، نذكّركم بلطف بأن الفاتورة {number} بقيمة {amt_ar} تستحق في {d_ar}. "
                f"إذا كنتم قد دفعتم بالفعل فشكراً لكم ويرجى تجاهل هذه الرسالة. - {business_name}")
    if level == 2:
        return (f"Dear {customer}, invoice {number} for {amt_en} was due on {d_en} and is still unpaid. "
                f"Please arrange payment this week or let us know when to expect it. - {business_name}",
                f"عزيزنا {customer}، الفاتورة {number} بقيمة {amt_ar} استحقت في {d_ar} ولم تُسدد بعد. "
                f"يرجى السداد هذا الأسبوع أو إبلاغنا بموعد الدفع المتوقع. - {business_name}")
    return (f"Dear {customer}, invoice {number} for {amt_en} is now well overdue (due {d_en}). "
            f"Please pay within 5 days to avoid pausing further credit. - {business_name}",
            f"عزيزنا {customer}، الفاتورة {number} بقيمة {amt_ar} متأخرة كثيراً (استحقت في {d_ar}). "
            f"يرجى السداد خلال 5 أيام لتجنب إيقاف التعامل الآجل. - {business_name}")
