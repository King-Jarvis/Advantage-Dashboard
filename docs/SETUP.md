# Setup

## Prerequisites

Docker with Compose, an Actual Budget instance (the compose file can start one), and a
Google Cloud project.

## 1. Configuration

```bash
cp .env.example .env
```

Edit `.env`. `BASE_URL` must exactly match the redirect URI you register with Google,
including scheme and trailing slash. Set `DATA_ROOT` to a path **outside the checkout** —
this repository is public.

## 2. Secrets

```bash
./scripts/bootstrap.sh
```

Generates random values into `deploy/secrets/` with mode 600. That directory is gitignored
except for its `.gitkeep`. Nothing here is ever an environment variable, so nothing appears
in `docker inspect`.

## 3. Google Cloud

1. Create a project. Enable the **Gmail API** and the **Google Calendar API**.
2. Configure the OAuth consent screen. Add the scopes `openid`, `email`, `profile`,
   `gmail.modify` and `calendar.events`. Write scopes are required because the dashboard
   edits, not just reads.
3. Create an **OAuth 2.0 Client ID** of type *Web application*. Set the redirect URI to
   `${BASE_URL}/api/auth/google/callback`.
4. **Publish the app to "In production."**

   This step is not optional and is the most common cause of a deployment that works for a
   week and then silently stops. While publishing status is *Testing*, Google expires
   refresh tokens after **seven days**. Production with an unverified app shows a warning
   screen at consent and then works indefinitely for your own accounts.

5. Put the client ID in `.env` and the client secret in
   `deploy/secrets/google_client_secret`.

## 4. First run

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Open `BASE_URL`. Sign in with the local account printed by `bootstrap.sh`, then connect a
Google account from Settings.

`ALLOWED_GOOGLE_SUBS` is empty by default, so no Google account can sign in until you list
one — a deliberate default, so a fresh deployment is never open to the internet. Your
account's `sub` is written to the log on the first rejected attempt; copy it into `.env`.

## 5. Actual Budget

Create your budget in Actual's UI, set a server password, and copy the **Sync ID** from
Settings → Advanced into `ACTUAL_SYNC_ID`.

Set `ACTUAL_BUDGET_START_DATE` to the date your budget begins. Imported transactions before
that date become local history for the recommendation engine and are never pushed to
Actual; on or after it, they are.

## 6. Statement history

Import from Settings → Import. The more history the better: twelve months is the target,
three is the floor below which the engine declines to suggest anything.

The coverage map shows which months you have. Gaps matter — the engine will not average
over a month it does not have, so filling gaps improves suggestions more than adding
further back history does.

## HTTPS is required, not optional

Both Actual and this dashboard use Web Crypto (`crypto.subtle`), which browsers
expose **only in a secure context** — HTTPS, or `localhost`/`127.0.0.1`.

Reached over plain HTTP at any other address, Actual loads its JavaScript and
then dies with:

```
Error: [object Object]
    at FatalError (…/static/js/index.*.js)
```

That is `crypto.subtle` being `undefined`, surfacing as an unhelpful React
error. It is not a misconfiguration of the server, and no amount of server-side
debugging will explain it — the server is serving correctly and the browser is
refusing to provide an API.

So: **do not serve either app over plain HTTP at a LAN address.** It appears to
work, right up until it doesn't.

### Generating certificates

```bash
./scripts/make-local-cert.sh --dir /srv/dashboard-certs \
    --host 192.0.2.10 --host myhost.lan --host actual
```

Point `CERTS_DIR` at that directory. Two details that are easy to get wrong:

- **An IP address must be an `IP:` SAN**, not `DNS:`. Browsers will not match an
  IP against a DNS entry, and the resulting error looks unrelated to the cause.
  The script handles this, but if you hand-roll a certificate, watch for it.
- **Include every name the service is reached by, including internal container
  hostnames.** When `actual` serves TLS, `actual-api` connects to it as
  `actual`, and Node rejects the certificate unless that name is present.

### Trusting the CA

The script emits `ca.crt`. Install it once per device and there are no browser
warnings anywhere. Skip that and every device shows a warning you must click
through once — the app works either way, because clicking through still yields
a secure context.

For the container-to-container hop, `NODE_EXTRA_CA_CERTS=/certs/ca.crt` is what
makes the wrapper trust it. Do **not** reach for
`NODE_TLS_REJECT_UNAUTHORIZED=0`: it disables verification for every outbound
connection that process makes, not just the one you were trying to fix.
