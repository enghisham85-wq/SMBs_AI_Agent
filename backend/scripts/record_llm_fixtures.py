"""Record LLM replay fixtures for every demo path.

    cd backend
    LLM_MODE=record ANTHROPIC_API_KEY=... uv run python -m scripts.record_llm_fixtures

Runs against a throwaway database in var/record/: seeds the sample cafe, then drives the demo paths
(stock orders, every sample invoice, the cash forecast, pending approvals, every Chaos scenario and
the VAT summary) so every Claude call they make is saved to tests/fixtures/llm/ (one JSON file per
request hash). Replay (LLM_MODE=replay) then serves those answers without the API. `--mode offline`
runs the same script with the deterministic stand-ins, to check the script itself without credit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
START = date(2026, 10, 4)


async def _record(mode: str) -> dict[str, Any]:
    from sqlalchemy import select

    from app import wiring
    from app.agents.accountant import vat
    from app.agents.accountant.intake import submit
    from app.approvals import service as approvals
    from app.chaos import service as chaos
    from app.config import get_settings
    from app.core import clock, scheduler
    from app.db.engine import init_engine, read_session
    from app.db.migrate import upgrade_head
    from app.graphs import runtime
    from app.llm.client import LLMClient, set_llm
    from app.models.harness import ApprovalRequest
    from app.models.tenancy import User
    from app.seed.sample_cafe import seed

    work = ROOT / "var" / "record"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    s = get_settings()
    s.DATABASE_URL = f"sqlite+aiosqlite:///{(work / 'app.db').as_posix()}"
    s.CHECKPOINT_DB_PATH = (work / "checkpoints.db").as_posix()
    s.FILES_DIR = (work / "files").as_posix()
    s.SAMPLE_INVOICES_DIR = (work / "samples").as_posix()
    s.DEMO_MODE = True
    fixtures = ROOT / "tests" / "fixtures" / "llm"
    before = set(fixtures.glob("*.json")) if fixtures.exists() else set()
    set_llm(LLMClient(mode=mode, fixtures_dir=str(fixtures)))

    init_engine()
    await upgrade_head()
    await runtime.init()
    wiring.register_all()
    info = await seed(start_date=START, history_days=42)
    bid = info["business_id"]
    async with read_session() as ss:
        owner = (await ss.execute(select(User).where(User.business_id == bid, User.role == "owner"))).scalar_one()
    steps: dict[str, Any] = {}

    # Stock: advance to the next Tuesday; purchase orders go through the independent check.
    await scheduler.advance(bid, days=2)
    steps["stock_days"] = clock.today().isoformat()
    # Books: every sample invoice (English, bilingual, Arabic-only and the faulty variants).
    files = json.loads((work / "samples" / "files.json").read_text(encoding="utf-8"))
    outcomes = {}
    for f in files:
        res = await submit(bid, (work / "samples" / f["file"]).read_bytes(), f["mime"], f["file"], "dashboard", None)
        outcomes[f["name"]] = res.get("outcome") or res.get("issues")
    steps["invoices"] = outcomes
    # Cash: advance through the week before rent and salaries (forecast, shortfall plan).
    await scheduler.advance(bid, days=5)
    # Harness: answer what is waiting, so escalations and incident analysis run.
    async with read_session() as ss:
        pending = (await ss.execute(select(ApprovalRequest).where(ApprovalRequest.business_id == bid,
                                                                  ApprovalRequest.status == "pending",
                                                                  ApprovalRequest.kind != "alert"))).scalars().all()
    for req in pending:
        keys = [o["key"] for o in req.options]
        await approvals.resolve(str(req.id), "approve" if "approve" in keys else (req.safe_default or keys[0]),
                                owner, "dashboard")  # type: ignore[arg-type]
    # Chaos: every scenario (incident analysis and rule proposals).
    steps["chaos"] = {}
    for sc in chaos.scenarios():
        row = await chaos.inject(bid, sc["key"], None, owner.id)
        steps["chaos"][sc["key"]] = {"status": row.status, "detected": row.detected, "rule": bool(row.rule_id)}
    # VAT summary (tax figures go through the independent check).
    steps["vat"] = (await vat.summary_with_status(bid, vat.period_for(clock.today(), "monthly")))["status"]
    # Roles and timeouts make no model calls of their own; one more day runs the timeouts.
    await scheduler.advance(bid, days=1)
    await runtime.close()
    after = set(fixtures.glob("*.json")) if fixtures.exists() else set()
    steps["new_fixtures"] = len(after - before)
    return steps


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--mode", choices=["record", "offline"], default=os.environ.get("LLM_MODE", "record"))
    args = p.parse_args()
    if args.mode == "record" and not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_PROFILE")):
        raise SystemExit("Set ANTHROPIC_API_KEY (or an `ant auth login` profile) to record, or use --mode offline.")
    print(json.dumps(asyncio.run(_record(args.mode)), indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
