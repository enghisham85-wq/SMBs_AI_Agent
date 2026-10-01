"""US2 acceptance (T066): invoice capture, checks, posting, bank reconciliation, corrections."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app import wiring
from app.agents.accountant import classification, posting, supplier_match
from app.agents.accountant.intake import submit
from app.approvals import service as approvals
from app.config import get_settings
from app.core import clock, scheduler
from app.db.engine import read_session, write_session
from app.db.types import Money
from app.models.books import Document, Extraction, JournalEntry, JournalLine, PayableInvoice
from app.models.finance_master import BankTransaction
from app.models.harness import ApprovalRequest, Incident, LearnedRule
from app.models.master import Item, Supplier
from app.models.purchasing import Delivery, DeliveryLine, PurchaseOrder, PurchaseOrderLine
from app.models.tenancy import User
from app.seed.sample_cafe import seed

START = date(2026, 10, 4)


@pytest.fixture
async def cafe(db: None, tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setattr(get_settings(), "SAMPLE_INVOICES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(get_settings(), "FILES_DIR", str(tmp_path / "files"))
    wiring.register_all()
    info = await seed(start_date=START, history_days=42)
    info["samples"] = tmp_path / "samples"
    return info


class Owner:
    def __init__(self, uid: uuid.UUID) -> None:
        self.id, self.role, self.username = uid, "owner", "owner"


async def _owner(bid: uuid.UUID) -> Owner:
    async with read_session() as s:
        return Owner((await s.execute(select(User.id).where(User.business_id == bid, User.role == "owner"))).scalar_one())


async def _submit(cafe: dict[str, Any], name: str) -> dict[str, Any]:
    files = {f["name"]: f for f in json.loads((cafe["samples"] / "files.json").read_text())}
    f = files[name]
    return await submit(cafe["business_id"], (cafe["samples"] / f["file"]).read_bytes(), f["mime"], f["file"], "dashboard", None)


async def _question(doc_id: str) -> ApprovalRequest:
    async with read_session() as s:
        return (await s.execute(select(ApprovalRequest).where(ApprovalRequest.graph_thread_id == f"document:{doc_id}",
                                                              ApprovalRequest.status == "pending"))).scalar_one()


async def _answer(cafe: dict[str, Any], doc_id: str, key: str) -> None:
    q = await _question(doc_id)
    res = await approvals.resolve(str(q.id), key, await _owner(cafe["business_id"]), "dashboard")
    assert res.status == "resolved", res.status


async def _invoice(invoice_id: str) -> PayableInvoice:
    async with read_session() as s:
        return (await s.get(PayableInvoice, uuid.UUID(invoice_id)))  # type: ignore[return-value]


async def _balanced(entry_id: uuid.UUID) -> bool:
    async with read_session() as s:
        lines = (await s.execute(select(JournalLine).where(JournalLine.entry_id == entry_id))).scalars().all()
    return bool(lines) and sum(ln.debit_minor for ln in lines) == sum(ln.credit_minor for ln in lines)


@pytest.mark.parametrize("name", ["en_coffee", "bi_packaging", "ar_bakery", "bi_dairy"])
async def test_clean_invoices_post_balanced_entries_with_confidence_and_file(cafe: dict[str, Any], name: str) -> None:
    res = await _submit(cafe, name)
    assert res["outcome"] == "posted", res
    inv = await _invoice(res["invoice_id"])
    assert inv.status == "posted" and inv.journal_entry_id and await _balanced(inv.journal_entry_id)
    async with read_session() as s:
        doc = await s.get(Document, uuid.UUID(res["document_id"]))
        ext = (await s.execute(select(Extraction).where(Extraction.document_id == doc.id))).scalars().first()
    assert doc.file_id is not None and doc.status == "posted"
    assert all(0 <= v <= 1 for v in ext.fields["confidence"].values())


async def test_arabic_digits_are_normalised(cafe: dict[str, Any]) -> None:
    res = await _submit(cafe, "ar_bakery")
    inv = await _invoice(res["invoice_id"])
    assert inv.invoice_number == "GB-3302"
    assert inv.total == Money.from_decimal("1858.20", "EGP")
    async with read_session() as s:
        sup = await s.get(Supplier, inv.supplier_id)
    assert sup.name_en == "Golden Bakery"


async def test_arithmetic_mismatch_reextracts_once_then_asks(cafe: dict[str, Any]) -> None:
    res = await _submit(cafe, "ar_bakery_wrong_total")
    assert res["waiting_for_owner"] and res["issues"] == ["arithmetic"]
    async with read_session() as s:
        attempts = (await s.execute(select(Extraction.attempt).where(Extraction.document_id == uuid.UUID(res["document_id"])))).scalars().all()
    assert sorted(attempts) == [1, 2]
    q = await _question(res["document_id"])
    assert "EGP 1,150.00" in q.text_en and "EGP 1,140.00" in q.text_en
    assert {o["key"] for o in q.options} == {"use_printed", "use_calculated", "retake"}
    await _answer(cafe, res["document_id"], "use_calculated")
    inv = await _invoice(res["invoice_id"])
    assert inv.status == "posted" and inv.total == Money.from_decimal("1140", "EGP")


async def test_duplicate_by_number_and_by_amount_and_date(cafe: dict[str, Any]) -> None:
    first = await _submit(cafe, "bi_packaging")
    assert first["outcome"] == "posted"
    dup = await _submit(cafe, "bi_packaging_duplicate")  # same number, different photo
    assert dup["issues"] == ["duplicate"]
    await _answer(cafe, dup["document_id"], "discard")
    async with read_session() as s:
        doc = await s.get(Document, uuid.UUID(dup["document_id"]))
    assert doc.status == "duplicate"
    soft = await _submit(cafe, "bi_packaging_soft_duplicate")  # new number, same amount and date
    assert soft["issues"] == ["duplicate"]
    same_file = await _submit(cafe, "bi_packaging")
    assert same_file["outcome"] == "same_file_already_received"


async def test_unbalanced_entry_is_blocked() -> None:
    d = posting.Draft(START, "test", None, "bad").dr("5900", 1000).cr("2000", 900)
    with pytest.raises(posting.UnbalancedEntryError):
        posting.assert_balanced(d)


async def test_three_way_mismatch_is_held_then_posted_as_delivered(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    async with write_session() as s:
        milk = (await s.execute(select(Item).where(Item.business_id == bid, Item.name_en == "Milk"))).scalar_one()
        po = PurchaseOrder(business_id=bid, number="PO-9001", supplier_id=milk.preferred_supplier_id, status="partially_received",
                           total=Money.from_decimal("1800", "EGP"), expected_date=clock.today(), notes={}, sent_at=clock.clock_now())
        s.add(po)
        await s.flush()
        s.add(PurchaseOrderLine(business_id=bid, po_id=po.id, item_id=milk.id, qty=Decimal(40), unit="L",
                                unit_price=Money.from_decimal("45", "EGP"), line_total=Money.from_decimal("1800", "EGP")))
        dl = Delivery(business_id=bid, po_id=po.id, received_on=clock.today(), discrepancies=[])
        s.add(dl)
        await s.flush()
        s.add(DeliveryLine(business_id=bid, delivery_id=dl.id, item_id=milk.id, qty_received=Decimal(36),
                           unit_price_on_note=Money.from_decimal("45", "EGP")))
    res = await _submit(cafe, "bi_dairy_full_qty")
    assert res["issues"] == ["three_way"]
    inv = await _invoice(res["invoice_id"])
    assert inv.status == "held"
    await _answer(cafe, res["document_id"], "post_delivered")
    inv = await _invoice(res["invoice_id"])
    assert inv.status == "posted"
    assert Decimal(inv.lines[0]["qty"]) == 36
    assert inv.total == Money.from_decimal(str(Decimal(36) * 45 * Decimal("1.14")), "EGP")


async def test_bilingual_total_disagreement_asks(cafe: dict[str, Any]) -> None:
    res = await _submit(cafe, "bi_dairy_total_mismatch")
    assert "bilingual_conflict" in res["issues"]
    await _answer(cafe, res["document_id"], "use_primary")
    assert (await _invoice(res["invoice_id"])).status == "posted"


async def test_supplier_matched_from_arabic_name(cafe: dict[str, Any]) -> None:
    async with read_session() as s:
        m = await supplier_match.match(s, cafe["business_id"], ["مَزرعة النّور للألبان"], None)
        sup = await s.get(Supplier, m.supplier_id)
    assert sup.name_en == "Al Noor Dairy" and m.method in ("alias", "fuzzy")


async def test_ambiguous_date_is_corrected_with_the_other_format(cafe: dict[str, Any]) -> None:
    res = await _submit(cafe, "en_produce_ambiguous_date")  # printed 05/10: 5 Oct is in the future
    inv = await _invoice(res["invoice_id"])
    assert inv.invoice_date == date(2026, 5, 10)
    async with read_session() as s:
        inc = (await s.execute(select(Incident).where(Incident.type == "date_format"))).scalars().all()
    assert inc


async def test_three_corrections_open_one_incident_and_a_rule_proposal(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    async with read_session() as s:
        gp = (await s.execute(select(Supplier).where(Supplier.business_id == bid, Supplier.name_en == "Gulf Packaging"))).scalar_one()
    for i in range(3):
        await classification.record_correction(bid, gp.id, "5900", "5120", None, f"t{i}")
    await classification.record_correction(bid, gp.id, "5900", "5120", None, "t4")
    async with read_session() as s:
        incs = (await s.execute(select(Incident).where(Incident.type == "recurring_correction"))).scalars().all()
        rules = (await s.execute(select(LearnedRule).where(LearnedRule.kind == "classification"))).scalars().all()
    assert len(incs) == 1 and len(rules) == 1
    assert rules[0].status == "proposed" and "Gulf Packaging" in rules[0].rule_text_en
    assert rules[0].trigger == {"supplier_id": str(gp.id), "account_code": "5120"}


async def test_two_faults_on_one_invoice_both_reported_and_held_until_both_resolved(cafe: dict[str, Any]) -> None:
    assert (await _submit(cafe, "bi_packaging"))["outcome"] == "posted"
    res = await _submit(cafe, "bi_packaging_duplicate_wrong_total")
    assert set(res["issues"]) == {"arithmetic", "duplicate"}
    inv = await _invoice(res["invoice_id"])
    assert inv.status == "held" and set(inv.hold_reason) == {"arithmetic", "duplicate"}
    await _answer(cafe, res["document_id"], "use_calculated")
    inv = await _invoice(res["invoice_id"])
    assert inv.status == "held" and inv.hold_reason == ["duplicate"]
    await _answer(cafe, res["document_id"], "both_valid")
    assert (await _invoice(res["invoice_id"])).status == "posted"


async def test_bank_lines_are_matched_automatically_with_source_and_the_rest_listed(cafe: dict[str, Any]) -> None:
    bid = cafe["business_id"]
    await scheduler.advance(bid, days=3)
    async with read_session() as s:
        txns = (await s.execute(select(BankTransaction).where(BankTransaction.business_id == bid,
                                                              BankTransaction.date >= START))).scalars().all()
        entries = (await s.execute(select(JournalEntry).where(JournalEntry.business_id == bid,
                                                              JournalEntry.reference_type == "sales_summary"))).scalars().all()
    auto = [t for t in txns if t.match_status == "auto_matched"]
    assert len(entries) == 3
    assert auto and all(t.match_source == "auto" and t.match_confidence >= 0.9 for t in auto)
    assert len(auto) / len(txns) >= 0.6
    for e in entries:
        assert await _balanced(e.id)
    _ = timedelta
