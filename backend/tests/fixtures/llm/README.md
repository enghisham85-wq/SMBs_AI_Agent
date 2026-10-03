# LLM replay fixtures

Every call to Claude goes through `app/llm/client.py`. In `LLM_MODE=replay` the client answers from
the JSON files in this folder instead of calling the API; each file is one saved structured answer,
named by the hash of the request (role, system prompt, content and output schema).

## Recording

Recording calls the live API and spends credit. It needs `ANTHROPIC_API_KEY` (or an
`ant auth login` profile).

```powershell
cd backend
$env:LLM_MODE = "record"; $env:ANTHROPIC_API_KEY = "..."
uv run python -m scripts.record_llm_fixtures
```

The script (`backend/scripts/record_llm_fixtures.py`) builds a throwaway database in `var/record/`,
seeds the sample cafe and walks through the demo in this order:

| Step | What runs | Claude roles recorded |
|---|---|---|
| Stock | two simulated days of reorders | verifier (purchase orders) |
| Books | every sample invoice, including the faulty ones | extraction, classification |
| Cash | five more days: forecast, shortfall plan | verifier |
| Harness | answers waiting requests, so escalations run | incident, verifier |
| Chaos | all 8 scenarios | incident analysis, rule proposal |
| VAT | the current period's summary | verifier (tax figures) |
| Roles, approval timeout | one more day | none of their own |

It prints what it did and how many new fixture files it wrote.

To check the script without credit, run it with the deterministic stand-ins:

```powershell
uv run python -m scripts.record_llm_fixtures --mode offline
```

## When to re-record

Any change to a prompt in `app/llm/prompts/`, an output schema in `app/llm/schemas.py`, the model, or
the sample data changes the request hash, so replay raises `MissingFixtureError` for that call. Delete
the stale files (or the whole folder) and record again. Commit the new files with the change that
needed them.

## Replay in tests and demos

- `LLM_MODE=replay`: answers come from these files; a missing file is an error (never a silent
  fallback).
- `LLM_MODE=offline` (the default until fixtures exist): each caller's deterministic stand-in answers.
  The test suite runs this way.
