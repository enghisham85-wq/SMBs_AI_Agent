"""End-of-day harness steps: approval timeouts and the owner's daily digest."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select

from app.approvals import service as approvals
from app.db.engine import read_session
from app.harness import calibration
from app.models.harness import AgentCalibration, ApprovalRequest, AuditLogEntry, Incident, LearnedRule

MAX_FLAGS_IN_MESSAGE = 5


async def collect(business_id: uuid.UUID, d: date) -> dict[str, Any]:
    start, end = datetime.combine(d, time.min), datetime.combine(d + timedelta(days=1), time.min)
    async with read_session() as s:
        flags = (await s.execute(select(AuditLogEntry).where(
            AuditLogEntry.business_id == business_id, AuditLogEntry.event == "act_flag",
            AuditLogEntry.business_time >= start, AuditLogEntry.business_time < end)
            .order_by(AuditLogEntry.business_time))).scalars().all()
        incidents = (await s.execute(select(Incident).where(
            Incident.business_id == business_id, Incident.created_at >= start, Incident.created_at < end))).scalars().all()
        proposed = (await s.execute(select(LearnedRule).where(LearnedRule.business_id == business_id,
                                                              LearnedRule.status == "proposed"))).scalars().all()
        pending = (await s.execute(select(ApprovalRequest).where(
            ApprovalRequest.business_id == business_id, ApprovalRequest.status == "pending",
            ApprovalRequest.kind != "alert"))).scalars().all()
        timed_out = (await s.execute(select(ApprovalRequest).where(
            ApprovalRequest.business_id == business_id, ApprovalRequest.status == "timed_out",
            ApprovalRequest.resolved_at >= start, ApprovalRequest.resolved_at < end))).scalars().all()
        degraded = (await s.execute(select(AgentCalibration).where(AgentCalibration.business_id == business_id,
                                                                   AgentCalibration.degraded.is_(True)))).scalars().all()
    seen: set[str] = set()
    items = []
    for f in flags:
        key = f.inputs.get("text_en", "")
        if key in seen:
            continue
        seen.add(key)
        items.append({"agent": f.agent, "text_en": key, "text_ar": f.inputs.get("text_ar", key),
                      "confidence": f.inputs.get("confidence"), "action_id": f.action_id, "refs": f.inputs.get("refs", {})})
    return {
        "date": d, "act_flag": items,
        "incidents": [{"id": i.id, "agent": i.agent, "type": i.type, "summary": i.summary, "status": i.status} for i in incidents],
        "rules_proposed": [{"id": r.id, "text_en": r.rule_text_en, "text_ar": r.rule_text_ar} for r in proposed],
        "pending_requests": len(pending), "timed_out": len(timed_out),
        "degraded": [{"agent": c.agent, "metric": c.metric} for c in degraded],
    }


def _text(dg: dict[str, Any]) -> tuple[str, str]:
    d: date = dg["date"]
    en = [f"Daily summary for {d.strftime('%d %b')}:"]
    ar = [f"ملخص يوم {d.strftime('%d/%m')}:"]
    flags = dg["act_flag"]
    if flags:
        en.append(f"{len(flags)} item(s) done with medium confidence - please glance at them:")
        ar.append(f"{len(flags)} بنود نُفذت بثقة متوسطة - يرجى مراجعتها سريعاً:")
        for f in flags[:MAX_FLAGS_IN_MESSAGE]:
            en.append(f"- {f['text_en']}")
            ar.append(f"- {f['text_ar']}")
    if dg["incidents"]:
        en.append(f"{len(dg['incidents'])} issue(s) caught and logged today.")
        ar.append(f"تم رصد وتسجيل {len(dg['incidents'])} مشكلة اليوم.")
    if dg["rules_proposed"]:
        en.append(f"{len(dg['rules_proposed'])} learned rule(s) waiting for your approval.")
        ar.append(f"{len(dg['rules_proposed'])} قاعدة مُتعلَّمة بانتظار موافقتك.")
    if dg["pending_requests"]:
        en.append(f"{dg['pending_requests']} request(s) still need an answer" +
                  (f" ({dg['timed_out']} timed out and were re-sent)." if dg["timed_out"] else "."))
        ar.append(f"{dg['pending_requests']} طلبات ما زالت تنتظر رداً.")
    if dg["degraded"]:
        names = ", ".join(sorted({c["agent"] for c in dg["degraded"]}))
        en.append(f"Working more carefully (lower auto-approval) for: {names}.")
        ar.append(f"أعمل بحذر أكبر (موافقة تلقائية أقل) لـ: {names}.")
    if len(en) == 1:
        en.append("Nothing needs your attention.")
        ar.append("لا شيء يحتاج انتباهك.")
    return "\n".join(en), "\n".join(ar)


async def send(business_id: uuid.UUID, d: date) -> dict[str, Any]:
    dg = await collect(business_id, d)
    en, ar = _text(dg)
    await approvals.post_alert(business_id=business_id, agent="harness", text_en=en[:1000], text_ar=ar[:1000],
                               dedupe_key=f"digest:{d.isoformat()}", context={"digest_date": d.isoformat(), "kind": "digest"})
    return dg


async def step_approval_timeouts(business_id: uuid.UUID, d: date) -> int:
    return await approvals.expire_due(business_id)


async def step_digest(business_id: uuid.UUID, d: date) -> dict[str, Any]:
    await calibration.close_day(business_id, d)
    return await send(business_id, d)


def register() -> None:
    from app.graphs.daily_run import register_step

    register_step("approval_timeouts", "expire_due", step_approval_timeouts)
    register_step("digest", "daily_digest", step_digest)
