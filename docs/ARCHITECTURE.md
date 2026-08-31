# Architecture

## Shape

Three processes: the **dashboard app** (owns the database and the OAuth tokens), **n8n**
(scheduling and orchestration), and **Actual Budget** behind a REST wrapper.

The app is the only thing that touches the database. n8n never holds Google credentials —
it asks the app for a short-lived access token per run. The app never talks to n8n except
by being called.

## Why the app owns OAuth

Two reasons. **Portability:** one `.env`, one consent flow, accounts added from the UI —
there is no second system to configure before the thing works. **Blast radius:** refresh
tokens live in one place, encrypted, rather than being duplicated into a workflow engine
that also runs arbitrary user-authored code.

## Why an outbox

Editing has to feel instant and also has to be durable. Those pull apart: a synchronous
write to Google would make every edit wait on a network round trip, and would fail
entirely when n8n is down.

So an edit writes to SQLite, marks the row `dirty`, and repaints in the same request. A row
is appended to `outbox`. `outbox-drain` pulls pending rows every 15 seconds, applies them,
and acks. On failure the row backs off and shows as pending in the UI rather than silently
reverting.

**Conflict rule:** the provider is the source of truth on every poll, *except* for rows
with unacked outbox entries. Without that exception a poll that ran before your edge landed
would clobber it.

## Why SQLite, and where the limit is

One writer (the app), tiny volumes, and a dependency-free driver in the standard library.
WAL mode allows concurrent readers.

The limit is a second writer. If something else ever needs to write — a second app
instance, or Grafana doing more than reading — that is the point to move to Postgres, not
before.

## Why polling rather than push

Google delivers push notifications only to a public HTTPS endpoint with a valid
certificate. Keeping the deployment private means polling: 30 seconds for calendar and
mail, 60 for budget, 15 for the outbox.

The browser is still live. SSE pushes changes to open tabs the moment they land, so the
page never needs refreshing even though the data underneath arrives on a timer.

If sub-second inbound ever matters, a tunnel providing public HTTPS is the upgrade and only
the trigger nodes change.

## Data separation

`transactions` mirrors Actual and is the live ledger. `history_txns` holds imported
statement history from before the budget's start date and never reaches Actual.

They are separate tables on purpose. Merging them would make it impossible to tell what
Actual believes from what was inferred from a statement, and the budget engine needs to
know the difference.
