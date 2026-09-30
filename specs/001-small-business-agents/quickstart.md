# Quickstart & Validation Guide: Small Business Agent Suite

**Feature**: `001-small-business-agents` | See [plan.md](./plan.md), [data-model.md](./data-model.md), [contracts/](./contracts/)

This guide proves the feature works end to end. Commands assume the layout in plan.md (`backend/`, `frontend/`) and Windows PowerShell or any POSIX shell.

## Prerequisites

- Python 3.12, Node.js 20+, `uv` (Python package manager)
- An Anthropic API credential (`ANTHROPIC_API_KEY`, or an `ant auth login` profile) — only needed for live extraction; tests and replay-mode demo run without it
- A Telegram bot token from @BotFather (`TELEGRAM_BOT_TOKEN`) — optional; the dashboard chat works without it

## Setup

```powershell
cd backend;  uv sync;  uv run alembic upgrade head;  uv run python -m app.seed --sample-cafe
cd ../frontend;  npm install
```

Environment (`backend/.env`): `DEMO_MODE=true`, `LLM_MODE=replay|live|record`, `TELEGRAM_BOT_TOKEN=...` (optional).

## Run

```powershell
cd backend;  uv run uvicorn app.main:app --reload        # API + scheduler + Telegram polling
cd frontend; npm run dev                                   # dashboard at http://localhost:5173
```

Seeded users: `owner / manager / staff` (demo passwords printed by the seed command).

## Automated validation

| Command | Proves |
|---|---|
| `cd backend; uv run pytest tests/unit` | forecasting, reorder, cash projection, checks, normalisation, matching |
| `uv run pytest tests/unit/graphs` | each LangGraph graph compiles; harness routes (retry once → escalate), `interrupt()` payloads and `Command(resume=...)` resumption, checkpoint survives restart |
| `uv run pytest tests/contract` | REST API matches OpenAPI ([rest-api.md](./contracts/rest-api.md)); event payloads match [events.md](./contracts/events.md) |
| `uv run pytest tests/integration -k chaos` | all 8 Chaos scenarios detected, explained, corrected, rule proposed (SC-001) |
| `uv run pytest tests/integration -k milk_e2e` | cross-agent milk scenario (User Story 5) |
| `uv run pytest tests/integration -k roles` | role refusals are enforced and logged (SC-012) |
| `uv run pytest -m slow tests/integration/test_sample_metrics.py` | SC-002, SC-003, SC-004, SC-006, SC-009 on a 3-month replay against a no-assistant baseline; writes `backend/var/reports/sample_metrics.json` (about 5 minutes; opt-in) |
| `LLM_MODE=live uv run pytest -m llm_live tests/eval` | SC-008 extraction accuracy per language group (needs `ANTHROPIC_API_KEY`; spends API credit) |
| `cd frontend; npm test; npx playwright test` | UI units; e2e approval flow, dashboard views, roles (the e2e run seeds its own cafe in `backend/var/e2e` on ports 8765/5175; set `PW_CHANNEL=chrome` to use an installed browser when Playwright's cannot be downloaded) |

## Manual demo walkthrough (expected outcomes)

1. **Log in as owner** → Home shows health strip, today's decisions, alerts; every figure shows "as of …" (FR-046).
2. **Stock (US1)** → Clock: *Advance to next Tuesday*. Expect: "Milk will run out Thursday evening… [Approve] [Edit] [Reject]" in the chat panel and in Telegram. Approve in Telegram → the dashboard copy shows "Already answered in Telegram by owner".
3. **Books (US2)** → In the Books inbox pick the sample *bi_packaging* (Gulf Packaging, bilingual; the file is `backend/var/sample_invoices/bi_packaging.pdf`). Expect fields with per-field confidence, Arabic digits normalised, posted journal entry that balances. Upload it again → held as duplicate with a one-tap question.
4. **Cash (US3)** → Cash view: 30-day chart with buffer line; lowest point and date. Advance to the week before rent + salaries → shortfall warning ≥ 14 days ahead with ranked actions; *Simulate* each to see the new lowest point.
5. **Harness (US4)** → Harness view shows each action's LangGraph nodes live (plan → precheck → … → finalize); restart the backend while an approval is pending, then approve — the action resumes from its checkpoint; open an action to see plan, checks, verifier verdict, audit trail.
6. **Chaos (US6)** → Inject each scenario; for each, confirm in < 2 min: incident opened with detection method → explanation to owner → correction/rollback → proposed learned rule. Approve the date-format rule; re-inject scenario 3 → rule applied automatically, no question asked.
7. **Roles** → Log in as staff: can record a delivery and waste; money figures hidden; attempting `/cash/forecast` returns 403 and appears in the audit log. Log in as manager: can approve a PO; cannot approve a learned rule.
8. **Timeout** → Leave an approval unanswered and advance the clock past its deadline → nothing irreversible happens; request re-sent with higher urgency.

## Reset

`POST /api/v1/demo/reset` (owner) or `uv run python -m app.seed --sample-cafe --reset` restores the known starting state.
