# Architecture

## Shape

One process: the **dashboard app**, which owns the database, the ledger, the
OAuth tokens and its own scheduling.

This was originally two, with n8n scheduling and orchestrating. That never got
built, and for a while nothing scheduled anything — an edit was pushed to Google
only if something happened to call the sync, which nothing did. `scheduler.py`
is that missing half, and it lives in the app because a background thread that
pushes an edit and pulls on a timer does not need a workflow engine.

The ingest routes n8n would have used still exist and are still tested, so an
external scheduler can drive a sync if you ever want one. They are closed unless
you set an ingest key.

## Why the app owns the ledger

The alternative was running Actual Budget underneath and syncing to it. That
buys proven envelope math and bank feeds, at the cost of a second service and a
repository that cannot be run without it.

Owning the ledger means one install script genuinely works with no external
dependency, and there is one SQLite file rather than a sync boundary
between two systems. The price is that transfers, splits, reconciliation and
month rollover are ours to get right — which is why the ledger has an invariant
test suite and was built before anything that depends on it.

If automated bank feeds are ever wanted, that is a separate integration
(SimpleFIN or GoCardless) behind the same import path, not a reshaping of the
schema.

## Why the app owns OAuth

**Portability:** one `.env`, one consent flow, accounts added from the UI —
there is no second system to configure before it works. **Blast radius:**
refresh tokens live in one encrypted place rather than being duplicated into a
workflow engine that also runs user-authored code.

## Why an outbox

Editing must feel instant and also be durable. Those pull apart: a synchronous
write to Google would make every edit wait on a round trip, and would fail
entirely whenever Google was unreachable.

An edit writes to SQLite, marks the row `dirty`, and repaints in the same
request. A row is appended to `outbox`. `outbox-drain` applies pending rows
every 15 seconds and acks. On failure the row backs off and shows as pending
rather than silently reverting.

This applies to calendar and mail only. **Budget edits have no outbox** — the
ledger is local, so a budget write is simply a database write. That is a direct
benefit of owning the ledger: no round trip, no reconciliation, no divergence.

**Conflict rule** (calendar and mail): the provider is the source of truth on
every poll, *except* rows with unacked outbox entries. Without that exception a
poll that ran before your edit landed would clobber it.

## Why SQLite, and where the limit is

One writer, modest volume, and a driver in the standard library. WAL allows
concurrent readers.

The limit is a second writer. If one ever appears, that is the point to move to
Postgres — not before.

## Why polling rather than push

Google delivers push notifications only to a public HTTPS endpoint with a valid
certificate. Keeping the deployment private means polling: 30 seconds for
calendar and mail, 15 for the outbox.

The browser is still live. SSE pushes changes to open tabs the moment they
land, so the page never needs refreshing even though the data arrives on a
timer.

## The ledger

Three transaction shapes share one table, distinguished by two columns:

| Shape | `parent_id` | `transfer_id` | Category |
|---|---|---|---|
| Plain | NULL | NULL | optional |
| Transfer | NULL | paired row's id | always NULL |
| Split parent | NULL | NULL | always NULL |
| Split child | parent's id | NULL | required |

**Account balance counts rows with `parent_id IS NULL` only.** The parent
carries the money; children carry only the categorisation. Counting both is the
classic way split handling doubles someone's spending.

**A transfer is never categorised.** Moving your own money between accounts is
not expenditure, and treating it as such is the classic way a budget invents
spending that never happened.

Deletion is soft throughout. A ledger that forgets is not auditable, and undo
on a money operation is not optional.
