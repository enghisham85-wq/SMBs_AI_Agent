# Contract: Dashboard REST API

Base path `/api/v1`. JSON over HTTPS. Session cookie auth (R14); state-changing requests require `X-CSRF-Token`. FastAPI generates the OpenAPI document at `/api/v1/openapi.json`; contract tests (schemathesis) run against it.

**Common rules**

- Money in responses: `{ "amount_minor": 36000, "currency": "OMR", "display": "36.000" }`.
- Every figure-bearing response includes `data_as_of` (object of source → timestamp), satisfying FR-046.
- Errors: `{ "error": { "code": "...", "message_en": "...", "message_ar": "..." } }`. `403 permission_denied` is always audit-logged (FR-049a).
- Role column: minimum role allowed (`staff` < `manager` < `owner`). Staff never receive money fields.

## Auth and users

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/auth/login` | public | username + password → session cookie |
| POST | `/auth/logout` | staff | end session |
| GET | `/me` | staff | current user, role, language |
| POST | `/me/telegram-link-code` | staff | issue one-time code to send to the bot |
| GET/POST/PATCH | `/users` | owner | manage users and roles |

## Clock and demo

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/clock` | staff | `{mode, current_date, last_run_date, advancing}` |
| POST | `/clock/advance` | owner | body `{days: 1}` or `{to_date}`; runs skipped days' jobs in order (FR-012a); 409 if already advancing |
| POST | `/demo/reset` | owner | reload sample cafe seed (FR-052) |

## Home

| GET | `/home` | manager | health strip (stock status, 30-day lowest cash point + date, % reconciled, review count), today's decisions, alerts by urgency |

## Approvals (shared with Telegram)

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/approvals?status=pending` | staff* | requests visible to caller's role |
| POST | `/approvals/{id}/resolve` | per request `required_role` | body `{option_key, edits?}`; `200` resolved, `409 already_resolved` with winning answer |
| GET | `/chat/stream` | staff | SSE stream for the in-dashboard chat panel (new requests, resolutions, alerts) |

## Stock

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/stock/items` | staff (no cost fields) | items with qty, days of cover, reorder status, expiry risk |
| GET | `/stock/items/{id}/forecast` | manager | forecast vs actual series with range and method |
| POST | `/stock/waste` | staff | record waste/spoilage with reason |
| POST | `/stock/counts` | manager | submit count(s); variance check runs |
| GET | `/purchase-orders` | manager | list with status |
| PATCH | `/purchase-orders/{id}` | manager | edit draft lines (re-runs checks) |
| POST | `/purchase-orders/{id}/deliveries` | staff | record delivery checklist/photo (multipart) |
| GET | `/suppliers` / `/suppliers/{id}/scorecard` | manager | supplier list / P3 scorecard |
| POST | `/sales/import` | manager | CSV upload (multipart, optional column mapping) → `{batch_id, imported, skipped_duplicates, errors[{row, reason}]}`; same file twice → 409 |
| POST | `/sales/manual` | manager | daily manual entry `{date, lines:[{item_id, qty, amount}], payment_method}` |
| GET | `/sales?date=` | manager | a day's sales with source (`seed`, `csv_upload`, `manual`) |

## Cash

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/cash/position` | manager | balances per account, cash on hand, 7-day committed outflows |
| GET | `/cash/forecast?horizon=30d\|13w&scenario=` | manager | series + buffer line + lowest point + confidence |
| GET | `/cash/shortfall-plan` | manager | ranked actions with impact, risk, simulated forecast |
| POST | `/cash/shortfall-plan/actions/{id}/simulate` | manager | forecast after this action |
| GET | `/cash/receivables` / `/cash/payables` | manager | ageing + reminder/payment schedule |
| POST | `/bank/statements` | manager | upload CSV statement (multipart) |
| GET/POST/PATCH | `/obligations` | owner (write) / manager (read) | recurring obligations |

## Books

| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/documents` | manager | upload invoice image/PDF (multipart) → `202` with document id |
| GET | `/documents?status=` | manager | document inbox with extraction confidence |
| GET | `/documents/{id}` | manager | fields, raw text, per-field confidence, checks, original file URL |
| GET | `/review-queue` | manager | items needing answers |
| GET | `/reconciliation` | manager | % matched, unmatched list |
| POST | `/reconciliation/{bank_txn_id}/match` | manager | confirm/override a match |
| GET | `/reports/pnl?from&to`, `/reports/balance-sheet?as_of` | manager | reports |
| GET | `/vat/summary?period=` | manager | P3 VAT summary |

## Harness

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/harness/actions?live=true` | manager | recent actions with stage; SSE at `/harness/stream` |
| GET | `/harness/actions/{id}` | manager | full plan, checks, verifier verdict, audit trail |
| GET | `/harness/incidents` | manager | incident log |
| GET | `/harness/rules` | manager | learned rules by status |
| POST | `/harness/rules/{id}/approve` \| `/reject` \| `/deactivate`; PATCH `/harness/rules/{id}` | owner | rule lifecycle (FR-008) |
| GET | `/harness/calibration` | manager | accuracy over time, current thresholds and limits |
| GET | `/audit-log?filters` | owner | audit log |
| GET/PATCH | `/settings` | owner (write) / manager (read) | thresholds, buffer, auto-approve rules |

## Chaos mode

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/chaos/scenarios` | owner | the 8 scenarios with parameters |
| POST | `/chaos/inject` | owner | body `{scenario, params?}` → `ChaosInjection` id; only in demo mode |
| GET | `/chaos/injections/{id}` | owner | outcome: detected, incident, rule, elapsed seconds (SC-001, SC-011) |
