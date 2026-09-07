"""Database schema and migrations.

Money is an integer number of cents, everywhere, without exception. Floats
cannot represent 0.10 exactly, and a budget that is wrong by a cent per
transaction is worse than one that is obviously broken -- it is trusted.

Deletion is soft. A ledger that forgets is not auditable, and undo on a money
operation is not optional.
"""


import json

SCHEMA_VERSION = 6

# Foreign keys are off by default in SQLite and must be enabled per
# connection, not once per database. Enforced in storage.connect().
DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ── Accounts ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL DEFAULT 'checking',
    on_budget   INTEGER NOT NULL DEFAULT 1,
    closed      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- ── Categories ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS category_groups (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL UNIQUE,
    is_income INTEGER NOT NULL DEFAULT 0,
    sort      INTEGER NOT NULL DEFAULT 0,
    hidden    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
    id        TEXT PRIMARY KEY,
    group_id  TEXT NOT NULL REFERENCES category_groups(id),
    name      TEXT NOT NULL,
    is_income INTEGER NOT NULL DEFAULT 0,
    sort      INTEGER NOT NULL DEFAULT 0,
    hidden    INTEGER NOT NULL DEFAULT 0,
    -- When a month ends with a negative balance, carry the debt into next
    -- month rather than silently absorbing it into To Be Budgeted. Off by
    -- default matches how most people expect an overspend to behave.
    carryover_negative INTEGER NOT NULL DEFAULT 0,
    UNIQUE(group_id, name)
);

-- ── Transactions ─────────────────────────────────────────────────────────
-- Three shapes share this table, distinguished by parent_id and transfer_id:
--
--   plain     parent_id NULL, transfer_id NULL, category may be set
--   transfer  transfer_id -> the paired row's id; category is always NULL,
--             because moving your own money between accounts is not spending
--   split     a parent (parent_id NULL, category NULL) plus children
--             (parent_id -> parent, each with its own category)
--
-- Account balance counts rows with parent_id IS NULL only: the parent
-- carries the money, children only carry the categorisation. Counting both
-- would double the amount, which is the classic way split handling goes
-- wrong.
CREATE TABLE IF NOT EXISTS transactions (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES accounts(id),
    date         TEXT NOT NULL,               -- YYYY-MM-DD
    amount_cents INTEGER NOT NULL,            -- negative = money out
    payee        TEXT NOT NULL DEFAULT '',
    payee_norm   TEXT NOT NULL DEFAULT '',    -- lowercased, for dedup & rules
    notes        TEXT NOT NULL DEFAULT '',
    category_id  TEXT REFERENCES categories(id),
    cleared      INTEGER NOT NULL DEFAULT 0,
    reconciled   INTEGER NOT NULL DEFAULT 0,
    transfer_id  TEXT REFERENCES transactions(id),
    parent_id    TEXT REFERENCES transactions(id),
    imported_id  TEXT,                        -- stable hash; import idempotency
    source       TEXT NOT NULL DEFAULT 'manual',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    deleted      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_txn_account ON transactions(account_id, date);
CREATE INDEX IF NOT EXISTS ix_txn_date    ON transactions(date);
CREATE INDEX IF NOT EXISTS ix_txn_cat     ON transactions(category_id, date);
CREATE INDEX IF NOT EXISTS ix_txn_parent  ON transactions(parent_id);

-- Import idempotency: the same statement row must never land twice. Partial,
-- so the many rows without an imported_id do not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS ux_txn_imported
    ON transactions(account_id, imported_id)
    WHERE imported_id IS NOT NULL AND deleted = 0;

-- ── Envelope budget ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS budget_months (
    month          TEXT NOT NULL,             -- YYYY-MM
    category_id    TEXT NOT NULL REFERENCES categories(id),
    budgeted_cents INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, category_id)
);

-- ── Identity ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    pw_hash       TEXT,                       -- NULL for Google-only accounts
    pw_salt       TEXT,
    google_sub    TEXT UNIQUE,                -- Google's stable account id
    created_at    TEXT NOT NULL,
    last_login_at TEXT,
    failed_count  INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT                        -- ISO timestamp, NULL when open
);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,            -- opaque, 256 bits of entropy
    user_id      TEXT NOT NULL REFERENCES users(id),
    csrf_token   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,               -- absolute cap, never extended
    ip           TEXT NOT NULL DEFAULT '',
    user_agent   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id);

-- ── Statement import ─────────────────────────────────────────────────────
-- Nothing reaches the ledger until a batch is committed. Rows are parsed,
-- shown for review, and only then written -- so a misread column or a wrong
-- date format is caught by a person rather than discovered months later in a
-- budget that quietly does not add up.
CREATE TABLE IF NOT EXISTS import_batches (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL REFERENCES accounts(id),
    filename      TEXT NOT NULL,
    fingerprint   TEXT NOT NULL DEFAULT '',   -- identifies the bank's format
    kind          TEXT NOT NULL DEFAULT 'csv',
    uploaded_at   TEXT NOT NULL,
    period_start  TEXT, period_end TEXT,
    rows_total    INTEGER NOT NULL DEFAULT 0,
    rows_duplicate INTEGER NOT NULL DEFAULT 0,
    rows_imported INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL DEFAULT 'review'  -- review | committed | discarded
);

CREATE TABLE IF NOT EXISTS import_rows (
    id            TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL REFERENCES import_batches(id),
    line_no       INTEGER NOT NULL,
    raw           TEXT NOT NULL DEFAULT '',
    date          TEXT,
    amount_cents  INTEGER,
    payee         TEXT NOT NULL DEFAULT '',
    payee_norm    TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    dedup_key     TEXT NOT NULL DEFAULT '',
    category_id   TEXT REFERENCES categories(id),
    is_duplicate  INTEGER NOT NULL DEFAULT 0,
    -- 'exact' when the bank's own id or our dedup key already exists, 'near'
    -- when it merely looks like one. The distinction matters to whoever is
    -- reviewing: exact is a fact, near is a judgement they should make.
    dup_kind      TEXT NOT NULL DEFAULT '',
    excluded      INTEGER NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    txn_id        TEXT REFERENCES transactions(id)
);

CREATE INDEX IF NOT EXISTS ix_import_rows_batch ON import_rows(batch_id);

-- One mapping per bank format, so each bank is a one-time cost rather than a
-- code change. Keyed on a hash of the header row.
CREATE TABLE IF NOT EXISTS bank_mappings (
    fingerprint TEXT PRIMARY KEY,
    label       TEXT NOT NULL DEFAULT '',
    column_map  TEXT NOT NULL,               -- JSON
    date_format TEXT NOT NULL DEFAULT '',
    amount_sign INTEGER NOT NULL DEFAULT 1,  -- -1 when outflows are positive
    created_at  TEXT NOT NULL
);

-- Which months actually have statement data. The budget engine must never
-- average over a month it does not have: a gap silently drags a mean toward
-- zero, and the fix is to make gaps visible rather than to be clever.
CREATE TABLE IF NOT EXISTS month_coverage (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    month      TEXT NOT NULL,
    txn_count  INTEGER NOT NULL DEFAULT 0,
    first_day  TEXT, last_day TEXT,
    PRIMARY KEY (account_id, month)
);

-- ── OAuth in flight ──────────────────────────────────────────────────────
-- One row per authorisation attempt, deleted the moment it is used. Kept in
-- the database rather than in memory so a restart mid-flow fails closed
-- rather than losing the verifier and leaving the user at a broken callback.
CREATE TABLE IF NOT EXISTS oauth_pending (
    state         TEXT PRIMARY KEY,
    code_verifier TEXT NOT NULL,
    purpose       TEXT NOT NULL,          -- 'signin' | 'connect'
    return_to     TEXT NOT NULL DEFAULT '/',
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

-- ── Calendar ─────────────────────────────────────────────────────────────
-- Mirrors what Google holds. The provider owns this data, so the local copy
-- is a cache with one exception: a row with `dirty` set carries a local edit
-- that has not been pushed yet, and a poll must not overwrite it.
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
    -- The provider's own id. An event created here has no Google id yet, so
    -- it carries a 'local:<uuid>' placeholder until the insert comes back
    -- with the real one. A placeholder rather than NULL because dropping the
    -- NOT NULL and the UNIQUE would mean rebuilding the table, and rebuilding
    -- a table that holds real data is a bad trade for a cosmetic nicety.
    source_uid   TEXT NOT NULL,
    calendar_id  TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    location     TEXT NOT NULL DEFAULT '',
    starts_at    TEXT NOT NULL,             -- ISO 8601, UTC
    ends_at      TEXT,
    all_day      INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'confirmed',
    updated_at   TEXT NOT NULL,
    dirty        INTEGER NOT NULL DEFAULT 0,
    deleted      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_id, source_uid)
);

CREATE INDEX IF NOT EXISTS ix_events_when ON events(starts_at);

-- ── Mail ─────────────────────────────────────────────────────────────────
-- Metadata and a snippet only. Full bodies are not stored: they are not
-- needed to decide whether something wants you, and holding them would turn
-- a convenience into a second copy of your mailbox.
CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL REFERENCES google_accounts(id) ON DELETE CASCADE,
    source_uid    TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    sender        TEXT NOT NULL DEFAULT '',
    sender_email  TEXT NOT NULL DEFAULT '',
    subject       TEXT NOT NULL DEFAULT '',
    snippet       TEXT NOT NULL DEFAULT '',
    received_at   TEXT NOT NULL,
    is_unread     INTEGER NOT NULL DEFAULT 1,
    is_starred    INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    labels        TEXT NOT NULL DEFAULT '',
    -- What the classifier thought, and what you said when it was wrong.
    -- Kept apart so a correction survives re-classification.
    importance    INTEGER,
    importance_override INTEGER,
    reason        TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    classified_at TEXT,
    dirty         INTEGER NOT NULL DEFAULT 0,
    deleted       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_id, source_uid)
);

CREATE INDEX IF NOT EXISTS ix_messages_when ON messages(received_at);

-- ── Themes ───────────────────────────────────────────────────────────────
-- A theme is data, not code: a validated map of design-token names to CSS
-- values, applied at runtime by setting custom properties on the root
-- element. That is why one can be imported from a file rather than written
-- into the stylesheet.
CREATE TABLE IF NOT EXISTS themes (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    author      TEXT NOT NULL DEFAULT '',
    base        TEXT NOT NULL DEFAULT 'dark',
    tokens_json TEXT NOT NULL DEFAULT '{}',
    -- Built-ins cannot be deleted: losing every theme would leave no way back
    -- to a known-good look, which is the one unrecoverable state here.
    builtin     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_rank ON messages(archived, importance);

-- ── Sync bookkeeping ─────────────────────────────────────────────────────
-- What each source last managed, so a stale or failing feed is visible on
-- screen rather than looking like a quiet week.
CREATE TABLE IF NOT EXISTS sync_state (
    source       TEXT PRIMARY KEY,          -- 'calendar:<account>', 'mail:<account>'
    cursor       TEXT NOT NULL DEFAULT '',
    last_run_at  TEXT,
    last_ok_at   TEXT,
    last_status  TEXT NOT NULL DEFAULT '',
    last_error   TEXT NOT NULL DEFAULT ''
);

-- ── Settings ─────────────────────────────────────────────────────────────
-- Key/value rather than columns, so adding a setting is not a migration.
-- `secret` marks a value stored encrypted; those are never returned to the
-- browser, only ever reported as set or not set.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    secret     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- ── Connected Google accounts ────────────────────────────────────────────
-- Separate from `users`: signing in proves who you are, connecting an
-- account grants access to its mail and calendar, and those are different
-- permissions that should be revocable independently.
CREATE TABLE IF NOT EXISTS google_accounts (
    id             TEXT PRIMARY KEY,
    sub            TEXT NOT NULL UNIQUE,     -- Google's stable account id
    email          TEXT NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    scopes         TEXT NOT NULL DEFAULT '',
    refresh_token  TEXT NOT NULL DEFAULT '', -- encrypted at rest
    access_token   TEXT NOT NULL DEFAULT '', -- encrypted; short-lived anyway
    expires_at     TEXT,
    connected_at   TEXT NOT NULL,
    last_sync_at   TEXT,
    last_error     TEXT NOT NULL DEFAULT ''
);

-- ── Audit ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'system',
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    detail      TEXT NOT NULL DEFAULT ''
);
"""


# Columns added after version 1. CREATE TABLE IF NOT EXISTS does nothing to a
# table that already exists, so a new column has to be added explicitly or it
# will simply never appear on any database that predates it -- which is every
# installation that already holds data.
ADDED_COLUMNS = [
    # Gmail actions that push back, and the body we fetch on demand.
    ("messages", "trashed", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "is_spam", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "body_text", "TEXT"),
    ("messages", "body_fetched_at", "TEXT"),
    ("messages", "push_error", "TEXT NOT NULL DEFAULT ''"),
    # The structured form of the body: headings, paragraphs, images.
    ("messages", "body_blocks", "TEXT"),
    # Why a push failed, and an intent to delete that survives until it lands.
    ("events", "push_error", "TEXT NOT NULL DEFAULT ''"),
    ("events", "pending_delete", "INTEGER NOT NULL DEFAULT 0"),
    ("events", "etag", "TEXT NOT NULL DEFAULT ''"),
    # RFC 5545 RRULE, exactly as Google stores it, and the reminder in
    # minutes before the start. -1 means "whatever the calendar's default is",
    # which is not the same as no reminder at all.
    ("events", "recurrence", "TEXT NOT NULL DEFAULT ''"),
    ("events", "reminder_minutes", "INTEGER NOT NULL DEFAULT -1"),
    # Set when this row is one instance of a repeating series. The rule itself
    # lives on the series, which we never fetch, so this is how we know a
    # thing repeats without pretending to know how.
    ("events", "series_id", "TEXT NOT NULL DEFAULT ''"),
    # Google's own classification. Birthdays generated from your profile or
    # contacts are 'birthday' and cannot be changed through the API at all,
    # so the interface needs to know before offering to change one.
    ("events", "event_type", "TEXT NOT NULL DEFAULT 'default'"),
    # How many times a push of this row has failed. Enumerating every error
    # Google might return permanently is a losing game; counting attempts
    # catches the ones nobody predicted.
    ("events", "push_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "push_attempts", "INTEGER NOT NULL DEFAULT 0"),
]


def has_column(conn, table, name):
    return any(r[1] == name for r in
               conn.execute("PRAGMA table_info(%s)" % table).fetchall())


def add_column(conn, table, name, decl):
    """Add a column if it is missing. Returns True when it did something.

    ALTER TABLE ADD COLUMN is the one schema change SQLite does cheaply and
    without rewriting the table, which is what makes this safe to run against
    a live database on every start.
    """
    if has_column(conn, table, name):
        return False
    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
    return True


def _seed_themes(conn):
    """Put the shipped themes in the table if they are not there.

    Matched by name so a second start does not add a duplicate, and so a
    built-in whose colours change in a release updates rather than forking.
    """
    from . import builtin_themes, themes
    for theme in builtin_themes.ALL:
        row = conn.execute("SELECT id FROM themes WHERE name=? AND builtin=1",
                           (theme["name"],)).fetchone()
        if row is None:
            themes.save(conn, theme, builtin=True)
        else:
            conn.execute(
                "UPDATE themes SET tokens_json=?, base=? WHERE id=?",
                (json.dumps(theme["tokens"], separators=(",", ":")),
                 theme["base"], row["id"]))
    conn.commit()


def migrate(conn):
    """Apply the schema. Safe to call on every start."""
    conn.executescript(DDL)
    for table, name, decl in ADDED_COLUMNS:
        add_column(conn, table, name, decl)
    _seed_themes(conn)
    cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    conn.commit()
    return SCHEMA_VERSION
