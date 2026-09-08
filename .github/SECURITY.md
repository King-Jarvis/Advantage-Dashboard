# Security

## Reporting

Open a GitHub Security Advisory on this repository rather than a public issue.

## What this application holds

Email metadata and snippets, calendar contents, and financial transactions —
plus OAuth refresh tokens granting ongoing access to the connected Google
accounts. Treat any deployment as holding personal data of real value.

## Design decisions worth knowing

**The app owns the Google tokens.** Refresh tokens are encrypted at rest with
Fernet, keyed from a file secret mounted at `TOKEN_KEY_PATH`. This is the one
place the project takes a third-party dependency (`cryptography`), because the
Python standard library has no symmetric cipher and storing bearer tokens in
plaintext would be worse than the dependency.

**Two unrelated identities.** A browser session (cookie, CSRF-protected) reaches
view and edit routes. The ingest key reaches `/api/ingest/*` and
`/api/sync/google` and nothing else — it cannot read or edit your data.
Compromising one grants nothing of the other. Full list in
[API.md](../docs/API.md), which is generated from the routing table.

**No markup sinks.** The UI renders text the user does not control — email
subjects and senders, transaction payees, model-suggested category names. All of it is
set with `textContent`. CI rejects any `innerHTML`, `insertAdjacentHTML` or
`document.write` in front-end code. The CSP additionally forbids inline script,
so the two controls are independent.

**Untrusted text reaching a model.** Email content can attempt prompt injection.
Model output is parsed as strict JSON, the importance score is clamped to 1-5,
and malformed output is discarded rather than trusted. Model output never
influences control flow — the worst outcome is a wrong score on one email.

**Budget numbers are not model output.** Every suggested figure is computed by
`stats.py` from your own history using deterministic statistics. The model only
writes the accompanying sentence. A budget you cannot reproduce or audit is
worse than no budget.

**Names only.** Two features can call a model, both off by default and both
enabled separately in Settings.

Filing an unfamiliar merchant sends normalised merchant names, your category
names, and up to forty worked examples from your own ledger in the form
`merchant -> category`. The examples are what let it tell a work canteen from
a night out when both are just restaurants. They are drawn from filings you
made by hand in preference to the model's own past output, so a guess is
never fed back as evidence for itself.

The savings plan sends category names alone, to judge which are contractual.

Neither sends amounts, dates, balances, account numbers or message contents,
and neither lets the model produce a number: it returns a label, and the
arithmetic is done here.

## Known limitations

- The dashboard has no multi-user model. It assumes one trusted operator.
- Data at rest relies on filesystem permissions — a `0700` data directory and
  `0600` files — plus Fernet encryption of the stored credentials. The database
  as a whole is not encrypted; full-disk encryption is the operator's job.
- The service listens on every interface so it can be reached from a phone, and
  is not hardened for exposure beyond a network you control.
- Rate limiting protects login only, not every endpoint.
