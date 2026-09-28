# Implementation Plan: Small Business Agent Suite

**Branch**: `001-small-business-agents` | **Date**: 2026-09-28 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-small-business-agents/spec.md`

**Note**: This template is filled in by the `/speckit-plan` command; its definition describes the execution workflow.

## Summary

Build a hackathon-MVP web application in which three assistants (Stock, Cash-Flow, Accountant) share one database and run every action through a common harness pipeline (plan → precondition check → risk class → independent verifier → execute → read-back verify → rollback/retry/escalate → audit log), with incidents, owner-approved learned rules and self-calibration. The owner interacts through a React dashboard (with an in-app chat panel) and a Telegram bot, both backed by one approval service. A presenter-controlled business clock drives daily jobs so a demo can cover weeks in minutes, and a Chaos panel injects the 8 fault scenarios.

Technical approach (from [research.md](./research.md)): Python 3.12 + FastAPI backend, **LangGraph** as the agent runtime (the harness lifecycle, every agent workflow and the daily run are LangGraph state graphs; owner approvals are checkpointed `interrupt()` pauses resumed by the approval service — research R17), SQLite via SQLAlchemy, deterministic code for all maths and checks, and Claude (`claude-opus-5`, structured outputs) only for reading invoices (English, bilingual, Arabic), independent verification, expense classification, message wording and rule proposals.

## Technical Context

**Language/Version**: Python 3.12 (backend); TypeScript 5 / React 18 (frontend)

**Primary Dependencies**: LangGraph 1.x + `langgraph-checkpoint-sqlite` (agent orchestration, required), FastAPI, Uvicorn, Pydantic v2, SQLAlchemy 2 + Alembic, `anthropic` SDK, `python-telegram-bot` v21, `statsmodels` + `pandas` (forecasting), `hijridate` (Ramadan), `argon2-cffi`; Vite, TanStack Query, Recharts, Tailwind CSS, i18next

**Storage**: SQLite (WAL) for demo, PostgreSQL-compatible schema; LangGraph checkpoints in the same database via `AsyncSqliteSaver` (swap to the Postgres saver with the schema); uploaded documents on local disk under `backend/var/files` (sha256-named)

**Testing**: pytest (+ pytest-asyncio, httpx, schemathesis), graphs compiled with `InMemorySaver` and interrupts resumed via `Command(resume=...)`, LLM record/replay fixtures, separate live eval suite; Vitest + Testing Library; Playwright e2e

**Target Platform**: Single Linux or Windows host running the API, scheduler and Telegram poller in one process; modern mobile and desktop browsers

**Project Type**: Web application (backend API + SPA frontend) with a chat-bot channel

**Performance Goals**: dashboard views < 2 s; one simulated day of jobs < 10 s excluding LLM calls; invoice extraction < 30 s; any Chaos scenario end to end < 2 min (SC-011)

**Constraints**: no agent moves money (FR-047); no bank credentials stored (FR-048); money in integer minor units with 3-decimal OMR (FR-051); all "now" comes from the business clock (FR-012a); demo must run offline from the LLM using replay mode

**Scale/Scope**: 1 business, ≤ 50 users, 20 seeded items (≤ 500 supported), 5 suppliers, 3 months of sales (≤ 100k lines), 8 Chaos scenarios, ~6 dashboard views

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is still the unfilled template — no project principles or gates are ratified. **Result: PASS (no gates to evaluate).**

In their absence this plan holds itself to the spec's own non-negotiables, re-checked after design:

| Guardrail (from spec) | Design evidence | Status |
|---|---|---|
| No money movement (FR-047) | No payment-execution code path or bank write API; payments exist only as recommendations/schedules | ✅ |
| Every action logged and verified (FR-001, FR-009) | Single LangGraph `harness_graph` in `harness/`; every node update streamed and written to the audit log | ✅ |
| Irreversible actions need approval (FR-002) | `approval_gate` node calls `interrupt()` for `irreversible_external` actions; `execute` is unreachable without a resume from `ApprovalService` or a matching auto-approve rule | ✅ |
| LangGraph is the agent runtime (user requirement) | All agent workflows, harness, daily run and conflict handling are compiled `StateGraph`s (R17) | ✅ |
| Verifier independence (FR-003) | `VerificationPacket` excludes primary reasoning ([llm-outputs.md](./contracts/llm-outputs.md)) | ✅ |
| Cross-agent writes only via events (FR-044) | Outbox events ([events.md](./contracts/events.md)); agents own their tables | ✅ |
| Role enforcement on every channel (FR-049) | Role dependency on every endpoint + inside `ApprovalService.resolve` (shared by Telegram) | ✅ |

Recommend running `/speckit-constitution` before `/speckit-tasks` if you want these adopted as formal project principles.

## Project Structure

### Documentation (this feature)

```text
specs/001-small-business-agents/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── rest-api.md      # Dashboard REST API
│   ├── events.md        # Inter-agent events
│   ├── telegram-bot.md  # Telegram channel
│   └── llm-outputs.md   # LLM structured-output schemas
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 output (/speckit-tasks - NOT created here)
```

### Source Code (repository root)

```text
backend/
├── pyproject.toml
├── alembic/
├── app/
│   ├── main.py                 # FastAPI app, lifespan starts scheduler + Telegram poller
│   ├── config.py
│   ├── db/                     # engine, session, base, money & quantity types
│   ├── models/                 # SQLAlchemy models per data-model.md section
│   ├── core/
│   │   ├── clock.py            # BusinessClock (real / simulated)
│   │   ├── scheduler.py        # advance loop: invokes daily_run_graph per skipped day
│   │   ├── events.py           # outbox + dispatcher (handlers start graph runs)
│   │   ├── auth.py             # sessions, roles, permission dependency
│   │   └── i18n.py             # en/ar strings, digit normalisation
│   ├── graphs/
│   │   ├── runtime.py          # checkpointer (AsyncSqliteSaver), graph registry, run/resume helpers
│   │   ├── daily_run.py        # daily_run_graph: ordered agent subgraphs for one business date
│   │   ├── conflict.py         # conflict_graph: both positions + recommendation → interrupt
│   │   └── streaming.py        # astream(stream_mode="updates") → SSE + audit log
│   ├── harness/
│   │   ├── state.py            # ActionState TypedDict (plan, checks, risk, verdict, attempt, result)
│   │   ├── action_spec.py      # ActionSpec: plan/preconditions/execute/verify/compensate callables
│   │   ├── graph.py            # harness_graph: plan→precheck→classify_risk→premortem_verify→approval_gate→execute→post_verify→rollback/retry/escalate→finalize
│   │   ├── verifier.py         # independent verifier node
│   │   ├── confidence.py       # thresholds, three-band routing
│   │   ├── calibration.py      # error tracking, degrade/restore
│   │   ├── incidents.py
│   │   ├── rules.py            # learned rules: propose/approve/apply
│   │   └── audit.py
│   ├── approvals/
│   │   ├── service.py          # create/resolve/timeout, first-answer-wins, resumes graph thread via Command(resume=...)
│   │   └── telegram_bot.py
│   ├── llm/
│   │   ├── client.py           # anthropic wrapper, live/record/replay
│   │   └── prompts/            # system prompts per role
│   ├── agents/                 # each agent: graphs.py (LangGraph subgraphs) + action_specs.py + plain-function domain logic
│   │   ├── stock/              # graphs: stock_update, forecast_check, reorder, delivery; logic: forecasting, reorder, checks
│   │   ├── cashflow/           # graphs: cash_forecast, shortfall_plan, reminders, budget; logic: projection, scenarios, checks
│   │   └── accountant/         # graphs: document (extract→checks→re-extract→post), reconciliation, trial_balance; logic: posting, matching, vat
│   ├── chaos/                  # 8 scenario injectors
│   ├── api/v1/                 # routers per contracts/rest-api.md
│   └── seed/                   # sample cafe dataset, invoice samples, feed.py (daily simulated sales + bank data)
└── tests/
    ├── unit/
    ├── contract/
    ├── integration/            # chaos, milk_e2e, roles, sample_metrics
    ├── eval/                   # live LLM accuracy (SC-008)
    └── fixtures/llm/           # recorded responses

frontend/
├── package.json
├── src/
│   ├── api/                    # typed client generated from OpenAPI
│   ├── components/             # HealthStrip, ApprovalCard, ChatPanel, PipelineView, charts
│   ├── pages/                  # Home, Stock, Cash, Books, Harness, Chaos, Login, Settings
│   ├── i18n/                   # en, ar (RTL)
│   └── hooks/
└── tests/
    ├── unit/
    └── e2e/                    # Playwright
```

**Structure Decision**: Two projects — `backend/` (FastAPI API, LangGraph runtime, agents, harness and Telegram poller in one process) and `frontend/` (React SPA). Agents are packages inside the backend rather than separate services, because they share one database and the event bus is in-process (research R11). Every agent workflow is a LangGraph subgraph, and every state-changing step runs through the shared `harness_graph` — the only path by which agent actions execute (R17). Domain maths lives in plain functions that nodes call, so it stays unit-testable without a graph.

## Complexity Tracking

No constitution violations to justify (constitution not yet ratified).
