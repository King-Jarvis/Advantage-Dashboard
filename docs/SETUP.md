# Setup

## Prerequisites

Docker with Compose, and a Google Cloud project. Nothing else — the ledger is
built in, so there is no separate finance service to run.

## 1. Configuration

```bash
cp .env.example .env
```

Edit `.env`. `BASE_URL` must exactly match the redirect URI you register with
Google, including scheme and any trailing slash. Set `DATA_ROOT` to a path
**outside the checkout** — this repository is public.

## 2. Secrets

```bash
./scripts/bootstrap.sh
```

Generates random values into `deploy/secrets/` with mode 600. That directory is
gitignored except for its `.gitkeep`. Nothing here is ever an environment
variable, so nothing appears in `docker inspect`.

## 3. Certificates — not optional

The dashboard uses Web Crypto (`crypto.subtle`), which browsers expose **only in
a secure context**: HTTPS, or `localhost`/`127.0.0.1`.

Reached over plain HTTP at a LAN address, the app loads its JavaScript and then
fails with an opaque error, because `crypto.subtle` is `undefined`. The server
is serving correctly; the browser is refusing to provide an API. No amount of
server-side debugging will explain it.

```bash
./scripts/make-local-cert.sh --dir /srv/dashboard-certs \
    --host 192.0.2.10 --host myhost.lan
```

Point `CERTS_DIR` at that directory. Two details that are easy to get wrong:

- **An IP address must be an `IP:` SAN**, not `DNS:`. Browsers will not match an
  IP against a DNS entry, and the resulting error does not point at the cause.
- **Include every name the service is reached by**, including any internal
  container hostname another service uses to reach it.

The script emits `ca.crt`. Install it once per device and there are no browser
warnings anywhere. Skip that and each device shows a warning you click through
once — the app works either way, because clicking through still yields a secure
context.

If a container ever needs to trust the CA, use `NODE_EXTRA_CA_CERTS` (or the
language equivalent) rather than disabling verification wholesale: switches like
`NODE_TLS_REJECT_UNAUTHORIZED=0` apply to every outbound connection the process
makes, not the single hop you were trying to fix.

## 4. Google Cloud

1. Create a project. Enable the **Gmail API** and the **Google Calendar API**.
2. Configure the OAuth consent screen with scopes `openid`, `email`, `profile`,
   `gmail.modify` and `calendar.events`. Write scopes are required because the
   dashboard edits, not just reads.
3. Create an **OAuth 2.0 Client ID**, type *Web application*, with redirect URI
   `${BASE_URL}/api/auth/google/callback`.
4. **Publish the app to "In production."**

   Not optional, and the most common cause of a deployment that works for a week
   and then silently stops. While publishing status is *Testing*, Google expires
   refresh tokens after **seven days**. Production with an unverified app shows a
   warning at consent and then works indefinitely for your own accounts.

5. Client ID goes in `.env`; client secret goes in
   `deploy/secrets/google_client_secret`.

## 5. First run

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Open `BASE_URL`, sign in with the local account printed by `bootstrap.sh`, then
connect a Google account from Settings.

`ALLOWED_GOOGLE_SUBS` is empty by default, so no Google account can sign in
until you list one — a deliberate default, so a fresh deployment is never open
to the internet. Your account's `sub` is written to the log on the first
rejected attempt; copy it into `.env`.

## 6. Accounts and statement history

Create your accounts under Budget → Accounts, then import statements from
Settings → Import.

The more history the better: twelve months is the target, three is the floor
below which the engine declines to suggest anything rather than guessing.

The coverage map shows which months you have. Gaps matter — the engine never
averages over a month it does not have, so filling a gap improves suggestions
more than adding further history does.
