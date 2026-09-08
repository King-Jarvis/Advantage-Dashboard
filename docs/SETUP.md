# Setup

## Prerequisites

Python 3.11 or newer, and a Google Cloud project. Nothing else — the ledger is
built in, so there is no separate finance service to run.

## 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/King-Jarvis/Advantage-Dashboard/main/install.sh | bash
```

`install.sh` is idempotent: it checks Python, fetches the code into
`~/.local/share/personal-dashboard`, builds a virtualenv, generates the
encryption key and a self-signed certificate, installs a systemd **user**
service, starts it, and prints your setup code. Running it again updates and
restarts, and never overwrites your database, keys or certificate.

Override any of `DASHBOARD_HOME`, `DASHBOARD_DATA`, `DASHBOARD_PORT` or
`DASHBOARD_BIND` in the environment if the defaults do not suit. Keep both
paths **inside your home directory** — the service runs with
`ProtectSystem=strict` and a private `/tmp`, so anything elsewhere is invisible
or unwritable to it, and systemd reports that only as `203/EXEC`.

## 2. The encryption key

`install.sh` generates this for you at `~/.dashboard/token.key`, with `umask
077`, and points `TOKEN_KEY_PATH` at it. It encrypts the Google refresh tokens
and any API key at rest. A file rather than an environment variable, because an
env var is readable from `/proc/<pid>/environ` and appears in container
inspection output.

**Back it up.** Lose it and every stored credential becomes unreadable — the
app then re-prompts rather than misbehaving, but you reconnect everything.

Every other secret — Google client ID and secret, Anthropic key, ingest key —
is entered in the Settings page and stored encrypted in the database. There is
no config file to edit.

## 3. Certificates

`install.sh` already generated a self-signed certificate, so you can skip this
section unless you want one your devices trust without a warning.

TLS itself is **not** optional. The dashboard uses Web Crypto (`crypto.subtle`), which browsers expose **only in
a secure context**: HTTPS, or `localhost`/`127.0.0.1`.

Reached over plain HTTP at a LAN address, the app loads its JavaScript and then
fails with an opaque error, because `crypto.subtle` is `undefined`. The server
is serving correctly; the browser is refusing to provide an API. No amount of
server-side debugging will explain it.

```bash
./scripts/make-local-cert.sh --dir /srv/dashboard-certs \
    --host 192.0.2.10 --host myhost.lan
```

Pass the two files to `--cert` and `--key`. Two details that are easy to get wrong:

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
   `https://<host>:<port>/api/auth/google/callback` — the exact address you
   reach the dashboard on, scheme and port included. Set `BASE_URL` in the
   service environment to that same origin so the two cannot drift; a mismatch
   surfaces as Google's `redirect_uri_mismatch`, which names the symptom and
   not the cause.
4. **Publish the app to "In production."**

   Not optional, and the most common cause of a deployment that works for a week
   and then silently stops. While publishing status is *Testing*, Google expires
   refresh tokens after **seven days**. Production with an unverified app shows a
   warning at consent and then works indefinitely for your own accounts.

5. Both the client ID and the client secret are entered in the dashboard's
   Settings page after first run. The secret is encrypted before it is stored.

## 5. First run

`install.sh` printed a setup code and a URL when it finished. Open it, create
your account, then connect a Google account from Settings. The code is good for
one claim: once an account exists, the setup route is closed for good.

Lost the code, or coming back later?

```bash
systemctl --user restart personal-dashboard
journalctl --user -u personal-dashboard -n 20
```

Useful service commands:

```bash
systemctl --user status personal-dashboard
systemctl --user restart personal-dashboard
tail -f ~/.dashboard/dashboard.log
```

`ALLOWED_GOOGLE_SUBS` is empty by default, so no Google account can sign in
until you list one — a deliberate default, so a fresh deployment is never open
to the internet. Your account's `sub` is written to the log on the first
rejected attempt; add it to the service environment as a comma-separated list.

## 6. Accounts and statement history

Create your accounts under Budget → Accounts, then import statements from
Settings → Import.

The more history the better: twelve months is the target, three is the floor
below which the engine declines to suggest anything rather than guessing.

The coverage map shows which months you have. Gaps matter — the engine never
averages over a month it does not have, so filling a gap improves suggestions
more than adding further history does.
