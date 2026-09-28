# Contract: Telegram Bot

Library: `python-telegram-bot` v21, long polling (research R13). The bot is a second front end to `ApprovalService`; it owns no business state.

## Linking

1. User opens dashboard profile → `POST /me/telegram-link-code` → code (8 chars, 15-min expiry).
2. User sends `/start <code>` to the bot → chat linked to that user (one chat ↔ one user). Reply in the user's language.
3. Unlinked chats get only a "please link your account" reply; no business data is sent to them.

## Commands

| Command | Role | Reply |
|---|---|---|
| `/start <code>` | any | link account |
| `/status` | manager | health strip (stock, lowest cash point, % reconciled) with data freshness |
| `/pending` | staff | open requests for this user's role |
| `/lang en\|ar` | any | switch message language |
| Photo / PDF upload | manager | treated as `POST /documents` via channel `telegram`; reply "Received, reading…" then result |
| Photo with `/delivery <po>` caption | staff | attach delivery photo to PO |

## Outgoing approval message

```
<text in user's language>
[Option 1] [Option 2] [Option 3]        ← inline keyboard; callback_data = "ar:<request_token>:<option_key>"
```

- `request_token` is opaque (not the DB id) and bound to the request.
- On callback: bot calls `ApprovalService.resolve(token, option_key, user)`. Results:
  - `resolved` → edit message to show the chosen option, who chose it and when.
  - `already_resolved` → edit message to "Already answered in <channel> by <user>: <option>" (first answer wins).
  - `permission_denied` → answer callback with a refusal notice; audit-logged.
- When a request is resolved in the dashboard, the bot edits every copy it sent to show the resolution.
- "Edit" options open a short follow-up question (single reply) or deep-link to the dashboard page.

## Failure handling

- Send failure (network, blocked bot) → `telegram_send_failed` audit entry; request remains open in the dashboard chat panel (FR-010a); retried on next scheduler tick.
- Duplicate callbacks (Telegram retries) are idempotent by `(callback_query_id)`.
