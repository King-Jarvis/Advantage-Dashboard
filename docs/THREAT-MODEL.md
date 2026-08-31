# Threat model

What this defends against, and how. Reporting instructions are in
[SECURITY.md](../.github/SECURITY.md).

## Assets

OAuth refresh tokens (ongoing access to mail and calendar), email metadata and snippets,
calendar contents, financial transactions and balances.

## Trust boundaries

Browser → app (session, CSRF). n8n → app (ingest key, scoped routes). App → Google and
Anthropic (outbound TLS). Everything inside the Docker internal network is trusted; nothing
outside it is.

## Threats and controls

| Threat | Control |
|---|---|
| Secret committed to a public repo | `.gitignore`, `.env.example`, gitleaks, pre-commit, and a CI grep for host-specific strings. Guards are tested by planting violations |
| Stored XSS via email subject, sender, payee, model rationale | `textContent` only; CI rejects every markup sink; CSP forbids inline script. Two independent controls |
| Prompt injection via email or payee text | Untrusted content delimited; output parsed as strict JSON; importance clamped 1–5; malformed output discarded; output never drives control flow |
| Malicious upload | Size cap, extension and content sniff, streamed parse with a row cap, never executed. CSV formula injection neutralised on export |
| OAuth CSRF or code interception | PKCE, single-use `state` bound to the session, exact redirect URI, incremental scopes |
| Refresh-token theft at rest | Fernet encryption, key from a file secret, non-root user, read-only root filesystem |
| Open redirect after login | `return_to` allowlisted to same-origin relative paths |
| Session hijack or fixation | HttpOnly, Secure, SameSite=Strict, rotation on login, idle and absolute expiry, lockout after repeated failures |
| Secret exposure via `docker inspect` | Secrets mounted as files, never set as environment values |
| n8n compromise | Ingest key reaches `/api/ingest`, `/api/outbox` and `/api/google/token` only — never view or edit routes |
| Container escape | Non-root uid, all capabilities dropped, `no-new-privileges`, read-only root filesystem, internal network |
| Supply chain | One hash-pinned runtime dependency, images pinned by digest, Dependabot |
| Log leakage | Bodies, subjects, payees, amounts and tokens are never logged |

## Accepted limitations

- **Single trusted operator.** There is no multi-user model or per-user authorisation.
- **Disk encryption is the operator's job.** The app protects data with filesystem
  permissions and a non-root user; it does not encrypt the database.
- **Rate limiting covers login only**, not every endpoint.
- **Prompt injection cannot be eliminated**, only bounded. Constraining output shape and
  keeping it out of control flow limits the worst case to a wrong importance score.
- **Memory limits are unavailable** where the host's cgroup memory controller is disabled;
  `mem_limit` is silently ignored by Docker in that case.
