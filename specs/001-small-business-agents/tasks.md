---

description: "Task list for the Small Business Agent Suite (hackathon MVP)"
---

# Tasks: Small Business Agent Suite

**Input**: Design documents from `/specs/001-small-business-agents/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/ (rest-api.md, events.md, telegram-bot.md, llm-outputs.md), quickstart.md

**Tests**: The spec's success criteria are verified by automated tests named in quickstart.md (Chaos scenarios for SC-001, role refusals for SC-012, sample-data metrics, per-language extraction accuracy for SC-008). Those acceptance and graph tests are included. Per-function unit tests are not listed separately, except for money and Arabic handling where errors are costly.

**Organization**: Tasks are grouped by user story so each story can be implemented and demonstrated on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story from spec.md (US1–US8)
- Paths follow plan.md: `backend/app/...`, `backend/tests/...`, `frontend/src/...`

## Global rules for every task

- LangGraph is the agent runtime. Every state-changing agent step runs through `harness_graph` via an `ActionSpec`, never by writing to the database directly from a router or handler.
- Code reads "now" only from `BusinessClock` (`backend/app/core/clock.py`), never from `datetime.now()`.
- Money is `amount_minor` (int) + `currency`, and OMR has exponent 3. Floats are never used for money.
- Every row has `business_id`. Every endpoint declares a minimum role, following contracts/rest-api.md.
- LLM calls go only through `LLMClient` (`backend/app/llm/client.py`) with model `claude-opus-5`.
- Figures returned to the UI carry `data_as_of`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [ ] T001 Create the directory skeleton from plan.md "Source Code": `backend/app/{db,models,core,graphs,harness,approvals,llm/prompts,agents/stock,agents/cashflow,agents/accountant,chaos,api/v1,seed}`, `backend/tests/{unit/graphs,contract,integration,eval,fixtures/llm}`, `frontend/src/{api,components,pages,i18n,hooks}`, `frontend/tests/{unit,e2e}`. Add an empty `__init__.py` to each Python package.
- [ ] T002 Create `backend/pyproject.toml` (uv, Python 3.12) with these dependencies: fastapi, uvicorn[standard], pydantic>=2, pydantic-settings, sqlalchemy>=2, alembic, aiosqlite, langgraph>=1, langgraph-checkpoint-sqlite, anthropic, python-telegram-bot>=21, pandas, statsmodels, hijridate, argon2-cffi, python-multipart, sse-starlette, reportlab, arabic-reshaper, python-bidi, pillow. Dev dependencies: pytest, pytest-asyncio, httpx, schemathesis, ruff, mypy.
- [ ] T003 [P] Scaffold `frontend/` with Vite React-TS. Add @tanstack/react-query, react-router-dom, recharts, tailwindcss, i18next, react-i18next, openapi-typescript, openapi-fetch; dev dependencies vitest, @testing-library/react, @playwright/test. Configure Tailwind in `frontend/tailwind.config.ts`.
- [ ] T004 [P] Configure ruff and mypy (strict on `app/`) in `backend/pyproject.toml`, and pytest settings (`asyncio_mode = "auto"`, markers `llm_live`, `slow`).
- [ ] T005 [P] Configure ESLint and Prettier in `frontend/eslint.config.js` and `frontend/.prettierrc`.
- [ ] T006 [P] Implement settings in `backend/app/config.py` with pydantic-settings:
  - `DATABASE_URL` (default `sqlite+aiosqlite:///./var/app.db`)
  - `CHECKPOINT_DB_PATH` (default `./var/checkpoints.db`; LangGraph checkpoints live in their own SQLite file)
  - `SQLITE_BUSY_TIMEOUT_MS` (default 5000)
  - `DEMO_MODE` (bool, default true)
  - `LLM_MODE` (`live|record|replay`, default `replay`)
  - `ANTHROPIC_API_KEY` (optional)
  - `TELEGRAM_BOT_TOKEN` (optional)
  - `FILES_DIR` (default `./var/files`)
  - `SESSION_SECRET`

  Also create `backend/.env.example` listing the same keys.
- [ ] T007 [P] Create `backend/tests/conftest.py` with fixtures:
  - an async in-memory SQLite engine with the schema created
  - a seeded business
  - a `clock` set to a fixed simulated date
  - a `llm_replay` fixture forcing `LLM_MODE=replay`
  - an `api_client` (httpx AsyncClient) with login helpers for owner, manager and staff

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Database, clock, auth, events, LLM client, LangGraph runtime, harness pipeline, approvals (dashboard + Telegram), seed data and the frontend shell. Every story needs these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Database and shared types

- [ ] T008 Implement the async engine and session factory in `backend/app/db/engine.py`:
  - SQLite pragmas on every connection: WAL mode (`PRAGMA journal_mode=WAL`), foreign keys on, `PRAGMA busy_timeout` = `SQLITE_BUSY_TIMEOUT_MS`.
  - Provide the `get_session` FastAPI dependency (reads) and a `write_session()` context manager that holds one process-wide `asyncio.Lock` for the length of the write transaction, so API requests, Telegram callbacks, event handlers and the daily run never write at the same time.
  - All code that commits must use `write_session()`. The lock is a no-op when `DATABASE_URL` is PostgreSQL.
- [ ] T009 [P] Implement shared column types and the base mixin in `backend/app/db/types.py`:
  - `Money` composite: `amount_minor` int + `currency` text. Currency exponent table `{OMR:3, AED:2, SAR:2, QAR:2, KWD:3, BHD:3}`. Helpers `from_decimal`, `to_display` (e.g. `36.000`), add/sub/compare that raise on currency mismatch.
  - `Quantity` = `Decimal(18,4)` + `unit`.
  - `BaseModel` mixin: UUID `id`, `business_id` FK, `created_at`/`updated_at` set from BusinessClock.
- [ ] T010 Initialize Alembic in `backend/alembic/` with `env.py` using the async engine and `app.models` metadata.
- [ ] T011 [P] Create tenancy models in `backend/app/models/tenancy.py`:
  - **Business**: `name` required; `currency` default `OMR`; `country` default `OM`; `vat_registered`; `vat_rate` default 0.05; `vat_period` enum `monthly`/`quarterly`; `weekend_days` default [5,6]; `min_cash_buffer` Money (owner-only edit); `demo_mode`.
  - **User**: `username` unique per business; `password_hash` argon2; `role` enum `owner`/`manager`/`staff`; `language` enum `en`/`ar`; `telegram_chat_id` unique ("one chat ↔ one user"); `telegram_link_code` ("one-time, expires 15 min"); `active`.
  - **Setting** key/value with defaults `price_change_pct=15`, `stock_variance_pct=5`, `approval_timeout_hours=4`, `journal_value_limit=200000` (minor units = OMR 200.000; journal entries above it get the independent second check), `stale_bank_days=1`, `dead_stock_days=21`, `po_auto_approve_limit` (0 = off), `reminder_auto_approve` enum `off`/`polite_only` (default `off`; `polite_only` lets level-1 reminders send without approval, levels 2–3 always need approval), `manual_bookkeeping_hours_per_week=6` (comparison figure for SC-006).
- [ ] T012 [P] Create the BusinessClock model in `backend/app/models/clock.py`: `mode` enum `real`/`simulated`, `current_date`, `last_run_date`, `advancing` bool.
- [ ] T013 [P] Create master-data models in `backend/app/models/master.py`:
  - **Item**: `name_en`, `name_ar`, `unit`, `category`, `is_ingredient`, `is_sold`, `shelf_life_days?`, `reorder_point?`, `safety_stock`, `storage_capacity?`, `preferred_supplier_id`, `is_critical`, `unit_cost` Money, `margin_class` enum `high`/`normal`/`low`.
  - **RecipeLine**: `sold_item_id`, `ingredient_item_id`, quantity + unit.
  - **Supplier**: `name_en`, `name_ar`, `vat_number?`, contact phone/email/telegram, `stated_lead_time_days`, `observed_lead_time_days`, `payment_terms_days`, `early_payment_discount?`, `reliability_score` 0–1, `date_format_hint?` `DMY`/`MDY`.
  - **SupplierAlias**: `supplier_id`, `alias_text`, `normalised_text`, `language`.
  - **SupplierPrice**: `supplier_id`, `item_id`, pack_size + unit, `min_order_qty`, `price` Money, `valid_from`.
- [ ] T014 [P] Create finance master models in `backend/app/models/finance_master.py`:
  - **Account**: `code`, `name_en`, `name_ar`, `type` enum `asset`/`liability`/`equity`/`income`/`expense`, flags `is_bank`, `is_inventory`, `is_vat_input`, `is_vat_output`.
  - **BankAccount**: `name`, `bank`, `currency`, `is_cash_on_hand`.
  - **BankTransaction**: `account_id`, `date`, signed Money `amount`, `description`, `external_ref?`, `import_batch_id`, `match_status` enum `unmatched`/`suggested`/`auto_matched`/`confirmed`/`excluded`, `matched_type`/`matched_id?`, `match_confidence?`.
  - **BankBalanceSnapshot**: `account_id`, `as_of`, `balance`.
  - **Sale**: `date`, `lines` (sold_item_id, qty, amount), `amount_total`, `payment_method` enum `cash`/`card`/`transfer`/`credit`, `source`, `import_batch_id?`, `row_hash?`.
  - **Obligation**: `type` enum `rent`/`salary`/`loan`/`tax`/`utility`/`subscription`/`other`, `description`, `amount`, `next_due_date`, `recurrence` `monthly`/`quarterly`/`annual`/`once`, `is_confirmed`, `last_seen_transaction_id?`.
- [ ] T015 [P] Create harness models in `backend/app/models/harness.py`:
  - **Action**: `agent` enum `stock`/`cashflow`/`accountant`/`harness`; `type`; `graph_name`; `graph_thread_id`; `plan` JSON; `risk_class` enum `read_only`/`reversible`/`irreversible_external`; `stage` enum `planned`/`prechecked`/`awaiting_approval`/`executing`/`verifying`/`completed`/`rolled_back`/`retrying`/`escalated`/`failed`; `dry_run`; `attempt` (1–2); `parent_action_id?`; `result`; `verifier_verdict?`; `incident_id?`.
  - **CheckResult**.
  - **ApprovalRequest**: `kind` enum `approval`/`question`/`alert`; `text_en`; `text_ar`; `options` list (key, label_en, label_ar, effect); `required_role`; `deadline`; `safe_default`; `urgency` 1–3; `status` enum `pending`/`resolved`/`timed_out`/`superseded`; `resolved_option?`; `resolved_by?`; `resolved_via` enum `dashboard`/`telegram`; `resolved_at?`; `reask_count`; `telegram_message_refs`; `graph_thread_id`; `interrupt_id`; `request_token` (opaque).
  - **Incident**: `status` enum `open`/`investigating`/`resolved`/`wont_fix`.
  - **LearnedRule**: `kind` enum `precondition`/`check`/`parsing_hint`/`classification`/`policy`; `status` enum `proposed`/`active`/`rejected`/`inactive`; `trigger` JSON; `times_applied`; `times_overridden`.
  - **AgentCalibration** + **AgentCalibrationHistory**.
  - **AuditLogEntry**: append-only.
- [ ] T016 [P] Create event models in `backend/app/models/events.py`:
  - **Event**: `type`, `version`, `producer`, `payload` JSON, `action_id?`, `occurred_at`.
  - **EventDelivery**: `event_id`, `consumer`, `status`, `handled_at`, `error?`; unique (`event_id`, `consumer`).
  - **FeedOverride** (demo data feed, used by T043): `date`, `overrides` JSON, `chaos_injection_id?`; unique (`business_id`, `date`).
- [ ] T017 Generate the first Alembic migration for T011–T016 in `backend/alembic/versions/0001_foundation.py`.

### Core services

- [ ] T018 Implement the BusinessClock service in `backend/app/core/clock.py`:
  - `now()` / `today()`: simulated date in demo mode, wall clock otherwise.
  - `advance(days=1 | to_date)`: rejects with a conflict error while `advancing` is true, and calls a pluggable per-day callback for each date from `last_run_date+1` to the target, in order.
  - Updates `last_run_date` after each day.
- [ ] T019 [P] Implement auth in `backend/app/core/auth.py`:
  - argon2 hashing
  - signed server-side session in an HTTP-only SameSite=Strict cookie
  - CSRF token check for non-GET requests
  - `require_role(min_role)` dependency with order `staff < manager < owner`
  - on refusal, writes AuditLogEntry `permission_denied` and returns 403 `{error:{code:"permission_denied",message_en,message_ar}}`
  - `can(user, min_role)` helper for use outside HTTP (Telegram)
- [ ] T020 [P] Implement i18n and normalisation helpers in `backend/app/core/i18n.py`:
  - `normalize_digits()`: Arabic-Indic ٠-٩ and Eastern ۰-۹ to 0-9, `٫` to `.`, `٬` removed
  - `normalize_arabic_name()`: strip diacritics and tatweel; unify أإآ→ا, ى→ي, ة→ه; casefold Latin
  - message catalog lookup `t(key, lang, **vars)`
- [ ] T021 [P] Write unit tests in `backend/tests/unit/test_money_i18n.py`: Money arithmetic and 3-decimal display, mismatched-currency error, digit normalisation (`٣٦٫٥٠٠` → `36.500`), Arabic name normalisation matching variants of the same supplier name.
- [ ] T022 Implement the event outbox in `backend/app/core/events.py`:
  - `publish(session, type, payload, producer, action_id)` writes an Event in the caller's transaction.
  - `subscribe(type, consumer_name, handler)` registry.
  - `dispatch_pending()` runs after commit. It calls each handler once per (event, consumer), records EventDelivery, and skips already-handled pairs (idempotent).
  - Validates payload required fields per contracts/events.md.
- [ ] T023 [P] Implement the audit writer in `backend/app/harness/audit.py`: `audit(event, *, agent|user, action_id, inputs, outputs, verification_result)`, which records business-clock and wall-clock timestamps.

### LLM client

- [ ] T024 Implement `LLMClient` in `backend/app/llm/client.py` using the official `anthropic` SDK:
  - `parse(role, system, content_blocks, output_model)` calls `client.messages.parse` with model `claude-opus-5` and `output_config={"effort": EFFORT[role]}`. Effort: extraction and verifier `high`; classification `low`; message and incident `medium`.
  - Server-side refusal fallback: `betas=["server-side-fallback-2026-07-01"]`, `fallbacks="default"`.
  - Checks `stop_reason` and raises `LLMRefusal` on `"refusal"`.
  - Puts `cache_control` on the stable system and schema prefix.
  - Modes: `live`; `record` (live, and writes `backend/tests/fixtures/llm/<sha256>.json`); `replay` (reads the fixture, raising `MissingFixture` if absent). The hash covers role, system, content and schema name.
  - Retries typed rate-limit and 5xx errors only.
- [ ] T025 [P] Define shared LLM output schemas in `backend/app/llm/schemas.py`, exactly as in contracts/llm-outputs.md: `Field[T]`, `InvoiceExtraction`, `VerifierVerdict`, `ExpenseClassification`, `OwnerMessage` (text_en/text_ar ≤ 280 chars), `IncidentAnalysis`, `RuleProposal`.
- [ ] T026 [P] Implement owner-message composition in `backend/app/llm/messages.py`: `compose(facts: dict, options) -> OwnerMessage` calls LLMClient role `message`. It then checks that every number in `text_en`/`text_ar` (after digit normalisation) appears in `facts`, and otherwise falls back to a template from the i18n catalog. Prompt in `backend/app/llm/prompts/owner_message.md`.

### LangGraph runtime and harness pipeline

- [ ] T027 Implement the graph runtime in `backend/app/graphs/runtime.py`:
  - Creates one `AsyncSqliteSaver` at startup on its own SQLite file (`CHECKPOINT_DB_PATH`, WAL mode, same busy timeout), separate from the app database so checkpoint writes never compete with business-data writes. `Action.stage` in the app database stays the source the dashboard reads.
  - Graph registry `register(name, builder)` and `get(name)`.
  - `start(name, input, thread_id)`, `resume(thread_id, value)` (via `Command(resume=value)`) and `state(thread_id)`.
  - Tests can inject `InMemorySaver`.
- [ ] T028 [P] Define `ActionState` (TypedDict) in `backend/app/harness/state.py` with the fields from data-model.md §5 "ActionState".
- [ ] T029 [P] Implement `ActionSpec` and its registry in `backend/app/harness/action_spec.py`. Fields: `name`, `agent`, `risk_class`, and callables `plan`, `preconditions`, `execute(dry_run)`, `verify`, `compensate`. Optional: `verifier_packet` (for high-impact actions), `auto_approve(state, settings) -> bool` (default `False`; each irreversible spec defines its own owner-set rule) and `approval_request(state)` (builds options and text through `llm/messages.compose`).
- [ ] T030 [P] Implement confidence routing in `backend/app/harness/confidence.py`: `band(confidence, agent) -> "act"|"act_flag"|"ask"`. It uses the AgentCalibration thresholds (defaults high 0.90, low 0.60). `act_flag` items go into the daily digest list.
- [ ] T031 [P] Implement the verifier in `backend/app/harness/verifier.py`: `verify_independently(packet) -> VerifierVerdict`. It builds a fresh request from the `VerificationPacket` (action type, source inputs, proposed output, active rules) and must never include primary reasoning or messages. Prompt in `backend/app/llm/prompts/verifier.md`. Includes the pre-mortem failure-mode list for irreversible actions.
- [ ] T032 [P] Implement incident creation in `backend/app/harness/incidents.py`: `open_incident(agent, type, detected_by, summary, refs, action_id?, chaos_injection_id?)` publishes `incident.opened`; `resolve_incident(...)` publishes `incident.resolved`.
- [ ] T033 Build `harness_graph` in `backend/app/harness/graph.py` (LangGraph `StateGraph[ActionState]`):
  - **Nodes**: `plan`, `precheck`, `classify_risk`, `premortem_verify`, `approval_gate`, `execute`, `post_verify`, `rollback`, `retry`, `escalate`, `finalize`.
  - **Routing**:
    - precheck failure goes to `escalate`, which puts the action on hold and asks the owner
    - `premortem_verify` runs only for specs with `verifier_packet`, and disagreement or a high-severity issue goes to `escalate` (no silent retry)
    - `approval_gate` calls `interrupt()` for `irreversible_external` actions unless the spec's `auto_approve(state, settings)` returns true; an auto-approval is audited as `auto_approved` with the rule that allowed it, and all other stages (verifier, pre-send checks, post-verify) still run
    - `execute` runs reversible writes inside a savepoint
    - failed `post_verify` goes to `rollback` → `retry` (attempt 2, with corrections) → `escalate`
  - **Every node**: updates `Action.stage` and writes an audit entry.
  - Register the graph in the runtime.
- [ ] T034 Implement the approval service in `backend/app/approvals/service.py`:
  - `create_from_interrupt(action, payload)` stores the request with an opaque `request_token` and pushes it to the chat SSE broker and the Telegram sender.
  - `resolve(token_or_id, option_key, user, via, edits=None)`:
    - checks `can(user, required_role)`, otherwise audits and returns `permission_denied`
    - does an atomic `UPDATE … WHERE status='pending'`; if no row changed, it returns `already_resolved` with the winning answer
    - on success, calls `runtime.resume(thread_id, {option_key, edits, user_id})` exactly once and notifies both channels
  - `expire_due()` for timeouts (used in US4).
- [ ] T035 [P] Implement the SSE broker and graph streaming in `backend/app/graphs/streaming.py`: an in-process pub/sub keyed by channel (`chat`, `harness`). `run_streamed(graph, input, config)` iterates `astream(stream_mode="updates")` and publishes node updates to `harness`.
- [ ] T036 Implement the daily run in `backend/app/graphs/daily_run.py` and `backend/app/core/scheduler.py`:
  - `daily_run_graph` is built from an ordered slot list: `import_sales`, `stock_update`, `forecast_check`, `reorder`, `bank_import`, `reconcile`, `balance_compare`, `cash_forecast`, `trial_balance`, `reminders`, `approval_timeouts`, `digest`.
  - Agents register node callables into slots, and empty slots are no-ops.
  - `scheduler.advance_to(date)` wires BusinessClock.advance so `daily_run_graph` runs once per day in date order. In real mode an asyncio loop triggers the same run once per real day.

### API shell, Telegram, seed

- [ ] T037 Implement the app entry in `backend/app/main.py`:
  - **Lifespan**: DB, checkpointer, graph registrations, event subscriptions, Telegram poller if a token is set, real-mode daily loop.
  - **Wiring**: mounts `api/v1` routers under `/api/v1`.
  - **Errors**: `{error:{code,message_en,message_ar}}`.
  - **Response helper**: `with_freshness(data, sources)` adds `data_as_of`.
- [ ] T038 [P] Implement auth and user endpoints in `backend/app/api/v1/auth.py` and `backend/app/api/v1/users.py`:
  - `POST /auth/login`, `POST /auth/logout`, `GET /me`
  - `POST /me/telegram-link-code` (8 chars, 15-min expiry)
  - `GET/POST/PATCH /users` (owner only)
- [ ] T039 [P] Implement clock and demo endpoints in `backend/app/api/v1/clock.py`:
  - `GET /clock`
  - `POST /clock/advance` (owner) with body `{days}` or `{to_date}`, returning 409 while advancing
  - `POST /demo/reset` (owner, demo mode only), which re-runs the seed
- [ ] T040 [P] Implement approval endpoints in `backend/app/api/v1/approvals.py`:
  - `GET /approvals?status=` filtered to the caller's role
  - `POST /approvals/{id}/resolve` returning 200 `resolved`, 409 `already_resolved` or 403
  - `GET /chat/stream` (SSE via sse-starlette)
- [ ] T041 Implement the Telegram bot in `backend/app/approvals/telegram_bot.py` (python-telegram-bot v21, long polling), per contracts/telegram-bot.md:
  - **Commands**: `/start <code>` links one chat to one user; `/pending`; `/status`; `/lang en|ar`.
  - **Unlinked chats**: get only the link prompt.
  - **Approval messages**: sent with an inline keyboard, `callback_data = "ar:<request_token>:<option_key>"`.
  - **Callbacks**: call `ApprovalService.resolve(..., via="telegram")`. Dedupe on `callback_query_id`. Edit the message for `resolved` / `already_resolved` / `permission_denied`.
  - **Dashboard resolutions**: edit every sent copy.
  - **Send failures**: audit `telegram_send_failed`, keep the request open in the dashboard (FR-010a) and retry on the next tick.
- [ ] T042 Implement the sample-cafe seed in `backend/app/seed/sample_cafe.py` with CLI `python -m app.seed --sample-cafe [--reset]` in `backend/app/seed/__main__.py`:
  - **Business and users**: 1 business (OMR, VAT 5%, buffer OMR 1,500); users owner, manager and staff, with printed demo passwords.
  - **Items and recipes**: 20 items, milk and coffee beans marked critical; recipes such as latte = 18 g coffee + 200 ml milk.
  - **Suppliers**: 5 suppliers with Arabic and English names, aliases, VAT numbers and prices, including "Al Noor Dairy" and "Gulf Packaging".
  - **Sales history**: 3 months of daily sales with weekday, weekend (Fri/Sat) and Ramadan-evening patterns, produced by the same generator as the daily feed (T043) so history and future days match.
  - **Money**: 2 bank accounts plus cash on hand, with matching transactions.
  - **Obligations**: rent and salaries due in the same week, plus utilities and subscriptions.
  - **Accounts**: small-business chart of accounts in `backend/app/seed/chart_of_accounts.json`.
  - **Calendar**: Oman public holidays for the demo year in `backend/app/seed/oman_holidays.json`.
  - **Clock**: set so the demo starts on a date that shows the milk scenario within 3 simulated days.
- [ ] T043 Implement the simulated daily data feed in `backend/app/seed/feed.py`, which supplies new data for each business date the clock moves into (demo mode only):
  - **Generator**: `generate_day(business, date, overrides=None) -> DayFeed` (sales + bank transactions + balance snapshot). Deterministic: the random seed is derived from `business_id` + date, so replays and tests are repeatable. Also used by T042 to create the history.
  - **Sales**: per sold item, base weekday demand × weekend (Fri/Sat) / public-holiday / Ramadan-evening uplift × noise; split across payment methods `cash`/`card`/`transfer`/`credit`; `source = seed`.
  - **Bank transactions**: card settlements (T+1), cash deposits (every 2–3 days), obligation payments on their due dates (rent, salaries, utilities), customer payments for open receivables following each customer's on-time profile, and payments for supplier invoices on their scheduled date. Ends with one `BankBalanceSnapshot` per account.
  - **Registration**: registers `feed_sales(date)` into the daily_run step `import_sales` and `feed_bank(date)` into `bank_import` (T036). No-op when `DEMO_MODE` is false.
  - **Idempotent per date**: skips if that date's feed rows already exist, so re-running an advance never duplicates data.
  - **Overrides for Chaos**: `overrides` lets injectors change a day's feed, e.g. `{"sales_multiplier": {item_id: 3}}` for the demand spike (scenario 4), `{"skip_bank": true}` for the missing bank feed day (scenario 6), or `{"extra_outflow": Money}` for the cash crunch (scenario 8). They are stored per date in a `FeedOverride` row, and T114 uses them instead of mutating data directly.
  - **Test** in `backend/tests/unit/test_feed.py`:
    - the same date twice gives identical output
    - advancing 7 days creates sales and bank transactions for every day, with no duplicates on a repeated advance
    - weekend and Ramadan days show uplift
    - `skip_bank` produces no bank rows for that date

### Frontend shell

- [ ] T044 [P] Generate the typed API client: script `npm run gen:api` runs openapi-typescript against `/api/v1/openapi.json` into `frontend/src/api/schema.d.ts`. The wrapper `frontend/src/api/client.ts` uses openapi-fetch and sends the CSRF header and cookies.
- [ ] T045 [P] Build the app shell:
  - `frontend/src/App.tsx`: router, QueryClient, role-aware nav that hides pages below the user's role, and a mobile-first layout.
  - `frontend/src/pages/Login.tsx`.
  - `frontend/src/i18n/{index.ts,en.json,ar.json}`, with `dir` switching prepared for Arabic.
- [ ] T046 [P] Build the chat panel: `frontend/src/components/ChatPanel.tsx` and `frontend/src/components/ApprovalCard.tsx`.
  - Subscribe to `/chat/stream`.
  - Render one-tap option buttons.
  - Show "Already answered in <channel> by <user>" on 409.
  - Show "resolved" state.
- [ ] T047 [P] Build `frontend/src/components/ClockControl.tsx` (owner only; "Advance 1 day", "Jump to date", disabled while advancing), `frontend/src/components/FreshnessLabel.tsx` (renders `data_as_of`) and `frontend/src/lib/money.ts` (display with the currency exponent).

### Foundation verification

- [ ] T048 Write graph tests in `backend/tests/unit/graphs/test_harness_graph.py` with `InMemorySaver` and fake ActionSpecs. Cases:
  1. `read_only` completes without interrupt.
  2. A `reversible` action with a failing `verify` is rolled back, retried once, then `escalated` with an incident.
  3. An `irreversible_external` action pauses with an `__interrupt__` payload, and `resume` with approve reaches `completed` while reject reaches `finalize` without `execute`.
  4. A spec whose `auto_approve` returns true skips the interrupt and writes an `auto_approved` audit entry; one returning false (the default) always interrupts.
  5. Verifier disagreement escalates without retry.
  6. Every node writes an audit entry and updates `Action.stage`.
  7. SC-005: every `ApprovalRequest` created by an interrupt has 2–4 options, each with `label_en` and `label_ar`, or is a `question` that accepts one short reply (≤ 100 characters). A request that breaks this fails creation in `ApprovalService`.
- [ ] T049 Write approval tests in `backend/tests/unit/graphs/test_approvals.py`:
  - concurrent resolves from dashboard and telegram: exactly one `resolved`, one `already_resolved`, and the graph resumed once
  - staff resolving a manager request returns `permission_denied` plus an audit entry
  - a checkpoint survives recreating the runtime (restart) and can then be resumed
  - concurrency (file-based SQLite, not in-memory): 20 parallel writers mixing API resolves, Telegram callbacks, event dispatch and a clock advance all complete with no "database is locked" error and no lost update

**Checkpoint**: Foundation ready. User story work can begin.

---

## Phase 3: User Story 1 - Warned before stock runs out; approve a PO in one tap (Priority: P1) 🎯 MVP

**Goal**: The Stock Agent tracks stock from sales and deliveries, forecasts demand, drafts purchase orders early enough, holds suspicious orders, and asks the owner to approve in one tap.

**Independent Test**: Seed the sample cafe and advance the clock. A milk stockout warning with a correct draft PO arrives at least 3 days ahead, approvable in the dashboard or Telegram. The price-spike, duplicate-PO and unit-mismatch cases are held.

### Tests for User Story 1

- [ ] T050 [P] [US1] Write acceptance tests in `backend/tests/integration/test_us1_stock.py` covering spec US1 scenarios 1–6: reorder trigger and quantity rules; recipe deduction (latte → 18 g coffee, 200 ml milk); >15% price change held; duplicate open PO blocked with merge offered; delivery discrepancy flagged; forecast-error fallback to "average of the last 4 same weekdays". Also assert the warning comes ≥ 3 days before the projected stockout, and that a PO past its `expected_date` is flagged late once, with reliability lowered once and an owner alert (FR-017).

### Implementation for User Story 1

- [ ] T051 [P] [US1] Create stock operation models in `backend/app/models/stock_ops.py`:
  - **StockLevel**: unique (item_id, location).
  - **StockMovement**: `type` enum `sale`/`purchase`/`waste`/`spoilage`/`adjustment`/`transfer`/`count_correction`; `reason` "required for waste/adjustment/count_correction"; append-only.
  - **StockCount**: `resolution` enum `accepted`/`investigating`/`adjusted`.
  - **DemandForecast**: `method` enum `holt_winters`/`same_weekday_avg`; unique (item_id, forecast_date, generated_on).
- [ ] T052 [P] [US1] Create purchasing models in `backend/app/models/purchasing.py`:
  - **PurchaseOrder**: `status` enum `draft`/`pending_approval`/`approved`/`sent`/`partially_received`/`received`/`closed`/`rejected`/`on_hold`/`cancelled`; `lines`; `total`; `expected_date`; `is_critical_order`; `created_by_action_id`; `approved_by?`; `approval_request_id?`; `sent_at?`; `merged_into_id?`.
  - **PurchaseOrderLine**.
  - **Delivery** and its lines, with `photo_file_id?`.
  - Enforce the transitions from data-model.md §2 in a `transition(po, to)` helper that raises on illegal moves.
- [ ] T053 [US1] Add the Alembic migration `backend/alembic/versions/0002_stock.py` for T051–T052.
- [ ] T054 [P] [US1] Implement stock tracking in `backend/app/agents/stock/tracking.py`:
  - `apply_sales(date)`: deducts ingredients via RecipeLine; sold items with no mapping raise a `missing_mapping` finding.
  - `receive_delivery(delivery)`.
  - `record_waste(item, qty, reason)` and `record_adjustment(...)`.
  - Every change is a StockMovement, and StockLevel is recomputed.
- [ ] T055 [P] [US1] Implement forecasting in `backend/app/agents/stock/forecasting.py`:
  - `holt_winters(series)` (statsmodels ExponentialSmoothing, weekly seasonality) × calendar uplift factors (weekend, public holiday, Ramadan evening via hijridate) learned from history.
  - `same_weekday_avg(series)`: last 4 same weekdays, with min/max range.
  - `forecast(item, horizon=14..30)` returns low/expected/high per day, using P10/P90 residual quantiles. The daily run always stores a 30-day horizon, so the cash projection (30 days) never runs short of sales forecast; the stock screens show the first 14 days by default.
  - `mape_7d(item)`.
- [ ] T056 [P] [US1] Implement reorder planning in `backend/app/agents/stock/reorder.py`:
  - `days_of_cover = stock / expected daily demand`.
  - Trigger when cover < lead time + safety buffer. Lead time = max(stated, observed); safety stock is raised before peak periods.
  - Quantity respects `min_order_qty`, pack size, `shelf_life_days`, `storage_capacity` and the current weekly PurchasingBudget remaining (unlimited until US3 publishes one).
  - Groups lines by supplier.
  - Produces the projected stockout date and ensures the warning is ≥ 3 days ahead.
- [ ] T057 [P] [US1] Implement stock checks in `backend/app/agents/stock/checks.py`, each returning CheckResult:
  - `negative_or_impossible_stock`
  - `count_variance` (> `stock_variance_pct`)
  - `price_sanity` (new vs trailing median > `price_change_pct` default 15)
  - `duplicate_po` (same supplier and items with an open PO)
  - `unit_mismatch` (order unit or pack size differs from the stock unit)
  - `sales_data_gap` (open business day with no sales)
  - `forecast_accuracy` (MAPE over threshold)
  - `late_delivery` (PO in status `sent` or `partially_received` and `BusinessClock.today()` > `expected_date`; the finding includes days late and whether any line is a critical item)
- [ ] T058 [P] [US1] Implement supplier performance and expiry in `backend/app/agents/stock/supplier.py` and `backend/app/agents/stock/waste.py`:
  - rolling observed lead time and `reliability_score` update on delivery
  - `record_late(supplier, po, days_late)` lowers `reliability_score` once per PO (not once per day late), and the late days count into observed lead time when the delivery arrives
  - expiry risk (stock won't sell before `shelf_life_days`)
  - dead stock (unsold ≥ `dead_stock_days`)
- [ ] T059 [US1] Implement stock ActionSpecs in `backend/app/agents/stock/action_specs.py`:
  - **`draft_po`** (reversible): preconditions run duplicate, price and unit checks. A failing check sets PO `on_hold` and asks the owner. It verifies that the PO exists with the correct totals and no duplicates.
  - **`send_po`** (irreversible_external):
    - `approval_request` uses `llm/messages.compose`, e.g. "Milk will run out Thursday evening. Order 40 L from Al Noor Dairy (arrives Wednesday), OMR 36.000?" with options Approve/Edit/Reject.
    - `verifier_packet` = PO, forecast, stock and price history.
    - `auto_approve` returns true only when `po_auto_approve_limit` > 0 and the PO total ≤ that limit.
    - `execute` marks the PO `sent` and records `sent_at` (MVP: no real supplier contact).
  - **`record_delivery`** (reversible).
  - **`adjust_stock`** (reversible, reason required).
- [ ] T060 [US1] Build the stock graphs in `backend/app/agents/stock/graphs.py`:
  - `stock_update_graph`: apply sales, then run the impossible-stock and sales-gap checks.
  - `forecast_check_graph`: compare yesterday's forecast with actuals. On breach, switch that item's method to `same_weekday_avg`, record calibration and report the cause.
  - `reorder_graph`: first runs `late_delivery` over open POs. For each late PO it:
    - calls `record_late`
    - recomputes days of cover assuming the delivery has not arrived
    - sends the owner an alert, e.g. "Al Noor Dairy delivery is 2 days late; milk runs out Friday", with options to wait, reorder from another supplier, or call the supplier
    - opens an incident only when a critical item would run out before the new expected date

    Then: forecast → cover → quantities → group → one `draft_po` harness run per supplier, then `send_po`.
  - `delivery_graph`: compare delivered vs PO and flag differences.
  - Register them into the daily_run slots `stock_update`, `forecast_check` and `reorder`.
- [ ] T061 [US1] Implement stock endpoints in `backend/app/api/v1/stock.py`, per contracts/rest-api.md "Stock":
  - `GET /stock/items` (staff: no cost fields)
  - `GET /stock/items/{id}/forecast`
  - `POST /stock/waste` (staff)
  - `POST /stock/counts` (manager)
  - `GET /purchase-orders`
  - `PATCH /purchase-orders/{id}`, which re-runs `draft_po` checks
  - `POST /purchase-orders/{id}/deliveries` (staff, multipart photo saved under FILES_DIR by sha256)
  - `GET /suppliers`
- [ ] T062 [US1] Add the Telegram `/delivery <po>` photo caption handler in `backend/app/approvals/telegram_bot.py`, which attaches the photo to the PO's delivery (staff role).
- [ ] T063 [US1] Implement sales import and manual entry, as the real-data alternative to the demo feed (T043):
  - **Service** in `backend/app/agents/stock/sales_import.py`:
    - `parse_csv(file, mapping)`: columns `date`, `item` (matched on `name_en`, `name_ar` via `normalize_arabic_name`, or item id), `qty`, `amount`, `payment_method` (enum `cash`/`card`/`transfer`/`credit`). Arabic-Indic digits are normalised.
    - Row validation:
      - the date is not after `BusinessClock.today()`
      - `qty` > 0
      - `amount` ≥ 0 in the business currency (integer minor units)
      - the payment method is valid
      - the item is known
    - Invalid rows are returned with row number and reason, and are not imported.
    - Deduplicates on `row_hash` (date + item + qty + amount + payment method) within the same `import_batch_id`, and rejects a file whose sha256 was already imported.
  - **ActionSpec** `import_sales` (reversible) in `backend/app/agents/stock/action_specs.py`:
    - `verify` checks that the imported row count and total amount equal the valid rows in the file
    - `compensate` deletes the batch's Sale rows and their StockMovements
    - if a date is already processed (≤ `last_run_date`), it then runs `stock_update_graph` for that date so stock is deducted and the `sales_data_gap` incident for that date is resolved
  - **Endpoints** in `backend/app/api/v1/sales.py` (manager; staff never see amounts):
    - `POST /sales/import` (multipart CSV + optional column mapping) returns `{batch_id, imported, skipped_duplicates, errors[]}`
    - `POST /sales/manual` takes `{date, lines:[{item_id, qty, amount}], payment_method}` for daily entry (`source = manual`)
    - `GET /sales?date=` lists a day's sales with source
  - **Demo-feed interplay**: `feed_sales(date)` in `backend/app/seed/feed.py` skips a date that already has `csv_upload` or `manual` sales, so real and simulated data never double up.
  - **Test** in `backend/tests/integration/test_sales_import.py`:
    - a valid CSV (including Arabic item names and digits) imports and deducts stock
    - bad rows are reported and not imported
    - re-uploading the same file is rejected
    - manual entry for a gap day resolves the `sales_data_gap` incident
    - the demo feed does not add sales for an imported date
- [ ] T064 [P] [US1] Build `frontend/src/pages/Stock.tsx` with components `frontend/src/components/StockTable.tsx` and `frontend/src/components/ForecastChart.tsx`:
  - items with days of cover, reorder status and expiry risk
  - per-item forecast vs actual chart with low/high band and method label
  - open POs and expected deliveries
  - a staff-friendly delivery checklist and waste form
  - a "Sales" tab for managers with CSV upload (column mapping preview and per-row error list) and a manual daily entry form, calling the T063 endpoints

**Checkpoint**: The Stock story works end to end on its own, with the dashboard chat and Telegram approvals.

---

## Phase 4: User Story 2 - Capture supplier invoices and keep the books correct (Priority: P1)

**Goal**: The Accountant Agent reads English, bilingual and Arabic-only invoices, checks them, matches them to POs and the bank, and posts balanced entries. Anything uncertain becomes a one-tap question.

**Independent Test**: Submit sample invoices (including a duplicate, a wrong total and a bilingual mismatch). Valid ones post balanced entries; the duplicate is held; the faulty ones produce targeted questions. Bank transactions are matched with confidence.

### Tests for User Story 2

- [ ] T065 [P] [US2] Write acceptance tests in `backend/tests/integration/test_us2_books.py` (replay mode) for spec US2 scenarios 1–6:
  - fields with per-field confidence and the original file linked
  - arithmetic mismatch: one re-extract, then a question with the field highlighted
  - duplicate by (supplier, number) and by (supplier, amount, date)
  - unbalanced entry blocked
  - three-way mismatch held
  - bank matches auto-applied above 0.90 and the rest listed

  Also cover:
  - Arabic digits normalised
  - the bilingual total disagreement conflict
  - a supplier matched from its Arabic name
  - three owner corrections of the same supplier to the same account open one `recurring_correction` incident and a proposed classification rule, and a fourth correction does not open another (FR-039)
  - two faults on one record: a duplicate invoice that also has a wrong total fires both `duplicate_invoice` and `extraction_arithmetic`; both are reported, and the invoice stays `held` until both are resolved (spec Edge Cases)
- [ ] T066 [P] [US2] Write the live accuracy eval in `backend/tests/eval/test_extraction_accuracy.py` (marker `llm_live`). It scores field accuracy separately for English, bilingual and Arabic-only groups against ground truth, and asserts each group is ≥ 90% (SC-008).

### Implementation for User Story 2

- [ ] T067 [P] [US2] Create books models in `backend/app/models/books.py`:
  - **Document**: file path, mime, `sha256`; `channel` enum `dashboard`/`telegram`; `uploaded_by`; `language_detected` enum `en`/`ar`/`bilingual`; `status` enum `received`/`extracting`/`extracted`/`needs_review`/`posted`/`rejected`/`duplicate`.
  - **Extraction**: `attempt` 1 or 2; `fields` JSON; `document_confidence`; `checks`; `verifier_verdict?`.
  - **PayableInvoice**: lines; `status` enum `draft`/`held`/`posted`/`paid`/`void`; `hold_reason?`; `match_result`; guard on (supplier_id, normalised invoice_number).
  - **ReceivableInvoice**: customer name, contact, `telegram?`; `number` unique per business (auto `INV-###` when not given); `invoice_date`; `due_date`; `lines` (description, qty, unit_price, vat_rate, line_total); `subtotal`, `vat_amount`, `total`; `amount_paid`; `status` enum `open`/`partially_paid`/`paid`/`void`; `paid_on?`; `source` (`seed`/`manual`); `late_payment_history_score`.
  - **JournalEntry**: `status` enum `posted`/`quarantined`/`reversed`; "never edited in place"; reversal creates an opposite entry.
  - **ClassificationCorrection**: `supplier_id`, `from_account_id`, `to_account_id`, `corrected_by`, `date`, `source_ref` (the transaction or invoice line).
  - **JournalLine**: `debit_minor`, `credit_minor`.
- [ ] T068 [US2] Add the Alembic migration `backend/alembic/versions/0003_books.py`.
- [ ] T069 [P] [US2] Build the sample invoice generator in `backend/app/seed/invoices/generate.py`:
  - Renders PDFs and photo-style JPEGs with reportlab, arabic-reshaper and python-bidi.
  - Three groups: English, bilingual and Arabic-only (Arabic-Indic digits).
  - Faulty variants: wrong total, duplicate number, DD/MM ambiguous date, bilingual total mismatch, missing VAT number, and full-quantity invoice vs short delivery.
  - Writes ground-truth JSON next to each file in `backend/app/seed/invoices/out/`.
  - Hooks into the sample-cafe seed.
- [ ] T070 [P] [US2] Write the extraction prompt in `backend/app/llm/prompts/invoice_extraction.md`. It asks for canonical values (ISO dates, Western digits, decimal point) plus `raw_text` as printed, per-field confidence, `date_format_observed`, and both language versions of supplier name and total on bilingual invoices. It includes the supplier hint list and active `parsing_hint` rules.
- [ ] T071 [US2] Implement extraction in `backend/app/agents/accountant/extraction.py`:
  - `extract(document, attempt)` sends a PDF `document` block or an image block with the prompt to `LLMClient.parse(role="extraction", output_model=InvoiceExtraction)`.
  - Post-processing: `normalize_digits` on raw_text vs value; bilingual name and total agreement (a disagreement becomes a conflict check); apply the supplier `date_format_hint`.
  - Confidence = model confidence × penalties (arithmetic, date sanity, unknown supplier, bilingual disagreement). Document confidence = minimum over required fields.
  - `LLMRefusal` is handled as low confidence plus an owner question.
- [ ] T072 [P] [US2] Implement supplier matching in `backend/app/agents/accountant/supplier_match.py`: VAT number first, then exact normalised alias (Arabic or English), then fuzzy ratio ≥ 0.9, otherwise unknown. Creates a SupplierAlias on owner confirmation.
- [ ] T073 [P] [US2] Implement books checks in `backend/app/agents/accountant/checks.py`:
  - `extraction_arithmetic`: lines sum to subtotal; subtotal + VAT = total; VAT = rate × base, within 1 minor unit.
  - `duplicate_invoice`: same supplier + number, or same supplier + total + date; also a sha256 duplicate file.
  - `date_sanity`: future, more than 1 year old, or DMY/MDY ambiguous.
  - `balanced_entry`.
  - `three_way_match`: invoice vs PO vs delivery quantities and prices.
  - `supplier_vat_validity`: VAT charged without a valid number.
  - `missing_po_or_delivery`.
- [ ] T074 [P] [US2] Implement posting in `backend/app/agents/accountant/posting.py`:
  - journal builders for a payable invoice (expense or inventory + VAT input / payable)
  - daily sales summary (bank or cash / sales + VAT output)
  - stock purchases (inventory account)
  - supplier payment
  - Every builder asserts Σdebit = Σcredit before returning.
  - `reverse(entry)`.
- [ ] T075 [P] [US2] Implement bank reconciliation in `backend/app/agents/accountant/matching.py`:
  - `score(txn, candidate)` from amount equality, date proximity, and reference or name similarity (after Arabic normalisation), giving 0–1.
  - Auto-match at or above the high threshold, suggest between the thresholds, leave unmatched below.
  - `reconciliation_status()` returns % matched and the unmatched list.
  - Every match records its `source` (`auto`, `suggested_confirmed`, `manual`) with confidence, so the UI can show where each match came from (FR-046).
- [ ] T076 [P] [US2] Implement expense classification in `backend/app/agents/accountant/classification.py`: first active `classification` rules, then supplier history, then `LLMClient.parse(role="classification", output_model=ExpenseClassification)`. It validates that `account_code` exists and routes by confidence band. Prompt in `backend/app/llm/prompts/classification.md`.

  Recurring-correction detection (FR-039):
  - every owner override of a suggested account is recorded as a `ClassificationCorrection` (supplier_id, from_account, to_account, date)
  - when the same (supplier, to_account) correction reaches 3 within 90 days and no active `classification` rule covers it, open an incident of type `recurring_correction`
  - the incident analysis step (`backend/app/harness/analysis.py`) turns it into a `RuleProposal` of kind `classification`, e.g. "Always classify Gulf Packaging as Packaging supplies (5120)"
  - no second incident is opened while a proposal for the same pair is pending or was rejected in the last 90 days
- [ ] T077 [US2] Implement accountant ActionSpecs in `backend/app/agents/accountant/action_specs.py`:
  - **`post_invoice`** (reversible): preconditions are the checks; verify that the entry is balanced, the invoice is `posted` and there is no duplicate; compensate by reversal. `verifier_packet` applies when total > `journal_value_limit`.
  - **`post_sales_summary`**.
  - **`apply_bank_match`** (reversible).
  - **`quarantine_entry`**.
- [ ] T078 [US2] Build the accountant graphs in `backend/app/agents/accountant/graphs.py`:
  - **`document_graph`**: extract (attempt 1) → normalise and checks → if the arithmetic fails, re-extract (attempt 2) → if still failing or the band is `ask`, `interrupt()` with a question, e.g. "This receipt's total (OMR 52.500) does not equal its lines plus VAT (OMR 50.400). [Use 52.500] [Use 50.400] [Retake photo]" → classify → `post_invoice` harness run. It publishes `invoice.posted` or `invoice.held`.
  - **`reconciliation_graph`**.
  - **`trial_balance_graph`**: a daily check that quarantines the entry causing an imbalance.
  - Register into the daily_run slot `trial_balance` and a new `reconcile` step after `bank_import`.
- [ ] T079 [US2] Implement books endpoints in `backend/app/api/v1/books.py`:
  - `POST /documents` (manager, multipart, returns 202 and starts `document_graph`)
  - `GET /documents?status=`
  - `GET /documents/{id}` (fields, raw text, confidence, checks, file URL)
  - `GET /review-queue`
  - `GET /reconciliation`
  - `POST /reconciliation/{bank_txn_id}/match`
  - `GET /reports/pnl?from&to` and `GET /reports/balance-sheet?as_of`, built from journal lines in `backend/app/agents/accountant/reports.py`
- [ ] T080 [US2] Add the Telegram photo/PDF upload handler in `backend/app/approvals/telegram_bot.py`. For managers and above it creates a Document with channel `telegram` and starts `document_graph`, replying "Received, reading…" and then the result.
- [ ] T081 [US2] Implement customer (receivable) invoices, so receivables can be created in the app rather than only seeded:
  - **Service** in `backend/app/agents/accountant/receivables.py`:
    - `create(customer, invoice_date, due_date, lines)` computes line totals, subtotal, VAT at `Business.vat_rate` and total in integer minor units, and assigns the next `INV-###` number when none is given.
    - Validation:
      - `due_date` ≥ `invoice_date`
      - `invoice_date` is not after `BusinessClock.today()`
      - at least one line
      - qty > 0
      - the number is unique per business, and a duplicate is rejected
    - `void(invoice, reason)` is allowed only while `amount_paid = 0` and posts a reversal.
    - `apply_payment(invoice, amount, bank_txn_id)` sets `partially_paid` or `paid` and `paid_on`. It is used by bank matching (T075) and publishes `customer_payment.received`.
  - **Journal entries** in `backend/app/agents/accountant/posting.py`:
    - on creation: Dr Accounts receivable = total; Cr Sales income = subtotal; Cr VAT output = VAT
    - on payment: Dr Bank; Cr Accounts receivable
  - **ActionSpecs** in `backend/app/agents/accountant/action_specs.py`:
    - `create_receivable` (reversible): `verify` checks that the invoice exists, the entry balances and the AR total rose by exactly `total`; `compensate` voids it with a reversal
    - `void_receivable` (reversible)
  - **Endpoints** in `backend/app/api/v1/books.py` (manager):
    - `POST /receivables` returns 201 with the invoice
    - `GET /receivables?status=&overdue=` (list with ageing bucket)
    - `GET /receivables/{id}` (lines, payments, reminders sent, promises)
    - `POST /receivables/{id}/void`
  - The Cash-Flow Agent picks up new receivables from the shared records on its next forecast. `reminders_graph` (T093) schedules reminders once an invoice is overdue; no new event is needed.
  - **Test** in `backend/tests/integration/test_receivables.py`:
    - create → balanced journal entry and the open invoice appears in the cash forecast inflows
    - duplicate number → rejected
    - a bank payment match → `paid`, reminder cancelled
    - voiding a paid invoice → refused
    - staff → 403
- [ ] T082 [P] [US2] Build `frontend/src/pages/Books.tsx` with `frontend/src/components/DocumentDetail.tsx`:
  - document inbox with extraction confidence
  - original file side by side with fields (low-confidence fields highlighted); every field and every bank match shows its confidence and source (extraction attempt, owner answer, learned rule, auto or manual match) (FR-046)
  - review queue
  - reconciliation % and unmatched list with confirm/override
  - P&L and balance sheet tables
  - a "Customer invoices" tab: list with status and ageing, a create form (customer, dates, lines, with VAT and total computed live), a detail view with payments and reminders, and a void action (T081 endpoints)

**Checkpoint**: The Accountant story works on its own with seeded bank data and sample invoices.

---

## Phase 5: User Story 3 - 30-day cash position and a plan before a shortfall (Priority: P1)

**Goal**: The Cash-Flow Agent projects cash for 30 days, warns about shortfalls at least 14 days ahead with a ranked, simulated action plan, and never sends a reminder for a paid invoice.

**Independent Test**: With rent and salaries in the same week, the shortfall is flagged ≥ 14 days ahead with ranked actions, each showing its simulated effect. A reminder for a just-paid invoice is cancelled.

### Tests for User Story 3

- [ ] T083 [P] [US3] Write acceptance tests in `backend/tests/integration/test_us3_cash.py` for spec US3 scenarios 1–6:
  - 30-day projection with lowest point and date
  - gap size, date and days-to-act
  - ranked actions with impact, risk and simulated forecast
  - reminder cancelled when paid
  - reminder approval rule: with `reminder_auto_approve = off` every reminder waits for approval; with `polite_only` a level-1 reminder is sent without approval (audited `auto_approved`) while level 2 still waits; a paid invoice is cancelled in both modes
  - stale bank data (> `stale_bank_days`) marks low confidence and asks for a statement
  - PO + invoice counted once

  Also assert the shortfall is flagged ≥ 14 days ahead (SC-004), and that no recommendation pays beyond terms.

### Implementation for User Story 3

- [ ] T084 [P] [US3] Create cash models in `backend/app/models/cash.py`:
  - **CashForecastRun**: `generated_on`, `bank_data_as_of`, `low_confidence_reason?`, `lowest_balance`, `lowest_date`.
  - **CashForecast**: `scenario` enum `expected`/`pessimistic`/`optimistic`; `below_buffer`.
  - **ShortfallPlan** and **PlanAction**: `type` enum `chase_receivable`/`delay_payable`/`defer_po`/`move_expense`/`financing`; `rank`; `simulated_lowest_balance`.
  - **PurchasingBudget**: `week_start`, `amount`, `reason`, `tightened`.
  - **PaymentReminder**: `level` 1–3; `status` enum `scheduled`/`pending_approval`/`sent`/`cancelled_paid`/`cancelled_owner`.
  - **PaymentPromise**.
- [ ] T085 [US3] Add the Alembic migration `backend/alembic/versions/0004_cash.py`.
- [ ] T086 [P] [US3] Implement bank statement import in `backend/app/agents/accountant/bank_import.py`: CSV parser (date, description, amount or debit/credit, balance) with column mapping and currency check. It creates BankTransactions and a BankBalanceSnapshot, dedupes by (account, date, amount, description, external_ref), and publishes `customer_payment.received` when a matched receivable is paid.
- [ ] T087 [P] [US3] Implement cash position in `backend/app/agents/cashflow/position.py`: per-account balances plus cash on hand, 7-day committed outflows, and freshness from the latest snapshot.
- [ ] T088 [P] [US3] Implement projection in `backend/app/agents/cashflow/projection.py`:
  - daily 30-day projection: opening + inflows − outflows
  - **Inflows**: Stock sales forecast × settlement lag per payment method; receivables by due date × on-time probability.
  - **Outflows**: payables per the recommended schedule; open and planned POs, deduplicated against invoices by `po_id`; obligations by recurrence.
  - Scenarios use P10/P50/P90 sales and pessimistic/expected receivable delays.
  - Outputs lowest balance and date, and below-buffer days.
- [ ] T089 [P] [US3] Implement cash checks in `backend/app/agents/cashflow/checks.py`:
  - `forecast_vs_actual` (identifies the wrong inflow or outflow line)
  - `bank_freshness`
  - `missing_recurring_obligation` (expected but not in the forecast or not seen in the bank)
  - `double_counting`
  - `unrealistic_inflow` (above the historical P95, which makes pessimistic the primary scenario)
- [ ] T090 [P] [US3] Implement the shortfall plan in `backend/app/agents/cashflow/plan.py`: `detect_shortfall(run)` returns gap, date and days_to_act. `build_plan(run)` produces candidate actions, re-simulates the projection with each one, and ranks by gap closed ÷ risk. Financing is always ranked last.
- [ ] T091 [P] [US3] Implement payables, reminders and budget:
  - `backend/app/agents/cashflow/payables.py`: early for a discount, on time, or end of terms when tight. It never goes beyond terms without owner instruction.
  - `backend/app/agents/cashflow/reminders.py`: escalating levels 1–3, with earlier starts for customers with late history, and promise tracking.
  - `backend/app/agents/cashflow/budget.py`: weekly purchasing budget, tightened when the pessimistic scenario breaches the buffer.
- [ ] T092 [US3] Implement cash-flow ActionSpecs in `backend/app/agents/cashflow/action_specs.py`:
  - **`send_reminder`** (irreversible_external): the precondition re-checks the latest bank transactions and Accountant records. If paid, it cancels with `cancelled_paid` and informs the owner ("Customer Al Mazaya paid invoice INV-104 today, so I cancelled the reminder…"). Has a `verifier_packet`. `auto_approve` returns true only when `reminder_auto_approve = polite_only` and the reminder is level 1; otherwise the owner approves it. The paid-check precondition runs either way.
  - **`publish_budget`** (reversible): publishes `budget.updated`.
  - **`save_forecast_run`** (reversible).
- [ ] T093 [US3] Build the cash graphs in `backend/app/agents/cashflow/graphs.py`:
  - **`cash_forecast_graph`**: freshness check → project → checks → save.
  - **`shortfall_plan_graph`**: detect → plan → owner message via `interrupt()`, e.g. "Heads up: cash drops to OMR 450 on 28 Oct…" with option buttons → publish `shortfall.predicted`.
  - **`balance_compare_graph`**.
  - **`reminders_graph`**.
  - Register into the daily_run slots `balance_compare`, `cash_forecast` and `reminders`.
- [ ] T094 [US3] Implement cash endpoints in `backend/app/api/v1/cash.py`:
  - `GET /cash/position`
  - `GET /cash/forecast?horizon=30d&scenario=`, with the buffer line, lowest point and confidence
  - `GET /cash/shortfall-plan` and `POST /cash/shortfall-plan/actions/{id}/simulate`
  - `GET /cash/receivables` and `GET /cash/payables` (ageing and schedule)
  - `POST /bank/statements` (manager, multipart CSV)
  - `GET/POST/PATCH /obligations` (write: owner)
- [ ] T095 [P] [US3] Build `frontend/src/pages/Cash.tsx` with `frontend/src/components/CashChart.tsx` and `frontend/src/components/ShortfallPlan.tsx`:
  - 30-day chart with the buffer line and lowest-point marker
  - ranked actions with a "Simulate" toggle that overlays the post-action line
  - receivables and payables ageing tables
  - bank statement upload
  - low-confidence banner when the bank data is stale

**Checkpoint**: The Cash-Flow story works on its own with seeded data.

---

## Phase 6: User Story 4 - Assistants catch and fix their own mistakes (Priority: P1)

**Goal**: Self-calibration, incident analysis, owner-approved learned rules applied as checks, approval timeouts with safe defaults, the daily digest, and the Harness view.

**Independent Test**: Force a verification failure. The action is rolled back, retried once, escalated, logged as an incident with a root cause, and followed by a proposed rule. Once approved, the rule changes the next run's behaviour. A request left unanswered past its deadline is re-sent with higher urgency and nothing irreversible happens.

### Tests for User Story 4

- [ ] T096 [P] [US4] Write acceptance tests in `backend/tests/integration/test_us4_harness.py` for spec US4 scenarios 1–8:
  - audit completeness
  - approval needed without an auto-approve rule
  - verifier disagreement escalates
  - rollback → retry → escalate
  - rule approve/reject/edit/deactivate and application on the next run
  - three confidence bands
  - calibration degrade and gradual restore
  - timeout re-ask with urgency +1 and no irreversible execution

### Implementation for User Story 4

- [ ] T097 [P] [US4] Implement self-calibration in `backend/app/harness/calibration.py`:
  - `record(agent, metric, value)` and rolling window evaluation.
  - When over threshold: set `degraded`, raise the confidence thresholds, lower `auto_approve_limit`, set `method_override`, and open an incident with the suspected cause.
  - On recovery, restore in steps. A **healthy day** is a business day on which the metric's value for that day is within its threshold. Each consecutive healthy day moves the thresholds, auto-approve limit and method back 25% of the way to their normal values, so 4 consecutive healthy days fully restore them and clear `degraded`. Any unhealthy day during recovery restarts the count from the current (partly restored) values.
  - Writes AgentCalibrationHistory.
- [ ] T098 [P] [US4] Implement incident analysis in `backend/app/harness/analysis.py`: on escalation or resolution, call `LLMClient.parse(role="incident", output_model=IncidentAnalysis)` then `RuleProposal`. Prompts are in `backend/app/llm/prompts/incident_analysis.md` and `backend/app/llm/prompts/rule_proposal.md`. The trigger is validated against per-kind JSON schemas in `backend/app/harness/rule_schemas.py`.
- [ ] T099 [US4] Implement learned rules in `backend/app/harness/rules.py`:
  - `propose(incident, proposal)` creates a `proposed` rule.
  - `approve`, `reject`, `edit` (new version) and `deactivate` are owner only.
  - `active_rules(agent, kind)` is cached and refreshed on `rule.activated`/`rule.deactivated`.
  - `apply_preconditions(spec, state)` is called by the `precheck` node in `harness/graph.py`.
  - `parsing_hint` rules (e.g. supplier date format DMY) feed the extraction prompt and `date_sanity`.
  - `times_applied` and `times_overridden` are counted.
- [ ] T100 [US4] Wire rules and calibration into the harness:
  - update `backend/app/harness/graph.py` so `precheck` runs active rules and `finalize` records calibration metrics and triggers analysis for escalated actions
  - update `backend/app/harness/confidence.py` to read the live thresholds
- [ ] T101 [US4] Implement approval timeouts and the digest:
  - `ApprovalService.expire_due()` in `backend/app/approvals/service.py`: marks `timed_out`, resumes the graph with the `safe_default` (never irreversible), and creates a new request with `urgency+1` and `reask_count+1`.
  - `backend/app/harness/digest.py` collects `act_flag` items for the daily digest message.
  - Register both in the daily_run slots `approval_timeouts` and `digest`.
- [ ] T102 [US4] Implement harness and settings endpoints in `backend/app/api/v1/harness.py` and `backend/app/api/v1/settings.py`:
  - `GET /harness/actions?live=true`, `GET /harness/actions/{id}` and the SSE `/harness/stream`
  - `GET /harness/incidents`
  - `GET /harness/rules`
  - `POST /harness/rules/{id}/approve|reject|deactivate` and `PATCH /harness/rules/{id}` (owner)
  - `GET /harness/calibration`
  - `GET /audit-log` (owner)
  - `GET/PATCH /settings` (write: owner, audited `setting_changed`)
- [ ] T103 [P] [US4] Build `frontend/src/pages/Harness.tsx` with components:
  - `frontend/src/components/PipelineView.tsx`: live node stages per action from `/harness/stream`.
  - `frontend/src/components/IncidentLog.tsx`: detection method and resolution.
  - `frontend/src/components/RulesPanel.tsx`: active and pending rules, with approve/edit/reject for the owner.
  - `frontend/src/components/CalibrationChart.tsx`: accuracy over time and current thresholds/limits.
  - An action detail drawer showing plan, checks, verifier verdict and audit trail.
- [ ] T104 [P] [US4] Build `frontend/src/pages/Settings.tsx` (owner): thresholds, minimum cash buffer, PO auto-approve limit, reminder auto-approval (off / polite first reminders only), approval timeout, users, and the Telegram link code.

**Checkpoint**: All P1 stories are complete.

---

## Phase 7: User Story 5 - The three assistants coordinate (Priority: P2)

**Goal**: Shared events connect the agents; conflicts go to the owner with both positions; critical stockouts outrank the budget.

**Independent Test**: Run the milk end-to-end scenario and confirm each step is reflected in all three agents:
1. forecast
2. PO
3. budget check
4. short delivery
5. full invoice
6. hold
7. owner confirms
8. corrected posting
9. updated payment
10. lower supplier score
11. proposed rule

### Tests for User Story 5

- [ ] T105 [P] [US5] Write event contract tests in `backend/tests/contract/test_events.py`. Every event type in contracts/events.md is published with its required payload fields and envelope; handlers are idempotent on re-dispatch.
- [ ] T106 [P] [US5] Write the milk end-to-end test in `backend/tests/integration/test_milk_e2e.py`, covering the spec "End-to-end example" steps 1–7 and asserting the state of all three agents after each step.

### Implementation for User Story 5

- [ ] T107 [P] [US5] Implement cash-flow event handlers in `backend/app/agents/cashflow/handlers.py`:
  - `po.drafted`: budget check → publish `budget.check_result`, and add a committed outflow
  - `po.approved_sent`: schedule the outflow
  - `invoice.posted`: replace the PO commitment and schedule payment
  - `invoice.held`: keep the PO amount
  - `customer_payment.received`: cancel reminders and re-forecast
- [ ] T108 [P] [US5] Implement stock event handlers in `backend/app/agents/stock/handlers.py`:
  - `budget.check_result`: proceed, defer or reduce, or open a conflict
  - `budget.updated` and `shortfall.predicted`: defer non-critical orders; prioritise `is_critical`, then `margin_class=high` fast movers
  - `invoice.posted`: update `unit_cost`, and publish `price.changed` when the change exceeds the threshold
  - `invoice.held`: note the supplier issue and lower reliability
  - `stock_valuation.mismatch`: open a joint incident
- [ ] T109 [P] [US5] Implement accountant event handlers in `backend/app/agents/accountant/handlers.py`:
  - `po.approved_sent`: make the open PO available for matching
  - `delivery.received`: store delivery data for the three-way match
  - `price.changed`: update the cost context

  Add a stock valuation agreement check (inventory account vs Stock valuation) in `backend/app/agents/accountant/checks.py` that publishes `stock_valuation.mismatch`.
- [ ] T110 [US5] Build `conflict_graph` in `backend/app/graphs/conflict.py`. It gathers both agents' positions with figures (e.g. Stock: order needed by date X; Cash-Flow: budget remaining Y) and computes a recommendation. A critical item's stockout outranks the budget, and the owner is notified. It then calls `interrupt()` for the owner's choice and publishes the outcome. Register it and start it from the `budget.check_result` handler when `conflict` is set.
- [ ] T111 [US5] Register all handlers in `backend/app/main.py` lifespan. Add a guard test in `backend/tests/unit/test_ownership.py` asserting that each agent package writes only its own tables (the written-by column in data-model.md). Method: a table `OWNERSHIP = {model_class: owning_agent}` in the test; the test parses every module under `backend/app/agents/<agent>/` with Python's `ast` and fails if a module other than `handlers.py` imports a model class owned by another agent. It also fails at runtime if a harness run started by one agent flushes an INSERT/UPDATE/DELETE on another agent's table (checked with a SQLAlchemy `before_flush` listener in the test).

**Checkpoint**: The three agents cooperate through logged events only.

---

## Phase 8: User Story 6 - Chaos mode demonstrates self-correction live (Priority: P2)

**Goal**: A control panel injects each of the 8 fault scenarios. The normal graphs detect, explain and correct the fault, and propose a rule, in under 2 minutes.

**Independent Test**: Run each scenario from the panel. Each shows an incident, an explanation, a correction or rollback, and a proposed rule, with elapsed time recorded.

### Tests for User Story 6

- [ ] T112 [P] [US6] Write parametrised acceptance tests for the 8 scenarios in `backend/tests/integration/test_chaos.py`. Each asserts `detected=True`, an incident with its detection method, an owner explanation, the correction or rollback applied, a proposed rule, and `elapsed_seconds < 120` (SC-001, SC-011). Scenario 3 additionally re-injects after rule approval and asserts no owner question is asked.

### Implementation for User Story 6

- [ ] T113 [P] [US6] Create the ChaosInjection model in `backend/app/models/chaos.py` (`scenario` enum of the 8 spec scenarios, `parameters`, `affected refs`, `outcome` with detected, incident_id, rule_id, elapsed_seconds) and the migration `backend/alembic/versions/0005_chaos.py`.
- [ ] T114 [P] [US6] Implement injectors in `backend/app/chaos/scenarios.py`, each mutating data (scenarios 4, 6 and 8 by writing a `FeedOverride` for the affected dates via T043, rather than editing rows directly) and then running the normal graph:
  1. duplicate supplier invoice
  2. supplier price spike (+25%)
  3. DD/MM date-format invoice
  4. sudden demand spike (×3 sales for one item for 2 days)
  5. customer pays before reminder
  6. missing bank feed day
  7. short delivery vs full invoice
  8. cash crunch (large unplanned outflow ~3 weeks out)
- [ ] T115 [US6] Implement the chaos service in `backend/app/chaos/service.py`: `inject(scenario, params)` is allowed only when `DEMO_MODE`. It links resulting incidents via `chaos_injection_id` and measures elapsed seconds until a rule is proposed.
- [ ] T116 [US6] Implement chaos endpoints in `backend/app/api/v1/chaos.py` (owner, demo only): `GET /chaos/scenarios`, `POST /chaos/inject`, `GET /chaos/injections/{id}`.
- [ ] T117 [P] [US6] Build `frontend/src/pages/Chaos.tsx`: scenario cards with an Inject button, a live timeline (injection → detection → explanation → correction → rule) from `/harness/stream`, and an outcome badge with elapsed time.

**Checkpoint**: All 8 scenarios can be demoed on stage.

---

## Phase 9: User Story 7 - One dashboard showing health, decisions and the harness (Priority: P2)

**Goal**: Home with the health strip, today's decisions and alerts. Freshness on every figure. Mobile friendly.

**Independent Test**: Open the dashboard with the sample cafe: every view shows its listed information with data freshness, and each decision can be answered in one action on a phone-sized screen.

### Tests for User Story 7

- [ ] T118 [P] [US7] Write Playwright e2e tests in `frontend/tests/e2e/dashboard.spec.ts`:
  - home health strip, decisions and alerts visible at 390×844
  - approve a PO from Home in one tap
  - every figure on Home, Stock, Cash and Books renders a FreshnessLabel
  - Harness view shows live stages while an action runs

### Implementation for User Story 7

- [ ] T119 [US7] Implement `GET /home` in `backend/app/api/v1/home.py` (manager):
  - health strip: stock OK/warnings count; lowest cash point in 30 days with its date; % reconciled and review count
  - today's decisions (pending approvals for the caller's role)
  - alerts sorted by urgency
  - all with `data_as_of`
- [ ] T120 [P] [US7] Build `frontend/src/pages/Home.tsx` with `frontend/src/components/HealthStrip.tsx` and `frontend/src/components/AlertList.tsx`: decisions as ApprovalCards, the chat panel docked on desktop and in a drawer on mobile, and the ClockControl for the owner.
- [ ] T121 [US7] Add a response check in `backend/app/main.py` (dev and test only) that fails any figure-bearing response missing `data_as_of`. Add a contract test in `backend/tests/contract/test_freshness.py` iterating the manager-level GET endpoints.

**Checkpoint**: The dashboard is complete for the demo.

---

## Phase 10: User Story 8 - Extended reporting (Priority: P3)

**Goal**: VAT summary, 13-week scenario forecast, supplier scorecard, and Arabic interface.

**Independent Test**: Each capability is checked separately on sample data: the VAT summary totals match the posted invoices; 13 weekly points show three scenarios; the scorecard shows stated vs observed lead time; the UI switches to Arabic RTL.

- [ ] T122 [P] [US8] Implement the VAT summary in `backend/app/agents/accountant/vat.py` (VatPeriodSummary: input VAT, output VAT, net, invoice list, and flags for invoices missing a valid VAT number or breakdown) with `GET /vat/summary?period=` in `backend/app/api/v1/books.py`. The summary is produced by a reversible `prepare_vat_summary` ActionSpec whose `verifier_packet` (period invoices, posted VAT lines, proposed totals) goes through the independent second check, because FR-003 covers tax figures; a disagreement escalates to the owner before the summary is marked ready. Add a reminder before the VAT period closes when bank payments have no invoice ("VAT period ends in 9 days. 4 bank payments have no invoice.").
- [ ] T123 [P] [US8] Add the 13-week weekly aggregation with three scenarios in `backend/app/agents/cashflow/projection.py` (`horizon="13w"`), and the horizon toggle in `frontend/src/components/CashChart.tsx`.
- [ ] T124 [P] [US8] Implement the supplier scorecard at `GET /suppliers/{id}/scorecard` in `backend/app/api/v1/stock.py` (stated vs observed lead time, price change history, reliability score), and `frontend/src/components/SupplierScorecard.tsx`.
- [ ] T125 [P] [US8] Complete the Arabic UI: all strings in `frontend/src/i18n/ar.json`; `dir="rtl"` switching and logical Tailwind properties in `frontend/src/App.tsx`; a language toggle stored on the user via `PATCH /me` in `backend/app/api/v1/auth.py`.

---

## Phase 11: Polish & Cross-Cutting Concerns

**Purpose**: Success-criteria verification, security, demo readiness

- [ ] T126 [P] Write role tests in `backend/tests/integration/test_roles.py`:
  - staff can record deliveries and waste
  - staff get 403 on money endpoints, and money fields are absent from staff responses
  - a manager can approve a PO but cannot approve a rule, change settings or change users
  - a Telegram answer from a staff user is refused

  Every refusal is audit-logged (SC-012).
- [ ] T127 [P] Write Playwright role tests in `frontend/tests/e2e/roles.spec.ts` (nav hides pages; forbidden actions are refused).
- [ ] T128 [P] Write the sample metrics test in `backend/tests/integration/test_sample_metrics.py`. It replays 3 months with the simulated clock against a "no assistant" baseline and asserts the criteria below. The baseline (in `backend/tests/integration/baseline_policy.py`) runs on the same feed data, starting stock and supplier lead times:
  - every Sunday, order each item's average weekly consumption over the previous 4 weeks, from its preferred supplier
  - no forecast, no safety-stock adjustment, no expiry handling and no budget limits
  - stockouts and waste are counted the same way for both runs

  The test also writes a metrics report (`backend/var/reports/sample_metrics.json`) that shows owner effort next to the manual bookkeeping estimate from `Setting.manual_bookkeeping_hours_per_week` (default 6 hours; see spec Assumptions) for SC-006.

  Assertions:
  - SC-002: stockout warnings ≥ 3 days ahead in ≥ 90% of cases
  - SC-003: fewer stockouts and less waste value than the baseline
  - SC-004: shortfalls flagged ≥ 14 days ahead in ≥ 90% of cases
  - SC-006: ≤ 15 minutes and ≤ 25 taps/replies per simulated week, excluding chaos
  - SC-009: ≥ 85% of bank transactions auto-matched with zero incorrect auto-matches
- [ ] T129 [P] Write REST contract tests with schemathesis in `backend/tests/contract/test_openapi.py`, run against `/api/v1/openapi.json` with seeded auth.
- [ ] T130 [P] Write the safety guard test in `backend/tests/unit/test_no_money_movement.py`. It asserts that no module calls a payment or bank write API, that bank credentials are not stored in any model (FR-047, FR-048), and that only `irreversible_external` specs can send outside the system.
- [ ] T131 Record LLM replay fixtures for every demo path (`LLM_MODE=record`, running quickstart steps 2–8) into `backend/tests/fixtures/llm/`. Document re-recording in `backend/tests/fixtures/llm/README.md`.
- [ ] T132 [P] Performance check in `backend/tests/integration/test_performance.py`: a one-day advance takes < 10 s excluding LLM; main GET endpoints respond < 2 s on seeded data.
- [ ] T133 [P] Write the root `README.md`: overview, architecture diagram (LangGraph harness + agent subgraphs + outbox events), setup, and a link to quickstart.md.
- [ ] T134 Run the quickstart.md validation end to end (automated commands, then the manual walkthrough steps 1–8) and fix any gaps found.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: depends on Setup. It blocks all stories.
- **US1, US2, US3 (Phases 3–5, P1)**: each depends only on Foundational. They can run in parallel.
- **US4 (Phase 6, P1)**: depends on Foundational. It is most visible once at least one of US1–US3 exists, but its tests use fake ActionSpecs.
- **US5 (Phase 7)**: depends on US1, US2 and US3 (it connects them).
- **US6 (Phase 8)**: depends on US1–US5. Each scenario needs its agent: 1, 3 and 7 need US2; 2 and 4 need US1; 5, 6 and 8 need US3. All need US4 for rules and US5 for cross-agent effects.
- **US7 (Phase 9)**: depends on the endpoints from US1–US4.
- **US8 (Phase 10)**: depends on US2 (VAT), US3 (13-week) and US1 (scorecard).
- **Polish (Phase 11)**: after the desired stories.

### Story completion order

```text
Setup → Foundational ─┬─ US1 (Stock) ──────┐
                      ├─ US2 (Books) ──────┼─ US5 (Coordination) ─ US6 (Chaos) ─┐
                      ├─ US3 (Cash) ───────┘                                    ├─ Polish
                      └─ US4 (Self-correction) ─ US7 (Dashboard) ─ US8 (P3) ────┘
```

### Within each story

- Acceptance tests first (they should fail), then models, migration, domain logic (plain functions), ActionSpecs, graphs, endpoints, UI.
- Domain logic files marked [P] do not depend on each other.

---

## Parallel Examples

### User Story 1

```text
Task: "T051 Create stock operation models in backend/app/models/stock_ops.py"
Task: "T052 Create purchasing models in backend/app/models/purchasing.py"
# after T053 migration:
Task: "T054 Stock tracking in backend/app/agents/stock/tracking.py"
Task: "T055 Forecasting in backend/app/agents/stock/forecasting.py"
Task: "T056 Reorder planning in backend/app/agents/stock/reorder.py"
Task: "T057 Stock checks in backend/app/agents/stock/checks.py"
Task: "T058 Supplier performance and expiry in backend/app/agents/stock/supplier.py + waste.py"
```

### User Story 2

```text
Task: "T069 Sample invoice generator in backend/app/seed/invoices/generate.py"
Task: "T070 Extraction prompt in backend/app/llm/prompts/invoice_extraction.md"
Task: "T072 Supplier matching in backend/app/agents/accountant/supplier_match.py"
Task: "T073 Books checks in backend/app/agents/accountant/checks.py"
Task: "T074 Posting in backend/app/agents/accountant/posting.py"
Task: "T075 Bank reconciliation in backend/app/agents/accountant/matching.py"
```

### User Story 3

```text
Task: "T086 Bank statement import in backend/app/agents/accountant/bank_import.py"
Task: "T087 Cash position in backend/app/agents/cashflow/position.py"
Task: "T088 Projection in backend/app/agents/cashflow/projection.py"
Task: "T089 Cash checks in backend/app/agents/cashflow/checks.py"
Task: "T090 Shortfall plan in backend/app/agents/cashflow/plan.py"
```

### Across stories (after Foundational)

```text
Developer A: US1 (T050–T064)   Developer B: US2 (T065–T082)
Developer C: US3 (T083–T095)   Developer D: US4 (T096–T104)
```

---

## Implementation Strategy

### MVP first (User Story 1)

1. Phase 1 Setup, then Phase 2 Foundational. This gives the harness graph, approvals in the dashboard and Telegram, the clock and the seed.
2. Phase 3 (US1). **Stop and validate**: advance the clock, see the milk warning, approve in Telegram, and see the dashboard copy show "Already answered".
3. That is a demoable slice: one agent, the full harness lifecycle, and both channels.

### Incremental delivery

1. Add US2, then US3. That gives three standalone agents.
2. Add US4 for self-correction, rules and the Harness view (the judges' centrepiece).
3. Add US5 to connect the agents (milk e2e), then US6 Chaos for the 8 live scenarios.
4. Add US7 dashboard polish, then US8 if time allows, then Polish (metrics, fixtures for an offline demo).

---

## Notes

- [P] = different files, no dependency on incomplete tasks. [USn] maps to spec user stories.
- Commit after each task or logical group. Stop at any checkpoint to validate a story on its own.
- Replay-mode LLM fixtures keep the tests and the demo independent of network and API cost. Only `tests/eval` spends credit.
