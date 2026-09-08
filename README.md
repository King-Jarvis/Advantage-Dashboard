# Personal Dashboard

One screen for what's happening, what needs your attention, and where your money is —
calendar, email triage and budget, all editable in place.

Self-hosted, portable, no telemetry. The runtime is the Python standard library plus a
single pinned dependency for encrypting OAuth tokens at rest.

## What it does

**Agenda** — events from one or more Google Calendars. Create, reschedule and delete
without leaving the page.

**Inbox** — not a mail client. Mail is scored for importance and shown as a short list of
things that actually want you. Archive, star, label, and correct the score when it's wrong;
corrections are kept separately so they survive re-classification.

**Budget** — zero-based envelope budgeting with its own ledger: accounts,
transactions, transfers, splits and reconciliation, all local. Assign and cover, or drag
money between envelopes. Import bank statements — including years of history — and get a
suggested budget drawn over your real spending.

## How the suggestion works

Every suggested figure is computed from your own statement history with deterministic
statistics: a rolling 12 covered months, outliers trimmed, classified as fixed recurring,
variable recurring, or an annual cost that should be a monthly sinking fund.

A language model writes the one-line rationale. **It never produces or adjusts a number.**
Models are unreliable at arithmetic over hundreds of rows, and a budget you cannot
reproduce or audit is worse than no budget.

Confidence is shown rather than hidden — the chart draws a band, and its width is the
confidence. Below three covered months it declines to guess instead of bluffing.

## Architecture

```
        browser
           │  HTTPS, session cookie
           ▼
┌──────────────────────────────────────────────┐
│  dashboard app                               │
│    owns the database and the refresh tokens  │
│    local write → repaint → dirty flag        │
│    background scheduler: push now, pull on   │
│                          a timer             │
└──────────────────────────────────────────────┘
           │  OAuth 2.0 + PKCE
           ▼
      Google APIs  (Gmail, Calendar)
```

One process. It is the only thing that touches the database and the only holder of refresh
tokens. An edit is written locally, repaints immediately, and is marked dirty; the
scheduler thread pushes it to Google at once and pulls fresh state on a timer. A push that
fails stays dirty and is retried, so nothing is silently lost when Google is unreachable.

There is an optional ingest key that lets an external scheduler such as n8n trigger a sync
over HTTP. It is off unless you set one, and it opens no route that can read your data.

## Getting started

Requires Python 3.11+, `git`, `openssl`, and a Google Cloud project.

```bash
curl -fsSL https://raw.githubusercontent.com/King-Jarvis/Advantage-Dashboard/main/install.sh | bash
```

That fetches the code, builds a virtualenv, generates the encryption key and a
self-signed certificate, installs a systemd **user** service, starts it, and prints the
setup code. It asks for no passwords and needs no root. Re-running it updates and
restarts without ever touching your database, keys or certificate.

Then open the printed URL, create your account, and add your Google client ID and secret
in Settings — no environment variables and no config file to edit.

Full walkthrough in [docs/SETUP.md](docs/SETUP.md), including the Google OAuth steps and
running it under systemd.

> **One trap worth knowing up front:** leave your Google OAuth app in *Testing* publishing
> status and Google expires refresh tokens after seven days, so syncing dies weekly for no
> visible reason. Publish to *In production* — unverified is fine for your own accounts.

## Security

This holds your mail, calendar and finances. See [SECURITY.md](.github/SECURITY.md) for
the threat model and the design decisions behind it.

In short: session auth with scrypt and CSRF on every mutation, a CSP with no inline
script, no markup sinks anywhere in the front end (CI enforces this), OAuth with PKCE,
and refresh tokens plus API keys encrypted at rest with a key held in a file rather than
an environment variable.

It is built to run on your own network behind your own TLS certificate. It is **not**
hardened for exposure to the public internet, and nothing here should be port-forwarded.

## Documentation

| | |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit and why |
| [BUDGET-ENGINE.md](docs/BUDGET-ENGINE.md) | The statistics, in full |
| [DESIGN.md](docs/DESIGN.md) | The interface principles |
| [THREAT-MODEL.md](docs/THREAT-MODEL.md) | What this defends against |
| [SETUP.md](docs/SETUP.md) | Step by step |
| [API.md](docs/API.md) | Endpoint reference |

## Licence

MIT — see [LICENSE](LICENSE).
