# Research: Small Business Agent Suite

**Feature**: `001-small-business-agents` | **Date**: 2026-09-28 | **Spec**: [spec.md](./spec.md)

The repository is empty (greenfield), so every technical choice below was made in this phase. The spec left no `NEEDS CLARIFICATION` markers; the unknowns resolved here are the Technical Context items in [plan.md](./plan.md).

---

## R1. Backend language and web framework

- **Decision**: Python 3.12 with FastAPI (ASGI, served by Uvicorn), Pydantic v2 models.
- **Rationale**: Strongest ecosystem for the numeric work (forecasting, time series), first-party Anthropic Python SDK with typed structured-output parsing, and first-class async for the Telegram bot and long LLM calls. Pydantic models double as API schemas and LLM output schemas.
- **Alternatives considered**: TypeScript/Node (good SDK, weaker forecasting libraries); Django (heavier, sync-first ORM); Go (fast, but slow to iterate for a hackathon).

## R2. Frontend

- **Decision**: React 18 + TypeScript + Vite; TanStack Query for server state; Recharts for charts; Tailwind CSS; i18next with RTL support prepared (Arabic UI is P3).
- **Rationale**: Mobile-friendly SPA that can show live harness pipeline updates via Server-Sent Events. Tailwind's logical properties make the later RTL switch cheap.
- **Alternatives considered**: Server-rendered templates + HTMX (less suitable for live charts and the chat panel); Next.js (SSR not needed, adds deployment complexity).

## R3. Storage and money representation

- **Decision**: SQLAlchemy 2.0 ORM + Alembic migrations. SQLite (WAL mode) for the demo; schema kept PostgreSQL-compatible. All money stored as integer **minor units** (baisa for OMR, 1/1000) with a currency code; quantities stored as `Decimal` with an explicit unit.
- **Concurrency**: API requests, Telegram callbacks, event handlers and the daily run share one process. Every SQLite connection sets `busy_timeout` (5 s), and all commits go through one process-wide `asyncio.Lock` (`write_session()`), so writes are serialised instead of failing with "database is locked". LangGraph checkpoints use a separate SQLite file. The lock is skipped on PostgreSQL.
- **Rationale**: One file database makes the demo reset (`seed` → known state) instant and reliable; integer minor units remove floating-point rounding errors that would break balanced-entry and arithmetic checks (FR-033, FR-035). 3-decimal OMR and 2-decimal AED/SAR handled by per-currency exponent (FR-051).
- **Alternatives considered**: PostgreSQL from day one (extra setup for judges); floats (unacceptable for accounting); `Decimal` columns for money (works, but integer minor units are simpler to sum and compare exactly).

## R4. LLM provider, models and call patterns

- **Decision**: Anthropic Claude via the official `anthropic` Python SDK. Model `claude-opus-5` for all LLM roles, with `output_config.effort` tuned per role:
  | Role | Input | Output | Effort |
  |---|---|---|---|
  | Invoice extraction (Accountant) | Image or PDF document block + instructions | Structured JSON (Pydantic schema) with per-field confidence | `high` |
  | Independent verifier (Harness) | Inputs + proposed output only (never primary reasoning) | Structured verdict: agree/disagree + issues | `high` |
  | Expense classification (Accountant) | Transaction text + chart of accounts + learned rules | Account code + confidence | `low` |
  | Owner-message wording, incident root-cause summary, learned-rule proposal | Structured incident/action facts | Short bilingual text / rule JSON | `medium` |
  - Structured outputs via `client.messages.parse()` with Pydantic output models (no prefill; prefill is rejected on current models).
  - Adaptive thinking (default on for `claude-opus-5`); streaming not required because outputs are small (`max_tokens` ≈ 4–8K).
  - Server-side refusal fallbacks enabled (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`) and `stop_reason` checked before reading content.
  - Prompt caching: stable system prompt + schema + chart of accounts + active learned rules placed first with a cache breakpoint; the document/transaction goes last.
- **Rationale**: One model keeps one prompt-cache namespace and one behaviour profile to evaluate; effort is the cost lever instead of model downgrades. PDFs and images are native input, including Arabic text, so no separate OCR stage is needed.
- **Deterministic by design**: forecasting, reorder maths, cash projection, arithmetic/duplicate/balance checks, bank-match scoring and the harness pipeline are ordinary code, not LLM calls. The LLM is used only where reading or wording is needed. This keeps SC-001/SC-007 testable and repeatable.
- **Alternatives considered**: Separate OCR engine + LLM (extra moving part, weaker on bilingual layout); a cheaper model for classification (possible later, only if the owner of the project chooses it; not a default).

## R5. Verifier independence

- **Decision**: The verifier is a separate API call with its own system prompt, receiving a `VerificationPacket` (source inputs + proposed output + applicable rules). It never receives the primary call's thinking, message history or rationale. Disagreement → escalation incident (FR-003). Deterministic checks run before the verifier so it only judges what code cannot.
- **Rationale**: Matches the spec requirement that the verifier "cannot simply agree"; also saves cost by skipping it for outputs that already failed a hard check.

## R6. Extraction confidence

- **Decision**: Field confidence = model-reported confidence (0–1, requested in the schema) × penalty factors from deterministic checks (arithmetic mismatch, date sanity, unknown supplier, bilingual disagreement). Document confidence = minimum of required-field confidences. Thresholds per agent stored in `AgentCalibration` (default high 0.90, low 0.60).
- **Rationale**: Model self-reports alone are over-confident; tying them to checks makes the three-band routing (FR-005) meaningful and lets self-calibration (FR-006) move thresholds.

## R7. Arabic and bilingual invoices

- **Decision**: Send the original image/PDF to the model, asking for values in canonical form (ISO dates, Western digits, decimal point) *plus* the raw text as printed. Server-side post-processing: normalise Arabic-Indic (٠-٩) and Eastern Arabic-Indic (۰-۹) digits and the Arabic decimal/thousands separators (٫ ٬); compare raw vs canonical; for bilingual invoices extract both language versions of name/total and raise a conflict if they disagree. Supplier matching uses a `SupplierAlias` table (Arabic and English names, VAT number) with normalised comparison (strip diacritics/tatweel, unify alef/ya/ta-marbuta forms).
- **Rationale**: Satisfies FR-032 and the bilingual-disagreement edge case; VAT number is the strongest cross-language key.
- **Test data**: Generate the sample invoice set (English, bilingual, Arabic-only; clean and faulty variants) as rendered PDFs + photographed-style images with known ground truth, so SC-008 is measured per language group.

## R8. Demand forecasting

- **Decision**: Per item daily forecast for 14–30 days with two methods:
  1. **Primary**: Holt-Winters exponential smoothing with weekly seasonality (`statsmodels`), multiplied by calendar-event uplift factors (weekend, public holiday, Ramadan evening) learned from history; range = expected ± residual quantiles (P10/P90).
  2. **Safe fallback**: average of the last 4 same weekdays (spec FR-020), range = min/max of those 4.
  Switch to fallback when rolling 7-day MAPE exceeds the item's threshold; restore gradually (FR-006).
- **Rationale**: Explainable, fast on 20 items × 3 months, and the fallback is exactly the method named in the spec.
- **Calendar**: Oman defaults — Friday–Saturday weekend, public-holiday table seeded for the demo year, Ramadan dates computed with `hijridate` (Umm al-Qura).
- **Alternatives considered**: Prophet (heavy dependency), LLM-based forecasting (non-deterministic, untestable).

## R9. Cash forecasting

- **Decision**: Deterministic daily ledger projection for 30 days: opening balance + scheduled inflows (sales forecast × payment-method settlement lag, receivables by due date × customer on-time probability) − outflows (payables per recommended schedule, open/planned POs, obligations by recurrence). Three scenarios by applying P10/P50/P90 sales and pessimistic/expected receivable delays. Action plan = greedy search over candidate actions ranked by (gap closed ÷ risk score), each re-simulated to show the post-action forecast (FR-026).
- **Rationale**: Transparent arithmetic the owner can follow; satisfies SC-004 (≥14 days warning) because the projection horizon is 30 days.

## R10. Business clock and scheduling

- **Decision**: A `BusinessClock` service is the only source of "now". In demo mode it stores a simulated datetime in the DB; `advance(days | to_date)` iterates day by day and, for each day, invokes the LangGraph `daily_run_graph` (R17), whose nodes run in fixed order (import day's sales → stock deduction → morning forecast check → reorder planning → bank import → balance comparison → cash forecast → trial balance → reminders → approval timeouts). Outside demo mode an asyncio loop triggers the same job list once per real day.
- **Rationale**: FR-012a requires in-order catch-up; a single job list for both modes avoids two code paths.
- **Demo data feed**: In demo mode, the `import_sales` and `bank_import` steps are fed by a deterministic generator (`seed/feed.py`) that produces each new business date's sales and bank transactions from the same seasonal model as the 3-month history. The random seed comes from business and date, so a replay gives the same data, and re-running a date is a no-op. Chaos scenarios change future days through stored per-date overrides (demand spike, missing bank day, extra outflow) instead of editing records directly. Real mode uses uploads and imports instead.
- **Alternatives considered**: APScheduler (hard to drive from a simulated clock), freezegun in production code (test-only tool). LangGraph nodes must read time only from `BusinessClock`, never the system clock.

## R11. Inter-agent events

- **Decision**: In-process event bus backed by a persisted `Event` table (transactional outbox): the publishing action writes the event in the same DB transaction; subscribers are dispatched after commit and record their handling in `EventDelivery`. Handlers are idempotent (keyed by event id).
- **Rationale**: Every cross-agent change is logged (FR-044) and replayable; no broker needed for a single-process demo.
- **Alternatives considered**: Redis/RabbitMQ (extra infrastructure, no benefit at this scale).

## R12. Harness action pipeline, rollback

- **Decision**: Implemented as the LangGraph `harness_graph` (see R17). Each agent capability registers an `ActionSpec` supplying the node callables `plan()`, `preconditions()`, `risk_class`, `execute(dry_run)`, `verify()`, `compensate()`. Reversible writes execute inside a DB savepoint and are rolled back on failed verification; external actions (send PO/reminder) are only emitted after approval and verified by delivery receipt. Retry once with corrected input (from the verifier's or check's issues), then escalate. Every stage writes an `AuditLogEntry` and streams a pipeline update to the dashboard via SSE.
- **Rationale**: Uniform lifecycle makes FR-001–FR-009 enforceable in one place and makes Chaos mode visual.

## R13. Telegram + in-dashboard chat

- **Decision**: `python-telegram-bot` v21 (async) using long polling in the demo (no public webhook needed). Approval requests rendered as inline keyboards; callbacks carry an opaque request token. A single `ApprovalService` owns request state; both channels call `resolve(request_id, option, user)` which uses an atomic conditional update (`status = pending`) so the first answer wins (FR-010, edge case), then resumes the paused LangGraph thread with `Command(resume=...)` exactly once (R17). Users link Telegram by sending a one-time code shown in their dashboard profile (FR-049a). If Telegram sending fails, the failure is logged and the request stays open in the dashboard (FR-010a).
- **Alternatives considered**: WhatsApp Business API (account approval lead time — rejected in clarification); webhooks (need public HTTPS during demo).

## R14. Authentication and roles

- **Decision**: Username + password (argon2 hash), server-side session in an HTTP-only, SameSite=Strict cookie; CSRF token for state-changing requests. Role check via a FastAPI dependency on every endpoint and in `ApprovalService.resolve` (so Telegram answers follow the same rules). Refusals written to the audit log (FR-049a, SC-012). Single tenant with `business_id` on every row to keep isolation explicit (FR-050).
- **Alternatives considered**: JWT bearer tokens (revocation harder, no benefit here); external IdP (overkill for a demo).

## R15. Testing strategy

- **Decision**:
  - `pytest` unit tests for all deterministic logic (forecasts, reorder, cash projection, checks, matching, normalisation).
  - Contract tests for the REST API (schemathesis against the OpenAPI document) and for event payloads.
  - Integration tests per Chaos scenario (8) driving the business clock — these are the acceptance tests for SC-001.
  - LLM calls behind a `LLMClient` interface; tests use recorded responses (record/replay fixtures keyed by request hash). A small, separately-run evaluation suite hits the live API for SC-008 accuracy per language group.
  - Frontend: Vitest + Testing Library; Playwright for the end-to-end milk scenario and role tests (SC-012).
- **Demo reliability**: The same record/replay cache can be switched on in demo mode so a live presentation does not depend on network latency.

## R17. Agent orchestration framework — LangGraph (project requirement)

- **Decision**: Build all agent workflows on **LangGraph 1.x** (`langgraph`, `langgraph-checkpoint-sqlite`). LangGraph is the runtime for the harness lifecycle, each agent's workflows, the daily run, and human-in-the-loop pauses. Mapping:
  | Spec concept | LangGraph construct |
  |---|---|
  | Harness action lifecycle (FR-001) | `harness_graph`: a `StateGraph` with nodes `plan → precheck → classify_risk → premortem_verify → approval_gate → execute → post_verify → finalize`, conditional edges `post_verify → rollback → retry` (max 1) `→ escalate`. Each agent capability plugs in its own node callables via an `ActionSpec`; the graph shape is shared. |
  | Owner approval / question (FR-002, FR-010, FR-011) | `interrupt()` inside `approval_gate`; state persisted by the checkpointer; `ApprovalService.resolve()` resumes the thread with `Command(resume={option_key, user})`. Timeouts resume with the safe-default option. |
  | Durable, resumable actions | `AsyncSqliteSaver` checkpointer in its own SQLite file (`var/checkpoints.db`) so its writes don't compete with business-data writes; `thread_id = action.id`. A server restart mid-approval resumes cleanly. |
  | Stock / Cash-Flow / Accountant agents | One compiled subgraph per workflow, e.g. `stock.reorder_graph` (forecast → days-of-cover → quantities → group by supplier → spawn `draft_po` actions), `accountant.document_graph` (extract → normalise → checks → re-extract once → classify → post), `cashflow.forecast_graph` (project → detect shortfall → build plan → simulate actions). |
  | Daily scheduler (FR-012a) | `daily_run_graph`: fixed sequence of agent subgraph nodes for one business date; `BusinessClock.advance` invokes it once per skipped day, in date order. |
  | Cross-agent conflict (FR-043) | `conflict_graph`: gathers both agents' positions, computes recommendation, interrupts for the owner. |
  | Live pipeline view (Harness dashboard) | `graph.astream(..., stream_mode="updates")` forwarded to the SSE stream; each node update also writes an `AuditLogEntry`. |
  | Chaos mode | Injectors mutate data, then run the normal graphs — no special paths, so detection is genuine. |
- **What stays outside LangGraph**: inter-agent events remain the transactional outbox (R11) — graphs publish events from nodes and event handlers start new graph runs. This keeps cross-agent writes logged and decoupled (FR-044). Deterministic maths (forecasting, cash projection, checks) are plain functions called from nodes.
- **LLM calls inside nodes**: nodes call Claude through the project's `LLMClient` wrapper around the official `anthropic` SDK (R4), not through a LangChain chat-model wrapper, so structured outputs, refusal fallbacks, prompt caching and record/replay fixtures work unchanged. LangGraph does not require LangChain models.
- **Testing**: graphs compiled with an in-memory checkpointer (`InMemorySaver`) in unit tests; interrupts tested by asserting the `__interrupt__` payload and resuming with `Command(resume=...)`. Optional LangSmith tracing only if an API key is configured (off by default; no business data leaves the tenant otherwise).
- **Rationale**: The user requires LangGraph. It also fits the harness naturally: explicit nodes and conditional edges mirror the lifecycle, and checkpointed `interrupt()` is exactly the human-in-the-loop pause the spec needs.
- **Alternatives considered**: hand-written pipeline classes (the original R12 design — superseded); LangChain `AgentExecutor`/ReAct agents (open-ended tool loops are harder to verify and would weaken the deterministic checks); Claude Managed Agents (hosted loop, but approval pauses and local data would be harder to control for this demo).

## R16. Performance and scale

- **Decision**: Targets for a single-business demo: dashboard views load < 2 s; a clock advance of one day completes < 10 s excluding LLM calls; invoice extraction < 30 s per document; Chaos scenario end to end < 2 min (SC-011). Scale: 1 business, ≤ 50 users, ≤ 500 items, ≤ 100k sales lines.
