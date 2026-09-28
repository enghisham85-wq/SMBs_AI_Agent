# Data Model: Small Business Agent Suite

**Feature**: `001-small-business-agents` | **Date**: 2026-09-28 | **Spec**: [spec.md](./spec.md) | **Research**: [research.md](./research.md)

Conventions (apply to every entity unless stated):

- `id`: UUID primary key. `business_id`: FK → Business on every row (tenant isolation, FR-050).
- `created_at`, `updated_at`: business-clock timestamps (FR-012a), not wall-clock.
- **Money** = `amount_minor` (integer) + `currency` (ISO 4217). The number of decimal places (exponent) comes from the ISO 4217 table, e.g. EGP 2 (piastres), OMR 3 (baisa). Default currency EGP. Never floats (R3).
- **Quantity** = `Decimal(18,4)` + `unit` code from the item's unit set.
- `confidence`: decimal 0–1. `source`: enum of where a value came from (`pos_import`, `csv_upload`, `manual`, `extraction`, `agent`, `seed`, `chaos`).
- Written-by column shows which agent owns writes; other agents change it only via events (FR-044).

---

## 1. Tenancy, users and settings

### Business
| Field | Type | Rules |
|---|---|---|
| name | text | required |
| country | text | ISO 3166 code; default `EG`; selects a CountryProfile; owner can change at any time (new values apply from that date forward) |
| currency | text | ISO 4217 code; default from the profile (`EGP`); owner can change **only while no financial record exists** (no currency conversion in the MVP), otherwise 409 `currency_locked` |
| vat_registered | bool | |
| vat_rate | decimal | default from the profile (EG 0.14); editable; a change applies to invoices dated from then on |
| vat_period | enum `monthly`/`quarterly` | default from the profile (EG `monthly`) |
| weekend_days | int[] | ISO weekday numbers (Mon=1 … Sun=7); default from the profile (EG [5,6] = Fri, Sat) |
| tax_id_pattern | text (regex) | default from the profile (EG 9-digit tax registration number); used by `supplier_vat_validity` |
| min_cash_buffer | Money | owner-only edit; seed default EGP 50,000.00 |

### CountryProfile (reference data, not per business)
Stored as JSON files `backend/app/seed/countries/<code>.json`, loaded at startup. Fields: `code`, `name_en`, `name_ar`, `currency`, `vat_rate`, `vat_period`, `weekend_days`, `tax_id_pattern`, `default_date_format` (`DMY` for EG), `public_holidays` (fixed-date list plus Islamic holidays computed from the Hijri calendar, with an optional per-year override list for moon-sighting dates), `ramadan_source` (`computed` or override dates), `money_defaults` (`min_cash_buffer`, `journal_value_limit`, `po_auto_approve_limit` in that currency), `seed_price_factor` (multiplier from the EGP seed price list). MVP ships `EG` (default), `OM`, `AE`, `SA`. Choosing a profile copies its values into the Business row, where the owner can then edit each one.
| demo_mode | bool | enables BusinessClock simulation |

### User
| Field | Type | Rules |
|---|---|---|
| username | text | unique per business |
| password_hash | text | argon2 |
| role | enum `owner`/`manager`/`staff` | FR-049 |
| language | enum `en`/`ar` | |
| telegram_chat_id | text? | unique; one chat ↔ one user (FR-049a) |
| telegram_link_code | text? | one-time, expires 15 min |
| active | bool | |

### Setting (key/value per business)
Thresholds and limits with defaults: `price_change_pct=15`, `stock_variance_pct=5`, `approval_timeout_hours=4`, `journal_value_limit` (default from the country profile, EG = EGP 10,000.00; journal entries above it get the independent second check), `stale_bank_days=1`, `dead_stock_days=21`, `po_auto_approve_limit` (0 = off), `reminder_auto_approve` enum `off`/`polite_only` (default `off`; `polite_only` lets level-1 reminders send without approval, levels 2–3 always need it), `manual_bookkeeping_hours_per_week=6` (comparison figure for SC-006). Owner-only edit; changes audited.

### BusinessClock
| Field | Type | Rules |
|---|---|---|
| mode | enum `real`/`simulated` | |
| current_date | date | simulated only |
| last_run_date | date | last day whose daily run completed |
| advancing | bool | true while an advance is running; a second advance is rejected (409) |

Advance is rejected while a previous advance is running. Each skipped day's jobs run in date order (R10).

---

## 2. Stock

### Item
name (en, ar), unit, category, `is_ingredient`, `is_sold`, shelf_life_days?, reorder_point?, safety_stock, storage_capacity?, preferred_supplier_id, `is_critical` (FR-043), `unit_cost` (Money, updated from posted invoices), margin_class (`high`/`normal`/`low`).

### RecipeLine
sold_item_id → Item, ingredient_item_id → Item, quantity + unit. A sold item with no recipe lines and no direct mapping triggers the *missing mapping* check.

### StockLevel
item_id, location, quantity, last_counted_at, last_updated_at. Unique (item_id, location). Invariant check: quantity < 0 or > storage_capacity → incident.

### StockMovement
item_id, type enum (`sale`, `purchase`, `waste`, `spoilage`, `adjustment`, `transfer`, `count_correction`), quantity (signed), date, source, reason (required for waste/adjustment/count_correction), reference (sale id / delivery id / action id). Append-only; StockLevel is the running sum.

### StockCount
item_id, counted_qty, calculated_qty, variance, counted_by, date, resolution enum (`accepted`, `investigating`, `adjusted`).

### DemandForecast
item_id, forecast_date, low, expected, high, method enum (`holt_winters`, `same_weekday_avg`), generated_on, confidence. Unique (item_id, forecast_date, generated_on).

### Supplier
name_en, name_ar, vat_number?, contact (phone, email, telegram), stated_lead_time_days, observed_lead_time_days (rolling mean), payment_terms_days, early_payment_discount?, reliability_score 0–1, date_format_hint? (`DMY`/`MDY`, set by learned rule).

### SupplierAlias
supplier_id, alias_text, normalised_text, language. Used for Arabic/English matching (R7).

### SupplierPrice
supplier_id, item_id, pack_size + unit, min_order_qty, price (Money), valid_from. Price-sanity check compares new vs trailing median.

### PurchaseOrder
supplier_id, status, lines (PurchaseOrderLine: item_id, qty, unit, unit_price, line_total), total, expected_date, is_critical_order, created_by_action_id, approved_by?, approval_request_id?, sent_at?, merged_into_id?.

State transitions:
```
draft ──(precheck ok)──> pending_approval ──approve──> approved ──send──> sent ──delivery──> partially_received ──> received ──> closed
  │                         │  └─reject──> rejected                                    └──────────────> received
  │                         └─edit──> draft
  └─(price spike / duplicate / unit mismatch)──> on_hold ──owner decides──> draft | cancelled
auto-approve rule within limit: draft ──> approved (logged, still verified)
```

### Delivery
purchase_order_id, received_on, received_by, lines (item_id, qty_received, unit_price_on_note), photo_file_id?, discrepancies (computed). Emits `delivery.received`.

A PO is **late** when its status is `sent` or `partially_received` and the business date is after `expected_date`. A late PO is flagged once (owner alert, supplier `reliability_score` lowered once), and the late days count into the supplier's observed lead time when the delivery arrives (FR-017).

---

## 3. Sales and cash

### Sale
date, lines (sold_item_id, qty, amount), amount_total, payment_method enum (`cash`, `card`, `transfer`, `credit`), source, import_batch_id?, row_hash? (dedupe within a batch; a file whose sha256 was already imported is rejected). Sources: demo feed (`seed`), CSV upload (`csv_upload`) or daily manual entry (`manual`); the demo feed skips dates that already have uploaded or manual sales. Daily import grouped by date; a business day with no sales for an open day → `sales_data_gap` incident.

### BankAccount
name, bank, currency, is_cash_on_hand.

### BankTransaction
account_id, date, amount (signed Money), description, external_ref?, import_batch_id, match_status enum (`unmatched`, `suggested`, `auto_matched`, `confirmed`, `excluded`), matched_type/matched_id?, match_confidence?.

### BankBalanceSnapshot
account_id, as_of (date+time), balance. Freshness = business-clock now − latest as_of (stale > `stale_bank_days`).

### Obligation
type enum (`rent`, `salary`, `loan`, `tax`, `utility`, `subscription`, `other`), description, amount, due_day/next_due_date, recurrence (`monthly`, `quarterly`, `annual`, `once`), is_confirmed, last_seen_transaction_id?.

### CashForecast (one row per run × scenario × day)
run_id, scenario enum (`expected`, `pessimistic`, `optimistic`), date, opening, inflows, outflows, closing, confidence, below_buffer bool. Run header (CashForecastRun): generated_on, bank_data_as_of, low_confidence_reason?, lowest_balance, lowest_date.

### ShortfallPlan
forecast_run_id, gap_amount, gap_date, days_to_act, actions (PlanAction: type enum [`chase_receivable`, `delay_payable`, `defer_po`, `move_expense`, `financing`], target ref, impact Money, risk enum, rank, simulated_lowest_balance), status.

### PurchasingBudget
week_start, amount, reason, tightened bool, published_event_id.

### PaymentReminder
receivable_invoice_id, level (1 polite → 3 firm), scheduled_for, status enum (`scheduled`, `pending_approval`, `sent`, `cancelled_paid`, `cancelled_owner`), cancel_reason?.

### PaymentPromise
receivable_invoice_id, promised_date, amount, recorded_by.

---

## 4. Books

### Document
file (stored path, mime, sha256), channel enum (`dashboard`, `telegram`), uploaded_by, language_detected enum (`en`, `ar`, `bilingual`), status enum (`received`, `extracting`, `extracted`, `needs_review`, `posted`, `rejected`, `duplicate`).

### Extraction
document_id, attempt (1 or 2), fields (JSON: each field → value, raw_text, confidence), document_confidence, checks (list of CheckResult), verifier_verdict?. SHA-256 duplicate files short-circuit to the existing record.

### PayableInvoice
supplier_id, invoice_number, invoice_date, due_date, lines (desc, qty, unit_price, vat, line_total, item_id?), subtotal, vat_amount, total, supplier_vat_number?, document_id, purchase_order_id?, status enum (`draft`, `held`, `posted`, `paid`, `void`), hold_reason?, match_result (three-way).
- Uniqueness guard: (supplier_id, normalised invoice_number) and soft-dup (supplier_id, total, invoice_date) → `duplicate_invoice` check (FR-034).
```
draft ──checks ok──> posted ──bank match──> paid
  └─check fails──> held ──owner resolves──> posted | void
```

### ReceivableInvoice
customer (name, contact, telegram?), number (unique per business; auto `INV-###` when not given), invoice_date, due_date (≥ invoice_date), lines (description, qty, unit_price, vat_rate, line_total), subtotal, vat_amount, total, amount_paid, status (`open`, `partially_paid`, `paid`, `void`), paid_on?, source (`seed`, `manual`), late_payment_history_score.
```
open ──partial payment──> partially_paid ──rest paid──> paid
  │                                └──full payment──────────────^
  └──void (only while amount_paid = 0, posts reversal)──> void
```
Created by the Accountant Agent (dashboard) or the seed; journal on creation Dr Accounts receivable / Cr Sales + Cr VAT output.

### Account (chart of accounts)
code, name_en, name_ar, type enum (`asset`, `liability`, `equity`, `income`, `expense`), is_bank, is_inventory, is_vat_input, is_vat_output. Seeded from a small-business template.

### JournalEntry
date, reference (type + id), memo, created_by (agent/user), status enum (`posted`, `quarantined`, `reversed`), lines (JournalLine: account_id, debit_minor, credit_minor). Invariant: Σdebit = Σcredit, else posting blocked (FR-035). Reversal creates an opposite entry; entries are never edited in place.

### ClassificationRule / expense classification result
Stored as LearnedRule (below) with `kind = classification`; each classified transaction records account_id, confidence, rule_id?.

### ClassificationCorrection
supplier_id, from_account_id, to_account_id, corrected_by, date, source_ref. Written each time the owner overrides a suggested account. Three identical (supplier, to_account) corrections within 90 days, with no active rule covering them, open a `recurring_correction` incident that leads to a classification rule proposal (FR-039); no repeat while a proposal is pending or was rejected in the last 90 days.

### VatPeriodSummary (P3)
period_start, period_end, input_vat, output_vat, net, invoice_ids, flagged_invoice_ids.

---

## 5. Harness

### Action
agent enum (`stock`, `cashflow`, `accountant`, `harness`), type (e.g. `draft_po`, `post_invoice`, `send_reminder`), `graph_thread_id` (LangGraph thread = action id; its checkpoints hold the in-flight `ActionState`), `graph_name`, plan (JSON: intent, reason, data_refs), risk_class enum (`read_only`, `reversible`, `irreversible_external`), stage enum (`planned`, `prechecked`, `awaiting_approval`, `executing`, `verifying`, `completed`, `rolled_back`, `retrying`, `escalated`, `failed`), dry_run bool, attempt (1–2), parent_action_id?, result (JSON), verifier_verdict?, incident_id?.

```
planned → prechecked → [awaiting_approval] → executing → verifying → completed
                │                                           └─fail→ rolled_back → retrying (attempt 2) → … → escalated
                └─precondition fail → escalated   (the business record, e.g. a PO, may go on_hold; the Action itself has no on_hold stage)
```

### CheckResult
action_id | extraction_id, check_name (e.g. `extraction_arithmetic`, `duplicate_invoice`, `price_sanity`, `balanced_entry`), passed bool, details JSON, learned_rule_id?.

### ApprovalRequest
action_id?, kind enum (`approval`, `question`, `alert`), text_en, text_ar, options (list: key, label_en, label_ar, effect), required_role (minimum role), deadline, safe_default (always non-irreversible), urgency (1–3), status enum (`pending`, `resolved`, `timed_out`, `superseded`), resolved_option?, resolved_by?, resolved_via enum (`dashboard`, `telegram`), resolved_at?, reask_count, telegram_message_refs, graph_thread_id, interrupt_id, request_token (opaque, unique; used in Telegram callbacks instead of the id).
- Created by the `approval_gate` / question nodes when they call LangGraph `interrupt()`; stores `graph_thread_id` and `interrupt_id` so the paused thread can be resumed.
- Resolution is an atomic conditional update `WHERE status='pending'`; only the winning resolution resumes the graph with `Command(resume={option_key, edits, user_id})`; second answer returns "already resolved" (spec edge case).
- On deadline: status `timed_out`, safe default applied, new request created with urgency+1 (FR-011).

### Incident
agent, type (check name or chaos scenario), detected_by (check / verifier / owner), summary, action_taken, root_cause?, status enum (`open`, `investigating`, `resolved`, `wont_fix`), related refs, chaos_injection_id?.

### LearnedRule
agent, kind enum (`precondition`, `check`, `parsing_hint`, `classification`, `policy`), rule_text_en, rule_text_ar, trigger (structured JSON condition, e.g. `{supplier_id: X, field: "date", format: "DMY"}`), source_incident_id, proposed_by, approved_by?, status enum (`proposed`, `active`, `rejected`, `inactive`), times_applied, times_overridden.
```
proposed ──owner approve──> active ──owner deactivate / edit──> inactive | active(v+1)
    └──owner reject──> rejected
```
Only owners may approve/edit (FR-049).

### AgentCalibration
agent, metric (e.g. `forecast_mape`, `extraction_correction_rate`, `recon_mismatch_rate`), window_days, current_value, threshold, high_confidence_threshold, low_confidence_threshold, auto_approve_limit, degraded bool, degraded_since?, method_override?. History in AgentCalibrationHistory (for the accuracy-over-time chart).

### AuditLogEntry
timestamp (business clock + wall clock), agent/user, action_id?, event (stage change, permission_denied, telegram_send_failed, setting_changed, …), inputs JSON, outputs JSON, verification_result?. Append-only.

### Graph checkpoints (LangGraph-managed)
Tables created and owned by `AsyncSqliteSaver` (checkpoints and pending writes per `thread_id`), stored in a separate SQLite file (`var/checkpoints.db`) from the business data. Not written by application code; treated as the durable state of in-flight graph runs. `Action.stage` is the application-level mirror, updated by each harness node, so dashboards and reports never read checkpoint internals.

### ActionState (graph state, not a table)
TypedDict carried through `harness_graph`: `action_id`, `spec_name`, `plan`, `inputs`, `precheck_results`, `risk_class`, `verifier_verdict`, `approval` (option, user), `attempt`, `execution_result`, `verification_result`, `corrections`, `outcome`. Agent subgraphs have their own state types (e.g. `DocumentState`, `ReorderState`, `CashForecastState`) and spawn harness runs for each state-changing step.

### Event (outbox) and EventDelivery
Event: type (see [contracts/events.md](./contracts/events.md)), producer agent, payload JSON, action_id?, occurred_at. EventDelivery: event_id, consumer, status, handled_at, error?. Consumers are idempotent on event_id.

### FeedOverride
date, overrides JSON (e.g. `sales_multiplier` per item, `skip_bank`, `extra_outflow`), chaos_injection_id?. Unique (business_id, date). Read by the demo data feed (`seed/feed.py`) when it generates that date's sales and bank transactions.

### ChaosInjection
scenario enum (8 spec scenarios), injected_by, injected_at, parameters, affected refs, outcome (detected bool, incident_id, rule_id, elapsed_seconds). Used for SC-001 and SC-011 reporting.

---

## 6. Relationships (summary)

```
Business 1─* User, Item, Supplier, …
Supplier 1─* SupplierPrice *─1 Item
Supplier 1─* PurchaseOrder 1─* Delivery
PurchaseOrder 1─* PayableInvoice (three-way match via lines)
Item 1─* RecipeLine (as sold or ingredient), StockMovement, DemandForecast
PayableInvoice / ReceivableInvoice / Sale ─* JournalEntry (by reference)
BankTransaction *─1 matched record (PayableInvoice | ReceivableInvoice | Sale deposit | expense JournalEntry)
Action 1─* CheckResult, AuditLogEntry; Action 0..1─1 ApprovalRequest; Action 0..1─1 Incident
Incident 1─0..1 LearnedRule
ChaosInjection 1─0..* Incident
```
