# Contract: LLM Call Schemas

All LLM calls are made from LangGraph nodes (research R17) through one `LLMClient` wrapper around the official `anthropic` SDK (research R4): model `claude-opus-5`, structured output via `client.messages.parse()` with the Pydantic models below, `stop_reason` checked (refusal → treated as low confidence + owner question), server-side refusal fallback enabled, stable prefix cached. Tests use record/replay fixtures keyed by request hash (R15).

## 1. `InvoiceExtraction` (Accountant, effort `high`)

Input: document block (PDF) or image block, then text instructions including the supplier hint list and active parsing-hint rules.

```text
InvoiceExtraction
  language: "en" | "ar" | "bilingual"
  supplier_name: Field[str]            # as printed
  supplier_name_alt: Field[str] | null # other-language version on bilingual invoices
  supplier_vat_number: Field[str] | null
  invoice_number: Field[str]
  invoice_date: Field[date]            # ISO; raw_text keeps printed form
  due_date: Field[date] | null
  currency: Field[str]
  lines: list[Line]                    # description, qty, unit, unit_price, vat_rate, line_total (each a Field)
  subtotal: Field[decimal]
  vat_amount: Field[decimal]
  total: Field[decimal]
  total_alt: Field[decimal] | null     # other-language total on bilingual invoices
  date_format_observed: "DMY" | "MDY" | "YMD" | "ambiguous"
  notes: list[str]

Field[T] = { value: T | null, raw_text: str | null, confidence: float 0..1 }
```

Post-processing (code, not LLM): digit normalisation, arithmetic check, date sanity, duplicate check, supplier alias match, bilingual agreement, confidence penalties (R6, R7).

## 2. `VerifierVerdict` (Harness, effort `high`)

Input: `VerificationPacket` = action type, source inputs (e.g. document, PO, delivery, history summary), proposed output, applicable active rules. **Never** includes the primary call's reasoning or messages (FR-003).

```text
VerifierVerdict
  agrees: bool
  issues: list[{ field_or_aspect: str, problem: str, severity: "low"|"medium"|"high", suggested_value: str | null }]
  failure_modes_checked: list[str]    # pre-mortem list for irreversible actions
  confidence: float 0..1
```

`agrees = false` or any `high` issue → escalate (no silent retry).

## 3. `ExpenseClassification` (Accountant, effort `low`)

Input: bank transaction or invoice line description, supplier history summary, chart of accounts, active classification rules.

```text
ExpenseClassification
  account_code: str        # must exist in chart of accounts (validated in code)
  confidence: float 0..1
  rule_applied: str | null # learned rule id if one matched
  rationale: str           # one sentence, shown to owner when asking
```

## 4. `OwnerMessage` (any agent, effort `medium`)

Input: structured facts (numbers already computed by code) + option list. Output: wording only — the model never invents figures; code validates that every number in the text appears in the facts.

```text
OwnerMessage
  text_en: str   # ≤ 280 chars
  text_ar: str   # ≤ 280 chars
```

## 5. `IncidentAnalysis` and `RuleProposal` (Harness, effort `medium`)

Input: incident record, checks that fired, owner resolution, related records.

```text
IncidentAnalysis
  root_cause: str
  category: "data_format" | "data_gap" | "duplicate" | "pricing" | "model_error" | "external" | "other"

RuleProposal
  rule_text_en: str
  rule_text_ar: str
  kind: "precondition" | "check" | "parsing_hint" | "classification" | "policy"
  trigger: object          # validated against per-kind JSON schema before saving
  expected_effect: str
```
