# The n8n side

## The one design decision worth understanding

n8n does **not** hold your Google credentials, and it never sees a Google
token. It authenticates *to the dashboard* with an ingest key and says "sync
now". The dashboard holds the refresh token, talks to Google, and answers with
counts.

The alternative — n8n's own Google OAuth credential — is the obvious approach
and the worse one:

- Your Google credentials would live in **two** places instead of one, so
  connecting an account means doing it twice and revoking means remembering
  both.
- n8n's credential encryption key sits in plaintext at `~/.n8n/config`. Read
  access to that file plus the database is read access to the credential.
- Access tokens turn up in execution logs. The workflow here sets
  `saveDataSuccessExecution: none` precisely because response bodies are the
  other way secrets leak into a log.

With the arrangement below, the worst a stolen ingest key can do is *trigger a
sync*. It cannot read your mail, your calendar, or the dashboard.

## What about signing in to n8n itself with Google?

Not available. n8n 2.34.6 does ship OIDC, but it is an enterprise feature
(`sso.ee`, gated on `feat:oidc`), so the community build refuses it. Options,
in order of how much they are worth:

1. **Leave it.** You sign in to n8n rarely; the dashboard is the thing you open
   daily, and that already has Google sign-in.
2. Put n8n behind the reverse proxy with access control there.
3. Pay for an n8n enterprise licence, only if you want SSO for other reasons.

This does not affect the sync in any way — that is machine-to-machine and uses
the ingest key, not a human login.

## Setup

**1. Generate an ingest key** and give it to both sides:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set `INGEST_KEY` in the dashboard's environment. Then in n8n, set two
environment variables (Settings → Variables, or the container's env):

| Variable | Value |
|---|---|
| `DASHBOARD_URL` | `https://dashboard.example.lan:8766` |
| `DASHBOARD_INGEST_KEY` | the key you just generated |

They are environment variables rather than values typed into the node so the
key does not end up in the workflow JSON — which is the file you would push to
a repository.

**2. Connect Google in the dashboard**, not in n8n: Settings → Credentials →
client ID and secret → Check connection → Connect account.

**3. Import** `google-sync.json` (n8n → Workflows → Import from file) and
activate it.

## Checking it works

Press **Sync now** on the account in dashboard Settings. It reports the number
of events and messages written, or the failure. That is the same code path the
schedule uses, so a green result there means the workflow will work.

To test the workflow's own door:

```bash
curl -sS -X POST $DASHBOARD_URL/api/sync/google -H "X-Ingest-Key: $INGEST_KEY" -d '{}'
```

## What the workflow does when it fails

It writes the failure back to `/api/ingest/sync`, which is what makes a stopped
feed *visible*. Without that, a sync that has been failing for a week looks
exactly like a quiet week — the screen keeps showing the last data it received
and nothing says it is stale.

A partial failure (one account of two) returns HTTP 200 with the detail in the
body, deliberately: failing the whole call would make the scheduler retry the
account that already succeeded.
