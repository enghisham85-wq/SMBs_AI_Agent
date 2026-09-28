# Small Business Agent Suite - Functional Specification

Stock Agent, Cash-Flow Agent and Accountant Agent, working together under one self-correcting harness.

## Overview

### Problem

Small businesses (cafes, restaurants, retail shops, workshops, clinics) lose money in three predictable ways:

- They run out of fast-moving stock, or over-order slow stock that expires or ties up cash.
- They discover cash shortfalls too late to act (salary day, rent day, a big supplier payment).
- Their books are behind, wrong, or both, so decisions are made on bad numbers and tax filings are rushed.

The owner usually has no finance team, and existing tools only report problems after they happen.

### Solution

Three cooperating AI agents that share one ledger and one harness:

| Agent | Core job | Key question it answers |
|---|---|---|
| Stock Agent | Keep the right stock at the right time | "What do I need to order, and when?" |
| Cash-Flow Agent | Protect the business from running out of cash | "Will I have enough cash in 2 to 4 weeks, and what should I do now?" |
| Accountant Agent | Keep the books correct and up to date | "Are my numbers right, and what do I owe?" |

### What makes this different

Every agent is built to:

1. Think before acting (plan, check preconditions, estimate impact).
2. Verify after acting (read back results, reconcile against source data).
3. Detect its own faults (forecast drift, extraction errors, duplicates, inconsistencies).
4. Act preventively (fix the cause before the problem reaches the owner).
5. Learn from incidents (every fault produces a new rule so it does not repeat).

### Target users

- Owner or manager of a business with 1 to 50 staff.
- Uses a POS system or simple sales records, receives supplier invoices (paper, PDF, photo, WhatsApp), and has one or more bank accounts.
- Low finance knowledge; prefers WhatsApp or a simple dashboard over accounting software.

## Shared Harness (applies to all three agents)

The harness is the supervisor layer that wraps every agent action. It is the main thing to show judges.

### Action lifecycle

Every action an agent takes goes through the same pipeline:

1. **Plan** - the agent states what it intends to do and why, with the data it relied on.
2. **Precondition check** - the harness validates inputs (data freshness, required fields present, values within expected ranges, no conflicting pending actions).
3. **Risk classification** - the action is tagged as:
   - Read-only (no approval needed)
   - Reversible write (auto-execute, can roll back)
   - Irreversible or external (sending a PO, a payment reminder, a filing) - needs owner approval unless an explicit auto-approve rule exists
4. **Pre-mortem (irreversible actions only)** - a verifier model asks "how could this go wrong?" and checks for the listed failure modes before execution.
5. **Execute** - the action runs, optionally in dry-run mode.
6. **Post-condition verification** - the harness reads back the result and confirms it matches the intent (record exists, totals match, no duplicates created).
7. **Rollback or escalate** - if verification fails, the harness rolls back reversible actions, retries once with corrected input, then escalates to the owner.
8. **Log** - every step is written to the Incident and Audit Log.

### Confidence scoring

- Every extraction, forecast and classification carries a confidence score (0 to 1).
- Each agent has thresholds:
  - Above high threshold: act automatically.
  - Between thresholds: act, but flag for review in the daily digest.
  - Below low threshold: do not act; ask the owner a specific question.
- Thresholds adjust based on the agent's recent accuracy (see Self-Calibration).

### Verifier model

- A second, independent model call reviews high-impact outputs (purchase orders, payment reminders, journal entries above a value limit, tax figures).
- The verifier sees the inputs and the proposed output but not the primary agent's reasoning, so it cannot simply agree with it.
- Disagreement between primary and verifier triggers escalation, not a silent retry.

### Self-calibration

- Each agent tracks its own error rate over time (forecast error, extraction corrections made by the owner, reconciliation mismatches).
- When error rises above a threshold, the agent:
  - Lowers its confidence and auto-approve limits.
  - Falls back to a simpler, safer method.
  - Reports the degradation and its suspected cause to the owner.
- When accuracy recovers, limits are restored gradually.

### Incident and learned-rule system

- Every detected fault creates an incident record: what happened, how it was detected, what was done, root cause.
- After resolution, the agent proposes a **learned rule** (for example: "Supplier X invoices use DD/MM format; never parse as MM/DD").
- The owner approves or rejects the rule. Approved rules become new preconditions or checks.
- Rules are visible and editable in the dashboard.

### Human-in-the-loop channel

- Approvals, questions and alerts go to the owner through WhatsApp (or Telegram / email) and the dashboard.
- Every request is specific and answerable in one tap: "Approve PO to Al Noor Dairy for 40 L milk, OMR 36.000? [Approve] [Edit] [Reject]".
- If the owner does not respond within a set time, the agent follows a defined safe default (usually: do nothing irreversible, re-ask, escalate urgency).

### Chaos mode (demo and testing)

- A control panel lets a user inject faults: wrong prices, duplicate invoices, missing sales data, bank feed gaps, conflicting records, sudden demand spikes.
- The purpose is to demonstrate detection, explanation, rollback and learned-rule creation live.

## Shared Data Model

All three agents read from and write to one shared store so their numbers always agree.

| Entity | Key fields | Written by |
|---|---|---|
| Item | id, name, unit, category, shelf life, reorder point, safety stock, preferred supplier | Stock |
| Stock Level | item, quantity, location, last counted, last updated | Stock |
| Stock Movement | item, type (sale, purchase, waste, adjustment, transfer), qty, date, source | Stock, Accountant |
| Supplier | id, name, contact, lead time (stated and observed), payment terms, reliability score | Stock, Accountant |
| Purchase Order | id, supplier, lines, status, expected date, approved by | Stock |
| Sale | date, items, amount, payment method, source (POS, manual) | Imported |
| Invoice (payable) | supplier, number, date, due date, lines, VAT, total, status, source file | Accountant |
| Invoice (receivable) | customer, number, date, due date, total, status | Accountant |
| Bank Transaction | account, date, amount, description, matched record | Imported, Accountant |
| Journal Entry | date, lines (account, debit, credit), reference, created by | Accountant |
| Cash Forecast | date, projected balance, inflows, outflows, confidence | Cash-Flow |
| Obligation | type (rent, salary, loan, tax, subscription), amount, due date, recurrence | Cash-Flow, Accountant |
| Incident | agent, type, detected by, action taken, root cause, status | Harness |
| Learned Rule | agent, rule text, trigger, source incident, approved by, active | Harness |
| Audit Log | timestamp, agent, action, inputs, outputs, verification result | Harness |

## Stock Agent

### Purpose

Keep stock levels right: no stockouts on items that sell, minimal waste and dead stock, and purchase orders placed at the right time with the right supplier.

### Inputs

- Sales data from POS (API, CSV export, or daily manual entry).
- Current stock counts (initial count, periodic counts, photo of a shelf or fridge).
- Recipes or bills of materials (for cafes and restaurants: 1 latte = 18 g coffee + 200 ml milk).
- Supplier list with lead times, minimum order quantities and prices.
- Calendar context: weekends, public holidays, Ramadan, school holidays, local events.
- Waste and spoilage records.
- Budget constraint from the Cash-Flow Agent.

### Core functions

1. **Stock tracking**
   - Deducts stock automatically from sales using recipes or item mappings.
   - Adds stock when deliveries are received (confirmed against the PO and supplier invoice).
   - Records waste, spoilage and manual adjustments with reasons.

2. **Demand forecasting**
   - Forecasts daily demand per item for the next 14 to 30 days.
   - Uses day-of-week patterns, recent trend, seasonality and calendar events.
   - Produces a forecast range (low, expected, high), not a single number.

3. **Reorder planning**
   - Calculates days of cover for each item: current stock divided by forecast daily demand.
   - Triggers a reorder when days of cover falls below supplier lead time plus safety buffer.
   - Recommends order quantity considering minimum order quantity, shelf life, storage space and the cash budget.
   - Groups items by supplier into one purchase order.

4. **Purchase order management**
   - Drafts purchase orders and sends them for owner approval (or auto-approves within set limits).
   - Sends approved POs to suppliers by WhatsApp or email.
   - Tracks expected delivery dates and flags late deliveries.

5. **Delivery receiving**
   - Owner or staff confirms the delivery by photo or checklist.
   - The agent compares delivered quantities and prices against the PO and flags differences.

6. **Waste and dead-stock management**
   - Tracks expiry dates and flags items at risk of expiring before they sell.
   - Suggests actions: use in specials, discount, stop reordering.
   - Identifies items that have not sold in N days.

7. **Supplier performance**
   - Records stated versus actual lead time and price changes.
   - Maintains a reliability score for each supplier.

### Self-checks (fault detection)

| Check | How it works | Action on failure |
|---|---|---|
| Forecast accuracy | Each morning compares yesterday's forecast with actual sales per item | If error exceeds threshold, lower confidence, switch to a safer method (average of last 4 same weekdays), report cause |
| Negative or impossible stock | Stock level below zero or above storage capacity | Flag missing sales mapping or unrecorded delivery; request a count |
| Book vs physical count | Compares counted stock with calculated stock | If variance exceeds tolerance, investigate (missing waste record, recipe error, theft) and adjust with reason |
| Price sanity | New supplier price compared with history | If price change exceeds threshold (for example 15%), hold the PO and ask |
| Duplicate PO | Same supplier, same items, open PO already exists | Block and merge or ask |
| Unit mismatch | Order in kg but stock in g, or pack size changed | Block and ask for confirmation |
| Sales data gap | No sales received for an open business day | Pause forecasting updates for that day and alert |

### Preventive actions

- Orders earlier from suppliers whose observed lead time is longer than stated.
- Raises safety stock automatically before known peak periods (weekends, holidays, Ramadan evenings).
- Warns about an upcoming stockout 3 or more days before it happens, not on the day.
- Reduces orders of items trending down before they become dead stock.
- Coordinates with the Cash-Flow Agent: if cash is tight, prioritises high-margin fast movers and delays non-critical orders.

### Outputs

- Daily stock status: items at risk, items to reorder, items at risk of expiry.
- Draft and sent purchase orders.
- Weekly waste and variance report.
- Supplier scorecard.

### Owner interactions (examples)

- "Milk will run out Thursday evening. Order 40 L from Al Noor Dairy (arrives Wednesday), OMR 36.000? [Approve] [Edit]"
- "Croissant waste was 22% this week. Suggest reducing Saturday bake by 15."
- "Supplier price for coffee beans rose 18%. I held the order. Continue or find alternative?"

## Cash-Flow Agent

### Purpose

Make sure the business never runs out of cash unexpectedly. Forecast the cash position, warn early, and take or recommend actions to protect liquidity.

### Inputs

- Current bank balances (bank feed, CSV statement upload, or manual entry).
- Expected inflows: sales forecast (from Stock Agent demand forecast and sales history), receivables with due dates (from Accountant Agent).
- Expected outflows: payables with due dates, planned purchase orders (from Stock Agent), recurring obligations (rent, salaries, loans, utilities, subscriptions), tax payments.
- Owner-defined minimum cash buffer.

### Core functions

1. **Cash position**
   - Shows current cash across accounts and cash on hand.
   - Shows committed outflows for the next 7 days.

2. **Cash forecasting**
   - Rolling daily forecast for 30 days and weekly forecast for 13 weeks.
   - Three scenarios: expected, pessimistic, optimistic.
   - Highlights the lowest projected balance and the date it occurs.

3. **Shortfall detection**
   - Flags any day where the projected balance falls below the minimum buffer.
   - Calculates the size of the gap and how many days remain to act.

4. **Action planning**
   - Proposes a ranked list of actions to close a gap, with impact and risk for each:
     - Chase specific overdue receivables.
     - Delay a non-critical supplier payment within its terms.
     - Reduce or delay a purchase order (in coordination with the Stock Agent).
     - Move a planned expense.
     - As a last resort, suggest short-term financing.
   - Shows the forecast after each proposed action so the owner sees the effect.

5. **Receivables collection**
   - Sends polite, escalating payment reminders for overdue customer invoices (with owner approval rules).
   - Tracks promises to pay and follows up.

6. **Payables scheduling**
   - Recommends when to pay each supplier invoice: early to capture a discount, on time, or at the end of terms if cash is tight.
   - Never recommends paying late beyond terms without explicit owner instruction.

7. **Budget signal to other agents**
   - Publishes a weekly purchasing budget that the Stock Agent must respect.

### Self-checks (fault detection)

| Check | How it works | Action on failure |
|---|---|---|
| Forecast vs actual balance | Each day compares yesterday's projected closing balance with the actual bank balance | If variance exceeds threshold, identify which inflow or outflow was wrong, correct the model, report |
| Bank feed freshness | Checks when balances were last updated | If stale, mark forecast as low confidence and ask for a statement |
| Missing recurring obligation | Expected recurring payment (rent, salary) not in forecast or not seen in bank | Ask owner to confirm or add it |
| Double counting | Same payable appears as both a PO commitment and an invoice | Merge using the PO reference |
| Reminder safety | Before sending a payment reminder, verifies the invoice is still unpaid by checking the latest bank transactions and Accountant records | Cancel the reminder if payment already received |
| Unrealistic assumption | Forecast relies on inflow much larger than historical pattern | Flag and use pessimistic scenario as primary |

### Preventive actions

- Warns 2 to 4 weeks before a shortfall, while there is still time to act.
- Starts collection reminders before invoices become seriously overdue for customers who have paid late before.
- Tightens the Stock Agent's purchasing budget automatically when the pessimistic scenario breaches the buffer.
- Builds a reserve ahead of known large payments (quarterly rent, annual license, VAT return).

### Outputs

- Daily cash summary: today's balance, lowest point in the next 30 days, alerts.
- 13-week cash forecast with scenarios.
- Shortfall action plan.
- Receivables ageing and collection status.
- Payment schedule recommendation.

### Owner interactions (examples)

- "Heads up: cash drops to OMR 450 on 28 Oct (buffer is OMR 1,500) because rent and salaries fall in the same week. Options: [Chase 3 overdue invoices: +OMR 1,200] [Delay coffee order 5 days: +OMR 380] [See plan]"
- "Customer Al Mazaya paid invoice INV-104 today, so I cancelled the reminder scheduled for tomorrow."
- "Yesterday's actual balance was OMR 600 lower than forecast. Cause: unplanned equipment repair. Added it to the log; forecast updated."

## Accountant Agent

### Purpose

Keep the books accurate and current with minimal owner effort: capture documents, record transactions, reconcile the bank, and prepare figures for VAT and management reporting.

### Inputs

- Supplier invoices and receipts (PDF, photo, WhatsApp forward, email).
- Sales data (POS daily totals, customer invoices).
- Bank transactions (feed or statement upload).
- Chart of accounts (default template for small businesses, customisable).
- VAT settings (rate, registration status, filing period).
- Owner answers to classification questions.

### Core functions

1. **Document capture**
   - Receives invoices and receipts from any channel.
   - Extracts supplier, invoice number, date, due date, line items, VAT, and total.
   - Stores the original file linked to the record.

2. **Transaction recording**
   - Creates payable and receivable records.
   - Posts double-entry journal entries automatically with correct accounts and VAT split.
   - Posts daily sales summaries from POS.
   - Records stock purchases so the stock ledger and the accounts agree.

3. **Bank reconciliation**
   - Matches each bank transaction to an invoice, payment, sale deposit or expense.
   - Suggests matches with a confidence score; auto-matches above threshold.
   - Lists unmatched items for review.

4. **Expense categorisation**
   - Classifies expenses to accounts using supplier history, descriptions and learned rules.
   - Asks the owner only when confidence is low, and learns from the answer.

5. **VAT preparation**
   - Tracks input VAT and output VAT per period.
   - Produces a VAT summary ready for filing, with the supporting list of invoices.
   - Flags invoices missing a valid VAT number or VAT breakdown.

6. **Reporting**
   - Profit and loss, balance sheet, and cash summary for any period.
   - Gross margin by category (using Stock Agent cost data).
   - Month-end close checklist.

7. **Accounts payable and receivable ageing**
   - Shares due dates and ageing with the Cash-Flow Agent.

### Self-checks (fault detection)

| Check | How it works | Action on failure |
|---|---|---|
| Extraction arithmetic | Line totals add up to subtotal; subtotal plus VAT equals total; VAT equals rate times base | Re-extract; if still wrong, ask owner with the specific field highlighted |
| Duplicate invoice | Same supplier and invoice number, or same supplier, amount and date | Block posting, show both, ask |
| Date sanity | Invoice date in the future, far in the past, or ambiguous DD/MM vs MM/DD | Apply supplier's learned format; otherwise ask |
| Balanced entries | Every journal entry has debits equal to credits | Block posting |
| Trial balance | Daily check that the ledger balances | Locate and quarantine the entry causing imbalance |
| Bank balance agreement | Ledger bank balance equals actual bank balance after reconciliation | List unexplained difference and investigate |
| Stock ledger agreement | Inventory account value agrees with Stock Agent valuation | Flag mismatch to both agents |
| PO match | Supplier invoice matches PO and received quantities (three-way match) | Hold for review if quantities or prices differ |
| Supplier VAT validity | VAT number present and in valid format when VAT is charged | Flag before VAT period closes |

### Preventive actions

- Flags missing documents (a bank payment with no invoice) before the VAT period closes, not after.
- Reminds the owner of filing deadlines with the draft figures already prepared.
- Detects recurring categorisation corrections and proposes a rule so the same correction is not needed again.
- Warns when a supplier invoice arrives without a matching PO or delivery, which may indicate an error or fraud.

### Outputs

- Up-to-date ledger and journal.
- Reconciliation status (percent matched, unmatched items).
- VAT summary per period.
- Monthly P&L and balance sheet.
- Review queue: items needing owner answers.

### Owner interactions (examples)

- "I received two invoices from Gulf Packaging with number GP-5521, same amount. Posted the first, held the second. Is it a duplicate? [Yes, discard] [No, both valid]"
- "This receipt's total (OMR 52.500) does not equal its lines plus VAT (OMR 50.400). Please check the photo. [Use 52.500] [Use 50.400] [Retake photo]"
- "VAT period ends in 9 days. 4 bank payments have no invoice. [See list]"

## How the Agents Work Together

### Shared events

| Event | Produced by | Consumed by | Effect |
|---|---|---|---|
| PO drafted | Stock | Cash-Flow | Checked against budget; added as committed outflow |
| PO approved and sent | Stock | Cash-Flow, Accountant | Outflow scheduled; open PO available for invoice matching |
| Delivery received | Stock | Accountant | Enables three-way match |
| Supplier invoice posted | Accountant | Cash-Flow, Stock | Due date scheduled; actual price updates item cost |
| Customer payment received | Accountant (from bank) | Cash-Flow | Cancel reminders; update forecast |
| Purchasing budget updated | Cash-Flow | Stock | Stock reprioritises orders |
| Shortfall predicted | Cash-Flow | Stock, owner | Stock defers non-critical orders; owner gets action plan |
| Price change detected | Stock or Accountant | Both, Cash-Flow | Cost and forecast updated |
| Stock valuation mismatch | Accountant | Stock | Joint investigation incident |

### Conflict resolution

- If two agents disagree (for example, Stock wants to order and Cash-Flow says there is no budget), the harness presents both positions with numbers to the owner and a recommended choice.
- Stockout of a critical item (defined per item) outranks budget, but the owner is notified.
- No agent may silently override another agent's record; changes go through events and are logged.

### End-to-end example

1. Stock Agent forecasts milk will run out Thursday and drafts a PO for OMR 36.000.
2. Cash-Flow Agent checks: pessimistic forecast shows cash dips below buffer next week. Milk is critical, so it approves but asks Stock to delay a non-critical packaging order by 5 days.
3. Owner approves both in two taps.
4. Delivery arrives with 36 L instead of 40 L; Stock records the shortfall and notes the supplier.
5. Supplier invoice arrives for 40 L. Accountant's three-way match catches the difference and holds the invoice.
6. Owner confirms; Accountant posts the corrected amount; Cash-Flow updates the payment; Stock lowers the supplier's reliability score.
7. Harness logs the incident and proposes a rule: "Always wait for delivery confirmation before posting invoices from this supplier."

## Dashboard

### Home

- Health strip: Stock (OK / warnings), Cash (lowest point next 30 days), Books (percent reconciled, items to review).
- Today's decisions needed (approvals and questions).
- Alerts sorted by urgency.

### Stock view

- Items with days of cover, reorder status and expiry risk.
- Open POs and expected deliveries.
- Forecast vs actual chart per item.

### Cash view

- 30-day daily and 13-week forecast chart with scenarios and buffer line.
- Shortfall plan with simulated effect of each action.
- Receivables and payables ageing.

### Books view

- Review queue, reconciliation status, P&L, VAT summary.
- Document inbox with extraction confidence.

### Harness view (key for the demo)

- Live action pipeline: plan, check, execute, verify.
- Incident log with detection method and resolution.
- Learned rules (active, pending approval).
- Agent accuracy over time and current confidence limits.
- Chaos mode control panel.

## Demo Scenarios (Chaos Mode)

Each scenario injects a fault and shows detection, explanation, correction and a learned rule.

1. **Duplicate supplier invoice** - Accountant blocks the second posting; Cash-Flow does not double-count the payment.
2. **Supplier price spike** - Stock holds the PO, asks the owner, proposes an alternative supplier.
3. **Wrong date format on invoice** - Accountant detects the impossible due date, corrects it using the supplier rule, and creates the rule if it did not exist.
4. **Sudden demand spike** (event in the area) - Stock's forecast error jumps; it lowers confidence, switches method, raises safety stock, and reports.
5. **Customer pays before reminder** - Cash-Flow's pre-send check sees the payment and cancels the reminder.
6. **Missing bank feed day** - Cash-Flow marks the forecast low confidence and asks for a statement rather than guessing.
7. **Short delivery vs full invoice** - three-way match catches it across Stock and Accountant.
8. **Cash crunch** - Cash-Flow predicts a shortfall 3 weeks out, tightens Stock's budget, and presents a ranked action plan.

## Non-Functional Requirements

### Safety and control

- No money is moved by any agent. Payments are recommended and scheduled only; the owner executes them.
- External messages (POs, reminders) require approval unless the owner sets an explicit auto-approve rule with a value limit.
- Every action is logged and reversible where possible.

### Accuracy

- Extraction and matching show confidence and source on every record.
- Figures shown to the owner always state the data freshness (for example "bank data as of today 08:00").

### Privacy and security

- Business data stays in the business's own tenant.
- Role-based access: owner, manager, staff (staff can record deliveries and waste only).
- Bank credentials are never stored by the agents; read-only feeds or uploaded statements only.

### Usability

- Every owner request can be answered in one tap or one short reply.
- Arabic and English interface and messages.
- Works on mobile.

### Localisation

- Currency with 3 decimal places (OMR) and configurable for other GCC currencies.
- VAT rate and filing period configurable (Oman default 5%).
- Local calendar: weekends, public holidays, Ramadan.

## MVP Scope for the Hackathon

### Must have

1. Shared data model seeded with a realistic sample cafe (3 months of sales, 20 items, 5 suppliers, bank transactions).
2. Stock Agent: stock tracking, forecasting, reorder, PO drafting, forecast self-check, price check.
3. Cash-Flow Agent: 30-day forecast, shortfall detection, action plan, reminder pre-send check.
4. Accountant Agent: invoice extraction from image/PDF, arithmetic and duplicate checks, posting, basic bank matching.
5. Harness: action pipeline, verification, rollback, incident log, learned rules.
6. Dashboard with Harness view and Chaos mode.
7. WhatsApp-style approval flow (real or simulated).

### Nice to have

1. VAT summary.
2. 13-week forecast and scenarios.
3. Supplier scorecard.
4. Arabic interface.

### Success metrics to present

- Stockouts prevented and waste reduced (simulated on sample data).
- Days of warning before cash shortfalls.
- Percent of faults detected automatically in chaos scenarios.
- Owner time per week (number of taps or questions) compared with manual bookkeeping.
