Propose ONE precise learned rule that would have prevented this incident, in English and Arabic.

The owner approves, edits or rejects it, and code applies it, so the trigger must match the schema
for its kind exactly:
- precondition / check: {"action_type": str, "field": dotted path in the action inputs, "op": lt|le|gt|ge|eq|ne,
  "value": number|string|boolean, "when": optional {field: value}}. A failing check holds the action and asks the owner.
- parsing_hint: {"supplier_id": str, "date_format": "DMY"|"MDY"} or {"supplier_id": str, "hint": str}
- classification: {"supplier_id": str, "account_code": str}
- policy: {"action_type": str, "require_approval": true}

Example: "Supplier X invoices use DD/MM format; never parse as MM/DD" ->
kind "parsing_hint", trigger {"supplier_id": "<id>", "date_format": "DMY"}.

Keep the rule text short and specific (name the supplier, item or action). Use only ids and values
present in the facts. Describe the expected effect in one sentence.
