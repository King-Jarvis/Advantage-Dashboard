# Threat model

What this defends against, and how. Reporting instructions are in
[SECURITY.md](../.github/SECURITY.md).

Every control below is one that exists in the code today. An earlier version of this
document described a containerised deployment — non-root uid, dropped capabilities, a
read-only root filesystem, secrets mounted at `/run/secrets` — none of which was ever
built. Claiming a control you do not have is worse than admitting the gap, because it is
the sentence someone reads when deciding to trust the thing.

## Assets

OAuth refresh tokens (ongoing access to mail and calendar), email metadata, snippets and
cached bodies, calendar contents, financial transactions and balances, and the local
account password.

## Deployment shape

One Python process, run as an unprivileged user under a systemd **user** service, serving
HTTPS directly. It holds its own SQLite database. There is no container, no reverse proxy
and no second service in the trust chain.

## Trust boundaries

Browser → app (session cookie, CSRF token). App → Google, and optionally Anthropic
(outbound TLS). An external scheduler → app, over the ingest key, if you set one.

**The local network is inside the boundary and should not be.** The service binds every
interface, so anything on your LAN reaches the login page. Read the first accepted
limitation below before deciding that is acceptable for your network.

## Threats and controls

| Threat | Control |
|---|---|
| Secret committed to the repo | `.gitignore`, gitleaks in pre-commit *and* over full history in CI, `detect-private-key`, a CI grep for host-specific strings, and a CI check that no `.db`/`.key`/`.pem`/`.qfx`/`.env` file is tracked. Guards are tested by planting violations |
| Stored XSS via email subject, sender, payee or model text | `textContent` only, with CI rejecting every markup sink; and a CSP with no `unsafe-inline` in `script-src` or `style-src`. Two independent controls, so one mistake is not a breach |
| Prompt injection via email, payee or category text | Untrusted content delimited and named as untrusted; output parsed as strict JSON; a category name that is not already yours is discarded, as is a flexibility class we did not define; importance clamped 1–5; output never drives control flow, and never becomes a monetary figure |
| Credential harvesting by a stolen session | Secrets are write-only through the API. `all_for_display` returns whether a secret is set, never its value — so a hijacked session cannot read back the Google client secret or the Anthropic key |
| Malicious statement upload | 10 MB size cap, 20,000 row cap, decode-and-sniff before parsing, streamed parse, parse errors surfaced rather than swallowed. Never executed. Uploads are staged for review and write nothing to the ledger until confirmed |
| OAuth CSRF or code interception | PKCE (S256), single-use `state` with an expiry, exact redirect URI, sign-in and connect scopes kept separate |
| Open redirect after login | `return_to` allowlisted to same-origin relative paths; backslashes and newlines rejected |
| Refresh-token theft at rest | Fernet encryption with the key in a file, never an environment value. Verified against the live database: refresh tokens, access tokens, the Google client secret and the API key are all ciphertext |
| Session hijack or fixation | `HttpOnly`, `Secure`, `SameSite=Strict`, rotation on login, 24-hour idle and 7-day absolute expiry with the absolute never extended, CSRF token compared in constant time on every mutation |
| Online password guessing | scrypt at 16 MB, constant-time comparison, an unknown username still pays for a hash so timing reveals nothing, and a 5-failure / 5-minute lockout |
| Server-side request forgery via the image proxy | Only URLs this server HMAC-signed are fetched; every address a host resolves to must be outside the local network; each redirect hop re-checked; image content types only, 8 MB cap, 12-second timeout |
| Path traversal in static files | Containment checked against the `realpath`, so a symlink is not a way out |
| SQL injection | Every value parameterised. The few queries that interpolate do so only with column names drawn from a hardcoded allowlist |
| Compromise of the process | systemd `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`, and a single `ReadWritePaths` for the data directory. Runs as you, not root |
| Data at rest read by another local account | Data directory `0700`; database, log, WAL and certificate key all `0600`, set at creation |
| A leaked ingest key | Reaches `/api/ingest/events`, `/api/ingest/messages`, `/api/ingest/sync` and `/api/sync/google` only. It opens no route that can read or edit your data, and a browser session cannot open an ingest route either |
| Supply chain | One direct runtime dependency, version-pinned, plus two transitive. CI fails the build if any other third-party import appears. Dependabot on both manifests and on the GitHub Actions |
| Log leakage | Message bodies, subjects, payees, amounts and tokens are not logged. Request logging is off unless `DASHBOARD_VERBOSE` is set |
| Tracking pixels in mail | Remote images are fetched by the server, so the sender sees the host and not your device or its address |

## Accepted limitations

- **The local network is trusted, and that is the largest exposure.** The service binds
  `0.0.0.0` so it can be opened from a phone or tablet. Anything on the LAN reaches the
  login page, where a password and the lockout are the whole boundary. The certificate is
  self-signed, so unless you install the CA on your devices, nothing verifies the server's
  identity and a browser warning is indistinguishable from the one you already dismiss.
  **Do not expose this to the internet.**

- **A DNS rebinding race remains in the image proxy.** Addresses are validated, then the
  fetch resolves the name a second time. A domain answering differently between the two
  lookups could reach an internal address. Bounded by the signature requirement, the
  image content-type check and the size cap.

- **Session identifiers are stored unhashed**, so a readable database yields usable
  sessions. Minor, since that database is already the whole prize.

- **The lockout is per account, not per source.** Someone on the network can keep you
  locked out five minutes at a time. Accepted deliberately: per-source lockout is the
  weaker choice against a distributed guesser.

- **The encryption key sits beside the data it protects.** This defends a leaked backup,
  a copied database or a drive that leaves the house — not someone who already has your
  login on this host. Back the key up separately: losing it makes every stored credential
  unreadable.

- **Single trusted operator.** No multi-user model and no per-user authorisation. Every
  account that can sign in can see everything.

- **Disk encryption is the operator's job.** The application does not encrypt the
  database as a whole, only the credentials inside it.

- **Rate limiting covers login only**, not every endpoint.

- **Prompt injection cannot be eliminated**, only bounded. Constraining the output shape
  and keeping it out of control flow limits the worst case to a wrong importance score, a
  merchant filed under a category you already created, or a category marked more flexible
  than it is -- which changes a suggested figure on screen, never a stored budget, and is
  corrected by one dropdown.

- **Dependencies are version-pinned, not hash-pinned.** A compromised release of the one
  runtime dependency would be installed. Add `--require-hashes` for a deployment that
  needs more than a version pin.
