#!/usr/bin/env python3
"""Regenerate docs/API.md from the routing table.

The reference had drifted to documenting seventeen of seventy-one routes, ten
of which no longer existed -- including three the security documentation cited
as the ingest key's entire reach. A hand-maintained endpoint list is wrong the
moment someone adds a route and forgets, and nothing notices.

server.py's ROUTES is the only thing that decides what is reachable, so it is
the only honest source for this file. Run with --check in CI to fail when the
committed file no longer matches.
"""
import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
API_MD = ROOT / "docs" / "API.md"

# Purpose lines are the one thing that cannot be derived. Missing entries are
# reported rather than silently blank, so a new route has to be described.
PURPOSE = {
    "login": "Open a session. Returns the CSRF token to send on every mutation",
    "logout": "End this session",
    "whoami": "The signed-in user, or 401",
    "health": "Liveness. No authentication, no data",
    "config": "Whether setup is complete and which features are configured",
    "claim": "Claim a fresh install with the setup token. Closes once a user exists",
    "g_start": "Begin the Google OAuth flow (PKCE)",
    "g_cb": "OAuth callback. Exchanges the code and stores the tokens encrypted",
    "g_check": "Whether Google is configured and reachable",
    "g_connect": "Begin connecting an additional Google account",
    "g_accts": "Connected Google accounts",
    "g_acct": "Disconnect an account and delete its tokens",
    "settings": "Read settings, or change them. Secrets are write-only",
    "setting": "Clear one setting",
    "accounts": "Ledger accounts: list, or create",
    "catall": "Categorise every unfiled row, deduplicated by merchant",
    "budget": "The month's envelopes, activity and balances",
    "suggest": "Suggested budget figures from your own statement history",
    "overview": "Totals across accounts",
    "home": "The combined home screen payload",
    "agenda": "Events for the agenda panel",
    "calendar": "Events for the month grid",
    "evnew": "Create an event locally and queue the push",
    "evedit": "Edit or delete an event",
    "inbox": "Scored mail, most important first",
    "image": "Fetch a remote image the server signed. Signature required",
    "themeact": "The active theme's tokens. Readable before sign-in; colours only",
    "themes": "Installed themes, or import one",
    "theme1": "Activate or delete a theme",
    "msgbody": "A message body, fetched on demand and cached, HTML stripped",
    "syncst": "Last sync, pending pushes and any error",
    "editmsg": "Star, read, archive, trash or spam a message",
    "ing_ev": "Calendar events, upserted by (account, source_uid)",
    "ing_msg": "Mail with importance, upserted by (account, source_uid)",
    "ing_sync": "Trigger a sync",
    "sync_ing": "Run a Google sync (ingest key)",
    "sync_ses": "Run a Google sync (Sync now button)",
    "history": "Import batches and what they covered",
    "coverage": "Which months each account has statements for",
    "txns": "Transactions, filtered",
    "unfiled": "Rows with no category yet",
    "fileone": "Edit or delete one transaction",
    "filemany": "File every row for one merchant at once",
    "xfers": "Linked transfers and candidate pairs",
    "ledger": "The full hierarchical ledger view",
    "xferlink": "Link two rows as one transfer, or unlink",
    "split": "Read, create or remove a transaction's split parts",
    "upload": "Upload a statement. Parsed and staged, writes nothing yet",
    "batches": "Staged and committed import batches",
    "unimport": "Undo a committed import",
    "batch": "Read, commit or discard a staged batch",
    "batchrow": "Edit one staged row before committing",
    "cats": "Categories: list, or create",
    "cat": "Rename, move, hide or delete a category",
    "groups": "Category groups: list, or create",
    "setbudget": "Set one envelope's amount for a month",
    "movemoney": "Move budgeted money between envelopes",
}

GROUPS = [
    ("Auth", "none / session",
     "Signing in and out, and the Google OAuth flow.",
     ["login", "logout", "whoami", "claim", "g_start", "g_cb"]),
    ("Setup and status", "none",
     "Reachable without a session. Neither returns personal data.",
     ["health", "config", "themeact"]),
    ("View", "session",
     "Reads. No CSRF token needed.",
     ["home", "overview", "budget", "suggest", "agenda", "calendar", "inbox",
      "msgbody", "syncst", "txns", "unfiled", "ledger", "xfers", "history",
      "coverage", "image"]),
    ("Edit", "session + CSRF",
     "Every mutation needs the `X-CSRF-Token` header from login.",
     ["evnew", "evedit", "editmsg", "fileone", "filemany", "split", "xferlink",
      "setbudget", "movemoney", "cats", "cat", "groups", "accounts", "catall"]),
    ("Import", "session + CSRF",
     "A statement is parsed and staged for review; nothing reaches the ledger "
     "until the batch is committed, and a commit can be undone.",
     ["upload", "batches", "batch", "batchrow", "unimport"]),
    ("Settings and themes", "session + CSRF",
     "Secrets are write-only: the API reports whether one is set, never its value.",
     ["settings", "setting", "g_check", "g_connect", "g_accts", "g_acct",
      "themes", "theme1"]),
    ("Sync", "session + CSRF",
     "The same work the background scheduler does, on demand.",
     ["sync_ses"]),
    ("Ingest", "ingest key",
     "For an external scheduler. Closed unless you set an ingest key, and it "
     "opens no route that can read or edit your data. Ingest is idempotent: "
     "re-posting the same payload repairs rather than duplicates.",
     ["ing_ev", "ing_msg", "ing_sync", "sync_ing"]),
]


def routes():
    src = (ROOT / "app" / "src" / "dashboard" / "server.py").read_text()
    start = src.index("ROUTES = [")
    table = src[start:src.index("\n]\n", start)]
    out = {}
    pattern = r'\("(\w+)",\s*\{([^}]*)\}[^)]*?re\.compile\(r"\^([^"]+)\$"\)'
    for m in re.finditer(pattern, table, re.S):
        name, methods, path = m.group(1), m.group(2), m.group(3)
        path = (path.replace(r"([0-9a-f]{32})", "{id}")
                    .replace(r"(\d{4}-\d{2})", "{month}")
                    .replace(r"([a-z_]{3,40})", "{key}"))
        order = ["GET", "POST", "PATCH", "PUT", "DELETE"]
        found = re.findall(r'"(\w+)"', methods)
        out[name] = (sorted(found, key=order.index), path)
    return out


def render():
    found = routes()
    placed = {n for _, _, _, names in GROUPS for n in names}
    missing = set(found) - placed
    undescribed = set(found) - set(PURPOSE)
    if missing or undescribed:
        for n in sorted(missing):
            print("  route %r is in ROUTES but no group in %s lists it"
                  % (n, pathlib.Path(__file__).name), file=sys.stderr)
        for n in sorted(undescribed):
            print("  route %r has no PURPOSE line" % n, file=sys.stderr)
        raise SystemExit("BLOCKED - the API doc generator is out of date")

    lines = [
        "# API",
        "",
        "<!-- Generated by scripts/gen-api-doc.py from the ROUTES table in",
        "     server.py. Do not edit by hand: run the script. -->",
        "",
        "Two unrelated identities. A **browser session** (cookie plus CSRF"
        " token) reaches",
        "the view and edit routes. The **ingest key** (`X-Ingest-Key`) reaches"
        " the ingest",
        "routes and nothing else. Compromising one grants nothing of the other.",
        "",
        "Any path not listed here is a 404. Authorisation is declared per"
        " route, so a new",
        "route is unreachable until it says what it needs.",
        "",
    ]
    for title, auth, blurb, names in GROUPS:
        lines += ["## %s — %s" % (title, auth), "", blurb, "",
                  "| Method | Path | Purpose |", "|---|---|---|"]
        for n in names:
            methods, path = found[n]
            lines.append("| %s | `%s` | %s |"
                         % (" \\| ".join(methods), path, PURPOSE[n]))
        lines.append("")
    lines += ["---", "",
              "%d routes." % sum(len(m) for m, _ in found.values()),
              "Regenerate with `./scripts/gen-api-doc.py`.", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed file is out of date")
    args = ap.parse_args()
    text = render()
    if args.check:
        if API_MD.read_text() != text:
            raise SystemExit(
                "BLOCKED - docs/API.md is out of date.\n"
                "  Run ./scripts/gen-api-doc.py and commit the result.")
        print("clean - docs/API.md matches the routing table")
    else:
        API_MD.write_text(text)
        print("wrote %s" % API_MD.relative_to(ROOT))
