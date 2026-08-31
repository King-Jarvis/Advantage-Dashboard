# API

Two unrelated identities. A **browser session** (cookie plus CSRF token) reaches view and
edit routes. The **ingest key** (`X-Ingest-Key`) reaches ingest, outbox and token routes and
nothing else. Compromising one grants nothing of the other.

## Ingest — ingest key

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/ingest/events` | Calendar events, upserted by `(account, source_uid)` |
| POST | `/api/ingest/messages` | Mail with importance, upserted by `(account, source_uid)` |
| POST | `/api/ingest/transactions` | Transactions from Actual, upserted by `actual_id` |
| POST | `/api/ingest/budget` | Envelope amounts for a month |
| GET | `/api/sync/{source}` | Cursor for delta queries |

All ingest is idempotent. Re-posting the same payload repairs rather than duplicates.

## Outbox — ingest key

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/outbox/pending` | Queued local edits awaiting push |
| POST | `/api/outbox/ack` | Report success or failure per row |

## Tokens — ingest key

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/google/token?account=` | Short-lived access token, refreshed as needed |

Refresh tokens never leave the app.

## Edit — session + CSRF

| Method | Path |
|---|---|
| PATCH | `/api/edit/event/{id}` |
| PATCH | `/api/edit/message/{id}` |
| PATCH | `/api/edit/transaction/{id}` |
| PATCH | `/api/edit/budget/{month}/{category}` |
| POST | `/api/edit/transaction` |

Every edit writes locally, returns the updated row, and enqueues an outbox entry.

## Import — session + CSRF

| Method | Path |
|---|---|
| POST | `/api/import/upload` |
| GET | `/api/import/batch/{id}` |
| POST | `/api/import/batch/{id}/commit` |
| GET | `/api/import/coverage` |

Nothing is written to the ledger until commit.

## View — session

`/api/view/agenda` · `/api/view/inbox` · `/api/view/budget` · `/api/view/status` ·
`/api/stream` (SSE)

`/api/view/status` reports each source's last sync and error, so a stale source is visible
rather than silently absent.

## Auth

`POST /api/auth/login` · `POST /api/auth/logout` · `GET /api/auth/google/start` ·
`GET /api/auth/google/callback` · `GET /api/auth/google/connect`
