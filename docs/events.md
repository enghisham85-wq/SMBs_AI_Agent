# Inter-agent events

Transport: persisted outbox (`Event` table) + in-process dispatch after commit. Every event has the envelope below; consumers must be idempotent on `event_id`. No agent writes another agent's records except in a handler for one of these events.

```json
{
  "event_id": "uuid",
  "type": "po.drafted",
  "version": 1,
  "business_id": "uuid",
  "producer": "stock | cashflow | accountant | harness",
  "action_id": "uuid | null",
  "occurred_at": "business-clock ISO datetime",
  "payload": { }
}
```

| Type | Producer | Consumers | Payload (required fields) | Consumer effect |
|---|---|---|---|---|
| `po.drafted` | stock | cashflow | po_id, supplier_id, total, expected_date, is_critical | Budget check; reply `budget.check_result`; add committed outflow |
| `budget.check_result` | cashflow | stock | po_id, within_budget, remaining_budget, recommendation (`proceed`/`defer`/`reduce`), conflict? | Stock proceeds, defers, or harness raises conflict request |
| `po.approved_sent` | stock | cashflow, accountant | po_id, lines, total, sent_at | Outflow scheduled; open PO available for matching |
| `delivery.received` | stock | accountant | delivery_id, po_id, lines(qty_received, unit_price), discrepancies | Enables three-way match |
| `invoice.posted` | accountant | cashflow, stock | invoice_id, supplier_id, po_id?, due_date, total, unit_costs (line unit costs) | Payment scheduled; PO commitment replaced (no double count); item cost updated |
| `invoice.held` | accountant | stock, cashflow | invoice_id, reason, po_id?, supplier_id?, total? | Stock notes supplier issue; cashflow keeps PO amount |
| `customer_payment.received` | accountant | cashflow | receivable_invoice_id, amount, bank_txn_id | Cancel reminders; update forecast |
| `budget.updated` | cashflow | stock | week_start, amount, tightened, reason | Stock reprioritises (critical → high-margin fast movers first) |
| `shortfall.predicted` | cashflow | stock, harness | run_id, gap_amount, gap_date, days_to_act, plan_id | Stock defers non-critical orders; owner receives plan |
| `price.changed` | stock or accountant | stock, accountant, cashflow | supplier_id, item_id, old_price, new_price, pct | Costs and forecast updated; price-sanity context |
| `stock_valuation.mismatch` | accountant | stock | ledger_value, stock_value, difference | Joint incident opened |
| `supplier.performance_updated` | stock | cashflow | supplier_id, reliability_score, observed_lead_time | Informational |
| `budget.conflict_resolved` | harness (conflict_graph) | dashboard stream | po_id, decision, recommendation, decided_by | Owner's choice on a Stock vs Cash-Flow disagreement is logged |
| `receivable.created` | accountant | cashflow | receivable_invoice_id, total, due_date | Inflow added to the forecast |
| `incident.opened` / `incident.resolved` | harness | dashboard stream | incident_id, agent, type, summary | UI update |
| `rule.activated` / `rule.deactivated` | harness | owning agent | rule_id, agent, kind, trigger | Rule cache refresh |

Versioning: additive payload fields keep `version`; breaking changes bump it and consumers must handle both during migration.
