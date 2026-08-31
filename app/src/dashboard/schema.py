"""Database schema and migrations.

Money is an integer number of cents, everywhere, without exception. Floats
cannot represent 0.10 exactly, and a budget that is wrong by a cent per
transaction is worse than one that is obviously broken -- it is trusted.

Deletion is soft. A ledger that forgets is not auditable, and undo on a money
operation is not optional.
"""


SCHEMA_VERSION = 1

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


def migrate(conn):
    """Apply the schema. Safe to call on every start."""
    conn.executescript(DDL)
    cur = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    conn.commit()
    return SCHEMA_VERSION
