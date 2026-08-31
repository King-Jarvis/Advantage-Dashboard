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

---

## Actual Budget wrapper — pinned from the running service

Verified against `jhonderson/actual-http-api:26.8.1` by reading
`/api-docs/swagger.json` from inside the container. Base path is **`/v1`** — a
detail absent from the README, and the cause of a confusing 404 if missed.

Auth header is `x-api-key`.

| Need | Endpoint |
|---|---|
| List transactions | `GET /v1/budgets/{syncId}/accounts/{accountId}/transactions` |
| Edit a transaction | `PATCH /v1/budgets/{syncId}/transactions/{transactionId}` |
| Add a transaction | `POST /v1/budgets/{syncId}/accounts/{accountId}/transactions` |
| **Import a statement** | `POST /v1/budgets/{syncId}/accounts/{accountId}/transactions/import` |
| Month envelopes | `GET /v1/budgets/{syncId}/months/{month}/categories` |
| **Set an envelope** | `PATCH /v1/budgets/{syncId}/months/{month}/categories/{categoryId}` |
| **Move money** | `POST /v1/budgets/{syncId}/months/{month}/categorytransfers` |
| Categories | `GET|POST /v1/budgets/{syncId}/categories` |
| Payees | `GET|POST /v1/budgets/{syncId}/payees` |
| Arbitrary query | `POST /v1/budgets/{syncId}/run-query` (ActualQL) |

Request bodies, as pinned:

```jsonc
// PATCH months/{month}/categories/{categoryId}
{ "category": { "budgeted": 12345, "carryover": false } }   // integer cents

// POST months/{month}/categorytransfers  — the native move-money primitive
{ "categorytransfer": { "fromCategoryId": "...", "toCategoryId": "...", "amount": 5000 } }

// POST accounts/{accountId}/transactions/import
{ "transactions": [ { "date": "2026-08-01", "amount": -1234,
                      "payee_name": "...", "imported_id": "...", "notes": "" } ],
  "dryRun": true, "defaultCleared": true, "reimportDeleted": false }
```

Two findings that shape the implementation:

- **`dryRun`** lets the import review screen ask Actual exactly what it *would*
  do before anything is committed. Use it to render the review, then re-post
  with `dryRun: false` on confirm.
- **`imported_id`** is Actual's own deduplication key. Set it to a stable hash
  of (date, amount, normalised payee) so Actual dedupes independently of our
  own duplicate detection — two mechanisms, not one.

### Authentication is conditional — read this

The api-key middleware only rejects when `NODE_ENV === 'production'`:

```js
if ((!apiKey || config.apiKey != apiKey) && config.nodeEnv == 'production') {
  res.status(403).json({"error": "Forbidden"});
}
```

Without that variable the check silently passes everything. The upstream
README's example `docker run` does not set it, so the default deployment
exposes a financial API with **no authentication at all**. The compose file
sets it and there is a test asserting a wrong key is rejected.

`authorizeRequest` is mounted on `/budgets/:budgetSyncId`, so the bare
`GET /v1/budgets` listing is **not** authenticated even with `NODE_ENV` set. It
returns budget names and sync IDs. Keep the wrapper on the internal network and
never proxy that path publicly.
