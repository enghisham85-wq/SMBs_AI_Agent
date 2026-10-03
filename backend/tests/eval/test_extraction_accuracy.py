"""Live extraction accuracy, spends API credit. Run with LLM_MODE=live and -m llm_live."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.agents.accountant import extraction
from app.llm.client import LLMClient, set_llm
from app.seed.invoices.generate import generate

pytestmark = pytest.mark.llm_live
FIELDS = ("invoice_number", "invoice_date", "subtotal", "vat_amount", "total", "supplier_vat_number")


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a == b
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except Exception:
        return str(a).strip().upper() == str(b).strip().upper()


def _score(ext: Any, truth: dict[str, Any]) -> tuple[int, int]:
    ok = total = 0
    for f in FIELDS:
        t = truth.get(f)
        if t is None:
            continue
        got = getattr(ext, f)
        total += 1
        ok += int(got is not None and _same(got.value, t["value"]))
    for gl, tl in zip(ext.lines, truth["lines"], strict=False):
        for f in ("qty", "unit_price", "line_total"):
            total += 1
            ok += int(_same(getattr(gl, f).value, tl[f]["value"]))
    total += max(0, len(truth["lines"]) - len(ext.lines)) * 3
    return ok, total


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
async def test_field_accuracy_per_language_group(tmp_path: Path) -> None:
    set_llm(LLMClient(mode="live"))
    files = generate(tmp_path, date(2026, 10, 3))
    by_group: dict[str, list[int]] = {"en": [0, 0], "bilingual": [0, 0], "ar": [0, 0]}
    for f in files:
        if f["expected"]:
            continue  # faulty variants are not accuracy samples
        data = (tmp_path / f["file"]).read_bytes()
        truth = json.loads((tmp_path / f"{f['name']}.truth.json").read_text(encoding="utf-8"))
        ext, source = await extraction.extract(data, f["mime"], hashlib.sha256(data).hexdigest(), [], 1)
        assert source == "llm"
        ok, total = _score(ext, truth)
        by_group[f["group"]][0] += ok
        by_group[f["group"]][1] += total
    report = {g: round(ok / total, 3) if total else None for g, (ok, total) in by_group.items()}
    print("field accuracy by group:", report)
    for group, acc in report.items():
        assert acc is not None and acc >= 0.90, f"{group}: {acc}"
