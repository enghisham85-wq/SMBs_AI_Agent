# Feature Specification: Small Business Agent Suite

**Feature Branch**: `001-small-business-agents`

**Created**: 2026-09-28

**Status**: Draft

**Input**: User description: "@small-business-agents-spec.md" — Three cooperating assistants (Stock, Cash-Flow, Accountant) for small businesses, sharing one set of business records and working under one self-correcting supervisor ("harness") that plans, checks, verifies, rolls back, escalates and learns from every fault. Scope for this feature is the hackathon MVP, demonstrated on a realistic sample cafe.

## Overview

Small businesses (cafes, restaurants, retail shops, workshops, clinics) with 1 to 50 staff lose money in three predictable ways: stockouts or over-ordering, cash shortfalls discovered too late, and books that are behind or wrong. The owner usually has no finance team and prefers a chat app or a simple dashboard over accounting software.

The suite gives the owner three assistants that answer:

| Assistant | Question it answers |
|---|---|
| Stock Agent | "What do I need to order, and when?" |
| Cash-Flow Agent | "Will I have enough cash in 2 to 4 weeks, and what should I do now?" |
| Accountant Agent | "Are my numbers right, and what do I owe?" |

What sets it apart is the shared harness: every assistant thinks before acting, verifies after acting, detects its own faults, acts preventively, and turns each fault into a rule so it does not repeat.

## Clarifications

### Session 2026-09-28

- Q: How should the owner receive and answer approval requests in the MVP? → A: Simulated chat panel inside the dashboard plus a real Telegram bot.
- Q: How does time move during the demo? → A: A simulated business clock the presenter can advance (by day or to a chosen date); all scheduled checks run on each step. Real calendar time is used outside demo mode.
- Q: Which user roles and logins are built in the MVP? → A: Full login with owner, manager and staff roles all enforced.
- Q: Which invoice languages must be read in the MVP? → A: English, bilingual (Arabic plus English) and Arabic-only invoices are all required.
- Q: What weekly owner-effort target should the demo show? → A: At most 15 minutes and 25 taps or replies per simulated week.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Get warned before stock runs out and approve a purchase order in one tap (Priority: P1)

The owner receives a message such as "Milk will run out Thursday evening. Order 40 L from Al Noor Dairy (arrives Wednesday), OMR 36.000? [Approve] [Edit] [Reject]". The Stock Agent keeps stock levels current from sales and deliveries, forecasts demand per item, works out days of cover, and drafts purchase orders grouped by supplier early enough to beat the supplier's lead time. Suspicious orders (price spike, duplicate, unit mismatch) are held and the owner is asked.

**Why this priority**: Stockouts and waste are the most visible daily losses for the target businesses, and this flow shows the full plan → check → approve → execute → verify loop.

**Independent Test**: Load the sample cafe, advance the simulated clock, and confirm that a stockout warning arrives at least 3 days ahead with a correct draft purchase order that the owner can approve, edit or reject in one action.

**Acceptance Scenarios**:

1. **Given** sales history and current stock for an item, **When** its days of cover falls below the supplier lead time plus safety buffer, **Then** the system drafts a purchase order with a recommended quantity that respects minimum order quantity, shelf life and the current purchasing budget, and asks the owner to approve it.
2. **Given** a sale of a recipe item (e.g., 1 latte), **When** the sale is recorded, **Then** ingredient stock is reduced by the recipe quantities (e.g., 18 g coffee, 200 ml milk).
3. **Given** a supplier's new price is more than 15% above its recent price, **When** a purchase order is drafted, **Then** the order is held and the owner is asked to continue or pick an alternative supplier.
4. **Given** an open purchase order already exists for the same supplier and items, **When** a new one is drafted, **Then** the duplicate is blocked and the owner is offered a merge.
5. **Given** a delivery is confirmed by checklist or photo, **When** delivered quantities or prices differ from the purchase order, **Then** the difference is recorded, flagged, and reflected in the supplier's record.
6. **Given** yesterday's forecast error for an item exceeds its threshold, **When** the morning check runs, **Then** the agent lowers its confidence, switches to a simpler method (average of the last 4 same weekdays), and reports the likely cause.

---

### User Story 2 - Capture supplier invoices and keep the books correct (Priority: P1)

The owner forwards an invoice photo or PDF. The Accountant Agent reads the supplier, number, dates, lines, VAT and total, checks the arithmetic, checks for duplicates and date problems, matches it against the purchase order and delivery, and records it in the books with balanced entries. Anything uncertain becomes a specific one-tap question, e.g., "This receipt's total (OMR 52.500) does not equal its lines plus VAT (OMR 50.400). [Use 52.500] [Use 50.400] [Retake photo]".

**Why this priority**: Correct books underpin every figure the other assistants use; without them the cash forecast and stock valuation are wrong.

**Independent Test**: Submit a set of sample invoice images/PDFs (including one duplicate and one with a wrong total) and confirm valid ones are posted, the duplicate is held, and the faulty one produces a targeted question.

**Acceptance Scenarios**:

1. **Given** a clear supplier invoice image or PDF, **When** it is submitted, **Then** supplier, invoice number, invoice date, due date, line items, VAT and total are captured, the original file is linked to the record, and each captured field shows a confidence score.
2. **Given** captured line totals, subtotal, VAT and total that do not add up, **When** the arithmetic check runs, **Then** the document is re-read once and, if still inconsistent, the owner is asked with the conflicting field highlighted.
3. **Given** an invoice with the same supplier and invoice number as one already recorded, or the same supplier, amount and date, **When** it is submitted, **Then** posting is blocked, both are shown, and the owner is asked whether it is a duplicate.
4. **Given** any proposed journal entry, **When** debits do not equal credits, **Then** posting is blocked.
5. **Given** a supplier invoice, a purchase order and a delivery record, **When** quantities or prices differ between them, **Then** the invoice is held for review (three-way match).
6. **Given** imported bank transactions, **When** matching runs, **Then** each transaction is matched to an invoice, payment, sales deposit or expense with a confidence score; matches above the high threshold are applied automatically and the rest are listed for review.

---

### User Story 3 - See the cash position 30 days ahead and get a plan before a shortfall (Priority: P1)

The owner sees today's cash, the lowest projected balance in the next 30 days and the date it happens. If the balance is projected to fall below the owner's minimum buffer, the Cash-Flow Agent warns weeks ahead and proposes a ranked list of actions (chase overdue invoices, delay a non-critical order, move an expense), showing the effect of each on the forecast. Before any payment reminder goes out, it checks the invoice is still unpaid.

**Why this priority**: Running out of cash is the most damaging failure for a small business, and early warning is the main value the owner cannot get elsewhere.

**Independent Test**: Load the sample cafe with rent and salaries falling in the same week and confirm the shortfall is flagged at least 14 days ahead with a ranked, simulated action plan.

**Acceptance Scenarios**:

1. **Given** bank balances, receivables, payables, planned purchase orders and recurring obligations, **When** the forecast runs, **Then** a daily projected balance for the next 30 days is shown with the lowest point and its date.
2. **Given** a projected day below the minimum buffer, **When** it is detected, **Then** the owner is told the size of the gap, the date, the number of days left to act, and a ranked list of actions each with its impact and risk.
3. **Given** a proposed action in the plan, **When** the owner views it, **Then** the forecast after that action is shown.
4. **Given** a scheduled payment reminder, **When** the latest bank transactions or books show the invoice already paid, **Then** the reminder is cancelled and the owner is told why.
5. **Given** bank data older than one business day, **When** the forecast is shown, **Then** it is marked low confidence and the owner is asked for a fresh statement.
6. **Given** a payable that appears both as a purchase order commitment and as an invoice, **When** the forecast runs, **Then** it is counted once.

---

### User Story 4 - Trust the assistants because they catch and fix their own mistakes (Priority: P1)

Every assistant action passes through the harness: plan, precondition check, risk classification, independent second review for high-impact outputs, execution, read-back verification, rollback or escalation, and logging. Every detected fault becomes an incident with a root cause and a proposed rule (e.g., "Supplier X invoices use DD/MM format; never parse as MM/DD") that the owner can approve, reject or edit.

**Why this priority**: The harness is the product's differentiator and the centrepiece of the demo; it is what makes it safe to let the assistants act.

**Independent Test**: Trigger any action that fails verification and confirm it is rolled back, retried once, escalated if still failing, logged as an incident, and followed by a proposed learned rule.

**Acceptance Scenarios**:

1. **Given** any assistant action, **When** it runs, **Then** its plan, inputs, risk class, execution result and verification result are recorded in the audit log.
2. **Given** an action classed as irreversible or external (sending a purchase order, sending a payment reminder), **When** no matching auto-approve rule exists, **Then** the action waits for owner approval.
3. **Given** a high-impact output (purchase order, payment reminder, journal entry above the value limit), **When** the independent reviewer — which sees the inputs and proposed output but not the original reasoning — disagrees, **Then** the action is escalated to the owner rather than silently retried.
4. **Given** a reversible action whose read-back does not match its intent, **When** verification fails, **Then** the action is rolled back, retried once with corrected input, and escalated to the owner if it fails again.
5. **Given** a resolved incident, **When** the assistant proposes a learned rule, **Then** the owner can approve, reject or edit it, and approved rules are applied as checks on later actions.
6. **Given** an output with confidence above the high threshold, between thresholds, or below the low threshold, **When** it is handled, **Then** it is acted on automatically, acted on and flagged in the daily digest, or not acted on and turned into a specific owner question, respectively.
7. **Given** an assistant's recent error rate rises above its threshold, **When** self-calibration runs, **Then** its confidence and auto-approve limits are lowered, it falls back to a safer method, and the owner is told the suspected cause; limits are restored gradually as accuracy recovers.
8. **Given** an approval request the owner has not answered within the set time, **When** the timeout passes, **Then** nothing irreversible is done and the request is re-sent with raised urgency.

---

### User Story 5 - The three assistants coordinate instead of contradicting each other (Priority: P2)

Assistants share one set of records and notify each other of events: a drafted purchase order becomes a committed outflow; a posted invoice updates item cost and the payment schedule; a customer payment cancels reminders; a predicted shortfall tightens the purchasing budget. When they disagree, the owner sees both positions with numbers and a recommended choice.

**Why this priority**: Coordination is what turns three tools into one system, but each assistant already delivers value alone (stories 1–3).

**Independent Test**: Run the end-to-end milk scenario (forecast → PO → budget check → short delivery → full invoice → hold → owner confirms → corrected posting → updated payment → lower supplier score → proposed rule) and confirm each step is reflected in all three assistants.

**Acceptance Scenarios**:

1. **Given** a drafted purchase order, **When** the Cash-Flow Agent receives it, **Then** it is checked against the weekly purchasing budget and added as a committed outflow.
2. **Given** a predicted shortfall, **When** it is published, **Then** the Stock Agent defers non-critical orders and prioritises high-margin fast movers.
3. **Given** a critical item (as defined per item) at risk of stockout and no budget, **When** the conflict arises, **Then** the stockout prevention outranks the budget and the owner is notified.
4. **Given** a disagreement between two assistants, **When** it arises, **Then** the owner sees both positions with figures and a recommended choice.
5. **Given** any change one assistant needs to make to another assistant's record, **When** it happens, **Then** it goes through a logged event; no assistant overwrites another's record silently.

---

### User Story 6 - Demonstrate self-correction live with Chaos mode (Priority: P2)

A presenter opens a control panel and injects a fault. The audience watches the relevant assistant detect it, explain it, correct or roll it back, and propose a learned rule.

**Why this priority**: Required for the hackathon demo and for testing the harness; depends on stories 1–4.

**Independent Test**: Run each of the 8 scenarios below from the control panel and confirm detection, explanation, correction and a proposed rule for each.

**Acceptance Scenarios** (one per scenario):

1. **Duplicate supplier invoice** → second posting blocked; cash forecast does not double-count the payment.
2. **Supplier price spike** → purchase order held; owner asked; alternative supplier proposed.
3. **Wrong date format on invoice** → impossible due date detected; corrected using the supplier's rule, or the rule is created if missing.
4. **Sudden demand spike** → forecast error detected; confidence lowered; method switched; safety stock raised; owner informed.
5. **Customer pays before reminder** → pre-send check cancels the reminder.
6. **Missing bank feed day** → forecast marked low confidence; statement requested rather than guessed.
7. **Short delivery vs full invoice** → three-way match holds the invoice.
8. **Cash crunch** → shortfall predicted about 3 weeks ahead; purchasing budget tightened; ranked action plan presented.

---

### User Story 7 - One dashboard showing health, decisions and the harness at work (Priority: P2)

The owner opens the dashboard on a phone or computer and sees a health strip (stock status, lowest cash point in 30 days, percent reconciled and items to review), today's decisions, and alerts sorted by urgency. Stock, Cash, Books and Harness views give detail; the Harness view shows the live action pipeline, incident log, learned rules, accuracy over time, current confidence limits and the Chaos mode panel.

**Why this priority**: The dashboard is the main demo surface, but the underlying flows are testable through the approval channel alone.

**Independent Test**: Open the dashboard with the sample cafe loaded and confirm each view shows the listed information, with data freshness stated on every figure.

**Acceptance Scenarios**:

1. **Given** pending approvals and questions, **When** the owner opens Home, **Then** they are listed as today's decisions and each can be answered in one action.
2. **Given** any figure shown, **When** it is displayed, **Then** its data freshness is stated (e.g., "bank data as of today 08:00").
3. **Given** the Harness view, **When** an action is running, **Then** its current stage (plan, check, execute, verify) is visible.

---

### User Story 8 - Extended reporting (Priority: P3)

Nice-to-have capabilities: VAT summary per period with supporting invoice list and flags for invoices missing a valid VAT number; 13-week cash forecast with expected, pessimistic and optimistic scenarios; supplier scorecard (stated vs actual lead time, price changes, reliability score); Arabic interface and messages.

**Why this priority**: Valuable for real use but not required to demonstrate the core concept.

**Independent Test**: Each capability can be checked on the sample cafe data independently.

**Acceptance Scenarios**:

1. **Given** a VAT period with posted invoices, **When** the owner requests the summary, **Then** input VAT, output VAT and net payable are shown with the supporting invoice list.
2. **Given** the 13-week forecast, **When** shown, **Then** three scenarios are displayed against the buffer line.
3. **Given** the owner selects Arabic, **When** messages and screens are shown, **Then** they appear in Arabic.

### Edge Cases

- Sales data missing for an open business day: forecasting updates for that day are paused and the owner is alerted; no demand is assumed to be zero.
- Calculated stock goes negative or above storage capacity: flagged as a likely missing recipe mapping or unrecorded delivery, and a stock count is requested.
- Counted stock differs from calculated stock beyond tolerance: investigated (missing waste record, recipe error, possible theft) and adjusted with a recorded reason.
- Order unit differs from stock unit, or pack size changed: blocked until the owner confirms.
- Invoice date in the future, far in the past, or ambiguous between DD/MM and MM/DD: the supplier's learned format is applied; otherwise the owner is asked.
- Supplier invoice arrives with no matching purchase order or delivery: flagged as a possible error or fraud.
- Ledger does not balance on the daily check: the entry causing the imbalance is located and quarantined.
- Expected recurring obligation (rent, salary) missing from the forecast or not seen in the bank: the owner is asked to confirm or add it.
- Forecast relies on an inflow much larger than the historical pattern: flagged, and the pessimistic scenario is used as primary.
- Actual closing balance differs from forecast beyond threshold: the wrong inflow or outflow is identified, the forecast corrected, and the owner informed.
- Owner never responds to an approval: nothing irreversible happens; the request is re-sent with raised urgency.
- Two faults hit the same record at once (e.g., duplicate invoice that also has a wrong total): each is detected and reported; the record is held until both are resolved.
- Bilingual invoice whose Arabic and English totals or names disagree: treated as an extraction conflict; the owner is asked with both values shown.
- Presenter jumps the clock several days ahead: skipped days' checks run in order, and approvals left unanswered through the jump time out per FR-011 rather than being skipped.
- Owner answers the same request in Telegram and in the dashboard: the first answer wins; the second is shown as already resolved.
- A learned rule the owner approved later produces wrong results: the owner can deactivate or edit it from the dashboard.

## Requirements *(mandatory)*

### Functional Requirements

**Shared harness**

- **FR-001**: Every assistant action MUST pass through the lifecycle: plan (intent, reason, data relied on) → precondition check (data freshness, required fields, values within expected ranges, no conflicting pending actions) → risk classification → independent review (high-impact actions) → execution (optionally dry-run) → read-back verification → rollback/retry/escalate → log.
- **FR-002**: Actions MUST be classed as read-only (no approval), reversible write (automatic, can be rolled back), or irreversible/external (requires owner approval unless an owner-defined auto-approve rule with a value limit covers it).
- **FR-003**: High-impact outputs (purchase orders, payment reminders, journal entries above a configurable value limit, tax figures) MUST be reviewed by an independent second check that sees the inputs and proposed output but not the original reasoning; disagreement MUST escalate to the owner.
- **FR-004**: When verification fails, the system MUST roll back reversible actions, retry once with corrected input, and escalate to the owner if the retry also fails.
- **FR-005**: Every captured field, forecast and classification MUST carry a confidence score from 0 to 1, handled by per-assistant high and low thresholds (act / act and flag in daily digest / ask the owner).
- **FR-006**: Each assistant MUST track its own error rate (forecast error, owner corrections, reconciliation mismatches); when it exceeds a threshold, the assistant MUST lower its confidence and auto-approve limits, fall back to a safer method, and report the suspected cause; limits MUST be restored gradually as accuracy recovers.
- **FR-007**: Every detected fault MUST create an incident record (what happened, how detected, action taken, root cause, status).
- **FR-008**: After resolving an incident, the assistant MUST propose a learned rule; the owner MUST be able to approve, reject, edit and deactivate rules, and approved rules MUST be applied as checks on later actions.
- **FR-009**: Every action step MUST be recorded in an audit log (time, assistant, action, inputs, outputs, verification result).
- **FR-010**: Owner requests MUST be specific and answerable in one tap or one short reply, delivered through both a simulated chat panel inside the dashboard and a real Telegram bot; an answer given in either place MUST resolve the request in both, and a request MUST NOT be actionable twice.
- **FR-010a**: If the Telegram bot is unreachable, requests MUST remain available and answerable in the dashboard chat panel, and the failure MUST be logged.
- **FR-011**: If the owner does not answer within a configurable time, the system MUST do nothing irreversible and MUST re-send the request with raised urgency.
- **FR-012**: A Chaos mode control panel MUST let a user inject each of the 8 listed fault scenarios on demand.
- **FR-012a**: In demo mode, the system MUST use a simulated business clock that the presenter can advance by one day or jump to a chosen future date; on each advance, every scheduled activity that falls in the skipped period (morning forecast checks, daily balance comparison, trial balance, reminders, approval timeouts) MUST run in date order. Outside demo mode, real calendar time is used. All records, logs and freshness labels MUST use the business clock's date.

**Stock Agent**

- **FR-013**: The system MUST reduce stock automatically from sales using recipes or item mappings, add stock on confirmed deliveries, and record waste, spoilage and manual adjustments with reasons.
- **FR-014**: The system MUST forecast daily demand per item for the next 14 days (minimum) as a low / expected / high range, using day-of-week pattern, recent trend and calendar events (weekends, public holidays, Ramadan).
- **FR-015**: The system MUST calculate days of cover per item and trigger a reorder when it falls below supplier lead time plus safety buffer, warning at least 3 days before a projected stockout.
- **FR-016**: Recommended order quantities MUST respect minimum order quantity, shelf life and the current purchasing budget, and items MUST be grouped by supplier into one purchase order.
- **FR-017**: The system MUST draft purchase orders for owner approval (or auto-approve within owner-set limits) and track expected delivery dates, flagging late deliveries.
- **FR-018**: The system MUST compare delivered quantities and prices against the purchase order and flag differences.
- **FR-019**: The system MUST hold any purchase order whose supplier price changed by more than a configurable threshold (default 15%), block duplicate open orders for the same supplier and items, and block unit or pack-size mismatches until confirmed.
- **FR-020**: Each morning, the system MUST compare the previous day's forecast with actual sales per item and apply self-calibration (FR-006) when error exceeds threshold.
- **FR-021**: The system MUST order earlier from suppliers whose observed lead time exceeds their stated lead time, and raise safety stock ahead of known peak periods.
- **FR-022**: The system MUST flag items at risk of expiring before they sell and items unsold for a configurable number of days.

**Cash-Flow Agent**

- **FR-023**: The system MUST show current cash across accounts and cash on hand, and committed outflows for the next 7 days.
- **FR-024**: The system MUST produce a rolling daily cash forecast for 30 days from bank balances, sales forecast, receivables, payables, planned purchase orders and recurring obligations, highlighting the lowest balance and its date.
- **FR-025**: The system MUST flag any day where the projected balance falls below the owner-defined minimum buffer, stating the gap size and days remaining to act.
- **FR-026**: The system MUST propose a ranked list of gap-closing actions (chase overdue receivables, delay a non-critical payment within terms, reduce or delay a purchase order, move a planned expense, and short-term financing only as a last resort), each with impact, risk and the resulting forecast.
- **FR-027**: The system MUST send polite, escalating payment reminders for overdue customer invoices only under owner approval rules, and MUST verify before sending that the invoice is still unpaid, cancelling the reminder if paid.
- **FR-028**: The system MUST recommend payment timing for supplier invoices (early for a discount, on time, or end of terms if cash is tight) and MUST NOT recommend paying beyond terms without explicit owner instruction.
- **FR-029**: The system MUST publish a weekly purchasing budget that the Stock Agent respects, and tighten it automatically when the forecast breaches the buffer.
- **FR-030**: Each day the system MUST compare the previous day's projected closing balance with the actual balance, identify the wrong inflow or outflow when variance exceeds threshold, correct the forecast and report.
- **FR-031**: The system MUST mark the forecast low confidence when bank data is stale, ask about missing recurring obligations, count each payable once when it appears as both purchase order and invoice, and flag inflow assumptions far above the historical pattern.

**Accountant Agent**

- **FR-032**: The system MUST accept supplier invoices and receipts as images or PDFs through the chat channel or dashboard, capture supplier, invoice number, dates, line items, VAT and total, and keep the original file linked to the record. English, bilingual (Arabic plus English) and Arabic-only invoices MUST all be supported, including Arabic-Indic numerals (٠-٩), which MUST be normalised to standard figures; supplier names MUST be matched to the same supplier record whether written in Arabic or English.
- **FR-033**: The system MUST check captured arithmetic (lines sum to subtotal; subtotal plus VAT equals total; VAT equals rate times base), re-read once on failure, then ask the owner with the field highlighted.
- **FR-034**: The system MUST block posting of likely duplicates (same supplier and number, or same supplier, amount and date) and ask the owner.
- **FR-035**: The system MUST create payable and receivable records and post balanced double-entry journal entries with correct accounts and VAT split, including daily sales summaries and stock purchases; unbalanced entries MUST be blocked.
- **FR-036**: The system MUST run a daily trial-balance check and quarantine any entry causing imbalance.
- **FR-037**: The system MUST match bank transactions to invoices, payments, sales deposits or expenses with a confidence score, auto-match above the high threshold, and list unmatched items for review.
- **FR-038**: The system MUST perform a three-way match between supplier invoice, purchase order and received quantities, holding the invoice when they differ.
- **FR-039**: The system MUST classify expenses using supplier history, descriptions and learned rules, ask the owner only when confidence is low, and propose a rule when the same correction recurs.
- **FR-040**: The system MUST flag a mismatch between the books' inventory value and the Stock Agent's valuation to both assistants.
- **FR-041**: The system MUST provide a review queue of items needing owner answers and show reconciliation status (percent matched, unmatched items).

**Coordination and dashboard**

- **FR-042**: All three assistants MUST read and write one shared set of records so their figures agree, and MUST exchange the events listed in Key Entities / Shared Events.
- **FR-043**: When assistants disagree, the owner MUST be shown both positions with figures and a recommended choice; stockout of an item marked critical outranks the budget, with the owner notified.
- **FR-044**: No assistant may change another assistant's records except through a logged event.
- **FR-045**: The dashboard MUST provide Home (health strip, today's decisions, alerts by urgency), Stock, Cash, Books and Harness views as described in User Story 7, usable on mobile.
- **FR-046**: Every figure shown to the owner MUST state its data freshness, and every captured or matched record MUST show its confidence and source.

**Safety, access and localisation**

- **FR-047**: No assistant may move money; payments are only recommended and scheduled, and the owner executes them.
- **FR-048**: Bank credentials MUST never be stored; bank data comes only from read-only feeds or uploaded statements.
- **FR-049**: Every user MUST log in, and the system MUST enforce three roles on every screen, chat action and approval:
  - **Owner**: full access, including settings, users, thresholds, minimum cash buffer, auto-approve rules and learned-rule approval.
  - **Manager**: views everything and handles day-to-day requests (approve or edit purchase orders and payment reminders, answer review questions, record deliveries, waste and counts), but cannot change settings, users, thresholds, the cash buffer or auto-approve rules, and cannot approve learned rules.
  - **Staff**: may only record deliveries and waste; sees no financial figures.
- **FR-049a**: Any attempt to act beyond a role's permissions MUST be refused and logged in the audit log. Each Telegram chat MUST be linked to exactly one user, and requests sent there MUST follow that user's role.
- **FR-050**: Each business's data MUST be isolated from every other business.
- **FR-051**: Amounts MUST support 3 decimal places (OMR default) and be configurable for other GCC currencies; VAT rate and filing period MUST be configurable (Oman default 5%).
- **FR-052**: The system MUST ship with a sample cafe dataset: 3 months of sales, 20 items, 5 suppliers, matching bank transactions, and sample supplier invoices in English, bilingual and Arabic-only forms.

**Nice to have (P3)**

- **FR-053**: The system SHOULD produce a VAT summary per period with supporting invoice list and flags for invoices missing a valid VAT number or breakdown.
- **FR-054**: The system SHOULD produce a 13-week weekly forecast with expected, pessimistic and optimistic scenarios.
- **FR-055**: The system SHOULD maintain a supplier scorecard (stated vs observed lead time, price changes, reliability score).
- **FR-056**: The system SHOULD offer Arabic as well as English for screens and messages.

### Key Entities *(include if feature involves data)*

- **Item**: A product or ingredient; name, unit, category, shelf life, reorder point, safety stock, preferred supplier, critical flag.
- **Recipe / Item Mapping**: How a sold product consumes items (e.g., 1 latte = 18 g coffee + 200 ml milk).
- **Stock Level**: Quantity of an item at a location, with last counted and last updated times.
- **Stock Movement**: A change to stock (sale, purchase, waste, adjustment, transfer) with quantity, date and source.
- **Supplier**: Name (Arabic and English variants), contact, stated and observed lead time, minimum order quantity, prices, payment terms, reliability score.
- **Purchase Order**: Supplier, lines, status, expected date, approver.
- **Delivery**: Received quantities and prices against a purchase order, confirmed by staff or owner.
- **Sale**: Date, items, amount, payment method, source (sales system or manual).
- **Payable Invoice**: Supplier, number, dates, lines, VAT, total, status, linked original file.
- **Receivable Invoice**: Customer, number, dates, total, status, reminders sent, promises to pay.
- **Bank Transaction**: Account, date, amount, description, matched record.
- **Journal Entry**: Date, balanced debit/credit lines by account, reference, creator.
- **Cash Forecast**: Per-date projected balance, inflows, outflows, scenario, confidence.
- **Obligation**: Recurring or one-off commitment (rent, salary, loan, tax, subscription) with amount, due date, recurrence.
- **Purchasing Budget**: Weekly spending limit published by Cash-Flow for Stock.
- **Approval Request**: A one-tap owner question or approval with options, deadline, safe default and outcome.
- **Incident**: Assistant, fault type, detection method, action taken, root cause, status.
- **Learned Rule**: Assistant, rule text, trigger, source incident, approver, active flag.
- **Audit Log Entry**: Time, assistant, action, inputs, outputs, verification result.
- **Shared Events**: PO drafted; PO approved and sent; delivery received; supplier invoice posted; customer payment received; purchasing budget updated; shortfall predicted; price change detected; stock valuation mismatch.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In the 8 Chaos mode scenarios, 100% of injected faults are detected automatically, explained to the owner, and followed by a proposed learned rule.
- **SC-002**: On the sample cafe data, stockout warnings for forecast-driven stockouts arrive at least 3 days before the projected stockout in at least 90% of cases.
- **SC-003**: Simulated over the sample 3 months, stockouts and waste value are each lower than a "no assistant" baseline replay of the same data.
- **SC-004**: Projected cash shortfalls are flagged at least 14 days before the shortfall date in at least 90% of cases.
- **SC-005**: 100% of owner requests can be answered in a single tap or a single short reply.
- **SC-006**: Owner effort for the sample cafe is at most 15 minutes and at most 25 taps or replies per simulated week (excluding Chaos mode injections), presented alongside a manual bookkeeping estimate.
- **SC-007**: 100% of duplicate invoices and unbalanced entries in the test set are blocked before posting; zero reminders are sent for invoices already paid.
- **SC-008**: At least 90% of fields on clear sample invoices are captured correctly without owner correction, measured separately for English, bilingual and Arabic-only invoices, each group meeting the target.
- **SC-009**: At least 85% of sample bank transactions are matched automatically, with no incorrect auto-matches in the test set.
- **SC-010**: 100% of assistant actions appear in the audit log with a verification result, and zero actions move money.
- **SC-011**: A presenter can run any single Chaos scenario end to end, from injection to proposed rule, in under 2 minutes.
- **SC-012**: In role tests, 100% of attempts by staff or manager to perform actions outside their role are refused and logged.

## Assumptions

- Scope is the hackathon MVP ("Must have" list); P3 items (VAT summary, 13-week forecast and scenarios, supplier scorecard, Arabic) are included only if time allows. Other features in the source document (receivables promise tracking beyond reminders, full P&L and balance sheet, month-end close checklist, photo-based stock counts, waste/dead-stock suggestions beyond flagging) are secondary within the MVP.
- Owner approvals use a simulated in-dashboard chat panel plus a real Telegram bot (see Clarifications); WhatsApp, email to suppliers, and live bank feeds are not required for the MVP.
- "Sending" a purchase order or payment reminder in the MVP means recording it as sent in the approval channel; no real supplier or customer is contacted.
- Sales and bank data come from the seeded sample dataset or file upload; live connections to point-of-sale systems and banks are out of scope for the MVP.
- Default thresholds (confidence high/low, forecast error, price change 15%, stock variance tolerance, approval timeout, journal value limit) are configurable and set to sensible values for a small cafe; exact values will be tuned during planning.
- Stale bank data means older than one business day.
- Single business (tenant) for the demo, seeded with one owner, one manager and one staff user; multi-business onboarding is out of scope.
- Default chart of accounts is a standard small-business template; customisation is not required for the MVP.
- Local calendar defaults to Oman (Friday–Saturday weekend, Omani public holidays, Ramadan).
- English is the default language.
