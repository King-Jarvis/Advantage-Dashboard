"""Reading bank statements.

Every bank exports differently, so the aim is not to understand all of them
but to make each one a one-time mapping rather than a code change. A header
row is fingerprinted, the mapping is remembered against that fingerprint, and
the next statement from the same bank needs no interaction.

Nothing here writes to the ledger. Parsing produces rows for review; a person
confirms them and only then are transactions created. A misread column or a
mis-detected date format is then caught by someone looking at it, rather than
discovered months later in a budget that quietly does not add up.
"""

import csv
import hashlib
import io
import json
import re
import uuid
from datetime import datetime

from .money import to_cents
from .text import norm_payee

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 20000

# Header names seen in the wild, lowercased. Order matters: the first match
# wins, so the more specific names come first.
HEADERS = {
    "date": ["transaction date", "posting date", "posted date", "value date",
             "date posted", "completed date", "date"],
    "payee": ["description", "payee", "merchant", "name", "details",
              "transaction description", "narrative", "reference", "memo"],
    "amount": ["amount", "transaction amount", "value"],
    "debit": ["debit", "money out", "paid out", "withdrawal", "withdrawals",
              "outflow"],
    "credit": ["credit", "money in", "paid in", "deposit", "deposits",
               "inflow"],
    "notes": ["notes", "note", "category", "type", "transaction type"],
    "balance": ["balance", "running balance"],
}

# Ordered most-specific first: %d/%m before %m/%d would silently mis-read
# every date below the 13th, so ambiguity is resolved by evidence in
# detect_date_format rather than by whichever pattern is tried first.
DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
    "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
    "%Y%m%d", "%d/%m/%y", "%m/%d/%y",
]

class ParseError(Exception):
    pass


def fingerprint(header):
    """Identify a bank's export format from its header row."""
    key = "|".join(sorted(h.strip().lower() for h in header if h.strip()))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def sniff(blob):
    """Decode bytes and work out what kind of file this is."""
    if len(blob) > MAX_BYTES:
        raise ParseError("file is larger than %d MB" % (MAX_BYTES // 1024 // 1024))
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = blob.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ParseError("could not decode the file as text")

    head = text[:4000].upper()
    if "<OFX>" in head or "OFXHEADER" in head:
        return "ofx", text
    if not text.strip():
        raise ParseError("the file is empty")
    return "csv", text


# ── CSV ───────────────────────────────────────────────────────────────────
def _find(header, kind):
    lowered = [h.strip().lower() for h in header]
    for want in HEADERS[kind]:
        for i, h in enumerate(lowered):
            if h == want:
                return i
    for want in HEADERS[kind]:                 # then substring
        for i, h in enumerate(lowered):
            if want in h:
                return i
    return None


def guess_mapping(header):
    """Best guess at which column is what. Always shown for confirmation."""
    m = {k: _find(header, k) for k in
         ("date", "payee", "amount", "debit", "credit", "notes")}
    if m["date"] is None:
        raise ParseError("no date column found in: " + ", ".join(header))
    if m["amount"] is None and m["debit"] is None and m["credit"] is None:
        raise ParseError("no amount column found in: " + ", ".join(header))
    return {k: v for k, v in m.items() if v is not None}


def detect_date_format(samples):
    """Choose a date format using evidence, not guesswork.

    A column of `03/04/2026` is genuinely ambiguous. Rather than assume a
    locale, every candidate format is tried against every sample and only
    those parsing all of them survive. If two survive and the data contains a
    day above 12 the ambiguity is already resolved; if it does not, the first
    surviving format is returned and the caller is expected to show it for
    confirmation.
    """
    usable = [s.strip() for s in samples if s and s.strip()]
    if not usable:
        raise ParseError("no dates to inspect")

    # Score rather than require unanimity. Real exports carry footer rows,
    # running totals and the occasional blank; refusing the whole statement
    # because one line is not a date helps nobody.
    scores = {}
    for fmt in DATE_FORMATS:
        n = 0
        for s in usable:
            try:
                datetime.strptime(s, fmt)
                n += 1
            except ValueError:
                pass
        if n:
            scores[fmt] = n

    if not scores:
        raise ParseError("unrecognised date format, e.g. %r" % usable[0])

    best = max(scores.values())
    if best < max(1, len(usable) // 2):
        raise ParseError("no date format fits most rows, e.g. %r" % usable[0])

    # Everything tying for best is genuinely ambiguous -- 03/04/2026 is both
    # a 3rd of April and a 4th of March, and no amount of cleverness settles
    # it. The caller shows the choice rather than picking silently.
    survivors = [f for f in DATE_FORMATS if scores.get(f) == best]
    return survivors[0], survivors


def parse_csv(text, mapping=None, date_format=None, amount_sign=1):
    """Parse into normalised rows. Returns (rows, meta)."""
    try:
        dialect = csv.Sniffer().sniff(text[:8000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration:
        raise ParseError("the file has no rows") from None

    mapping = mapping or guess_mapping(header)
    body = [r for r in reader if any(c.strip() for c in r)]
    if len(body) > MAX_ROWS:
        raise ParseError("more than %d rows" % MAX_ROWS)

    di = mapping["date"]
    if not date_format:
        date_format, _ = detect_date_format(
            [r[di] for r in body[:60] if len(r) > di])

    rows = []
    for n, raw in enumerate(body, start=2):     # line 1 is the header
        row = {"line_no": n, "raw": dialect.delimiter.join(raw),
               "error": "", "date": None, "amount_cents": None,
               "payee": "", "notes": ""}
        try:
            row["date"] = datetime.strptime(raw[di].strip(),
                                            date_format).strftime("%Y-%m-%d")
            row["amount_cents"] = _amount(raw, mapping, amount_sign)
            for key in ("payee", "notes"):
                idx = mapping.get(key)
                if idx is not None and len(raw) > idx:
                    row[key] = raw[idx].strip()
        except (ValueError, IndexError, TypeError) as e:
            # A bad row is reported, never silently dropped: a statement that
            # imports 47 of 48 rows without saying so is worse than one that
            # refuses.
            row["error"] = str(e)[:200]
        row["payee_norm"] = norm_payee(row["payee"])
        rows.append(row)

    return rows, {"header": header, "mapping": mapping,
                  "date_format": date_format, "amount_sign": amount_sign,
                  "fingerprint": fingerprint(header),
                  "delimiter": dialect.delimiter}


def _amount(raw, mapping, sign):
    """Single amount column, or separate debit and credit columns."""
    if "amount" in mapping:
        cents = to_cents(raw[mapping["amount"]].strip())
        return cents * sign
    debit = credit = 0
    if "debit" in mapping and len(raw) > mapping["debit"]:
        v = raw[mapping["debit"]].strip()
        debit = to_cents(v) if v else 0
    if "credit" in mapping and len(raw) > mapping["credit"]:
        v = raw[mapping["credit"]].strip()
        credit = to_cents(v) if v else 0
    if debit and credit:
        raise ValueError("row has both a debit and a credit")
    # A debit column holds a positive number meaning money out.
    return credit - abs(debit)


# ── OFX / QFX ─────────────────────────────────────────────────────────────
_TAG = re.compile(r"<([A-Z0-9.]+)>([^<\r\n]*)", re.I)


def parse_ofx(text):
    """Parse the SGML-ish OFX body.

    OFX is not XML -- tags are frequently unclosed -- so this reads the
    transaction blocks directly rather than pretending a parser will cope.
    """
    rows = []
    blocks = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, re.S | re.I)
    if not blocks:
        raise ParseError("no transactions found in the OFX file")
    for n, block in enumerate(blocks, start=1):
        fields = {k.upper(): v.strip() for k, v in _TAG.findall(block)}
        row = {"line_no": n, "raw": block.strip()[:400], "error": "",
               "date": None, "amount_cents": None, "payee": "", "notes": ""}
        try:
            raw_date = fields.get("DTPOSTED", "")[:8]
            row["date"] = datetime.strptime(raw_date, "%Y%m%d").strftime("%Y-%m-%d")
            row["amount_cents"] = to_cents(fields.get("TRNAMT", ""))
            row["payee"] = fields.get("NAME") or fields.get("PAYEE") or ""
            row["notes"] = fields.get("MEMO", "")
            # FITID is the bank's own unique id for the transaction, which is
            # a far better deduplication key than anything we could derive.
            row["fitid"] = fields.get("FITID", "")
        except (ValueError, TypeError) as e:
            row["error"] = str(e)[:200]
        row["payee_norm"] = norm_payee(row["payee"])
        rows.append(row)
    return rows, {"header": [], "mapping": {}, "date_format": "%Y%m%d",
                  "amount_sign": 1, "fingerprint": "ofx", "delimiter": ""}


def parse(blob, mapping=None, date_format=None, amount_sign=1):
    kind, text = sniff(blob)
    if kind == "ofx":
        rows, meta = parse_ofx(text)
    else:
        rows, meta = parse_csv(text, mapping, date_format, amount_sign)
    meta["kind"] = kind
    return rows, meta


def revert_batch(conn, batch_id):
    """Undo a committed import.

    Filing a statement against the wrong account is an ordinary mistake and
    was, until now, permanent: a hundred and twenty rows in the wrong place
    with nothing to do but delete them one at a time.

    Every committed row remembers the transaction it created, so the undo is
    exact -- it removes what this import added and nothing else. Rows edited
    since are still removed: they arrived with this file, and leaving a few
    behind because they were touched would be a stranger outcome than taking
    the import back whole.

    delete_transaction does the careful part. It takes whole units, so half a
    transfer or a split parent without its children is never left behind.
    """
    from . import ledger
    row = conn.execute(
        "SELECT id, state, account_id FROM import_batches WHERE id=?",
        (batch_id,)).fetchone()
    if row is None:
        raise KeyError(batch_id)
    if row["state"] != "committed":
        raise ValueError("only a committed import can be undone")

    txns = [r["txn_id"] for r in conn.execute(
        "SELECT txn_id FROM import_rows WHERE batch_id=? AND txn_id IS NOT NULL",
        (batch_id,)).fetchall()]
    removed = 0
    for tid in txns:
        removed += ledger.delete_transaction(conn, tid)
    conn.execute("UPDATE import_rows SET txn_id=NULL WHERE batch_id=?",
                 (batch_id,))
    conn.execute("UPDATE import_batches SET state='reverted', rows_imported=0"
                 " WHERE id=?", (batch_id,))
    conn.commit()
    # Coverage is derived from what is actually in the ledger, so it has to be
    # recomputed or the months this file covered stay marked as covered.
    refresh_coverage(conn, row["account_id"])
    return {"batch": batch_id, "transactions_removed": removed,
            "rows": len(txns)}


# ── deduplication ─────────────────────────────────────────────────────────
def dedup_key(account_id, row, occurrence=0):
    """A stable identity for a statement row.

    The bank's own FITID when there is one. Otherwise date, amount and
    normalised payee -- plus an occurrence index, because two identical
    coffees on the same day are two transactions, not a duplicate, and
    collapsing them would quietly lose money.
    """
    if row.get("fitid"):
        basis = "fitid:%s:%s" % (account_id, row["fitid"])
    else:
        basis = "%s|%s|%s|%s|%d" % (account_id, row["date"],
                                    row["amount_cents"], row["payee_norm"],
                                    occurrence)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def assign_keys(account_id, rows):
    """Give every parsed row a dedup key, counting repeats within the file."""
    seen = {}
    for row in rows:
        if row.get("error") or row["date"] is None:
            continue
        sig = (row["date"], row["amount_cents"], row["payee_norm"])
        n = seen.get(sig, 0)
        seen[sig] = n + 1
        row["dedup_key"] = dedup_key(account_id, row, n)
    return rows


def find_existing(conn, account_id, row, fuzz_days=3):
    """Is this row already in the ledger?

    Exact identity first. Failing that, a near match -- same amount, same
    normalised payee, within a few days -- because banks routinely re-date a
    transaction between a pending and a posted export.
    """
    if row.get("dedup_key"):
        hit = conn.execute(
            "SELECT id FROM transactions WHERE account_id=? AND imported_id=?"
            " AND deleted=0", (account_id, row["dedup_key"])).fetchone()
        if hit:
            return hit["id"], "exact"
    if row["date"] is None or row["amount_cents"] is None:
        return None, ""
    hit = conn.execute(
        "SELECT id FROM transactions WHERE account_id=? AND deleted=0"
        "  AND amount_cents=? AND payee_norm=?"
        "  AND ABS(julianday(date) - julianday(?)) <= ?",
        (account_id, row["amount_cents"], row["payee_norm"], row["date"],
         fuzz_days)).fetchone()
    return (hit["id"], "near") if hit else (None, "")


def new_id():
    return uuid.uuid4().hex


def remember_mapping(conn, meta, label=""):
    if not meta.get("fingerprint") or meta["fingerprint"] == "ofx":
        return
    conn.execute(
        "INSERT INTO bank_mappings (fingerprint, label, column_map,"
        " date_format, amount_sign, created_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(fingerprint) DO UPDATE SET column_map=excluded.column_map,"
        " date_format=excluded.date_format, amount_sign=excluded.amount_sign",
        (meta["fingerprint"], label, json.dumps(meta["mapping"]),
         meta["date_format"], meta["amount_sign"],
         datetime.now().isoformat(timespec="seconds")))
    conn.commit()


def recall_mapping(conn, fp):
    row = conn.execute("SELECT * FROM bank_mappings WHERE fingerprint=?",
                       (fp,)).fetchone()
    if row is None:
        return None
    return {"mapping": json.loads(row["column_map"]),
            "date_format": row["date_format"],
            "amount_sign": row["amount_sign"], "label": row["label"]}


# ── batches ───────────────────────────────────────────────────────────────
def create_batch(conn, account_id, filename, blob, mapping=None,
                 date_format=None, amount_sign=None):
    """Parse a file into a reviewable batch. Writes nothing to the ledger.

    A remembered mapping for this bank is used automatically, so the second
    statement from the same bank needs no interaction.
    """
    kind, text = sniff(blob)
    remembered = None
    if kind == "csv" and mapping is None:
        try:
            header = next(csv.reader(io.StringIO(text)))
            remembered = recall_mapping(conn, fingerprint(header))
        except (StopIteration, csv.Error):
            remembered = None
    if remembered:
        mapping = mapping or remembered["mapping"]
        date_format = date_format or remembered["date_format"]
        if amount_sign is None:
            amount_sign = remembered["amount_sign"]

    rows, meta = parse(blob, mapping=mapping, date_format=date_format,
                       amount_sign=1 if amount_sign is None else amount_sign)
    assign_keys(account_id, rows)

    dates = sorted(r["date"] for r in rows if r["date"])
    bid = new_id()
    conn.execute(
        "INSERT INTO import_batches (id, account_id, filename, fingerprint,"
        " kind, uploaded_at, period_start, period_end, rows_total, state)"
        " VALUES (?,?,?,?,?,?,?,?,?,'review')",
        (bid, account_id, filename[:200], meta.get("fingerprint", ""),
         meta.get("kind", "csv"),
         datetime.now().isoformat(timespec="seconds"),
         dates[0] if dates else None, dates[-1] if dates else None, len(rows)))

    duplicates = 0
    for row in rows:
        existing, how = find_existing(conn, account_id, row)
        is_dup = bool(existing)
        duplicates += 1 if is_dup else 0
        conn.execute(
            "INSERT INTO import_rows (id, batch_id, line_no, raw, date,"
            " amount_cents, payee, payee_norm, notes, dedup_key, is_duplicate,"
            " dup_kind, excluded, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id(), bid, row["line_no"], row["raw"][:500], row["date"],
             row["amount_cents"], row["payee"][:200], row["payee_norm"][:200],
             row["notes"][:200], row.get("dedup_key", ""), int(is_dup), how,
             # Duplicates and unparseable rows are excluded by default. The
             # safe default is to not import; including one is a decision.
             int(is_dup or bool(row["error"])), row["error"]))
    conn.execute("UPDATE import_batches SET rows_duplicate=? WHERE id=?",
                 (duplicates, bid))
    conn.commit()
    return bid, meta


def batch(conn, batch_id):
    return conn.execute("SELECT * FROM import_batches WHERE id=?",
                        (batch_id,)).fetchone()


def batch_rows(conn, batch_id):
    return conn.execute(
        "SELECT * FROM import_rows WHERE batch_id=? ORDER BY line_no",
        (batch_id,)).fetchall()


def set_row(conn, row_id, **fields):
    allowed = {"excluded", "category_id", "payee", "notes", "amount_cents",
               "date", "category_source"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError("cannot set: " + ", ".join(sorted(bad)))
    # Choosing a category while reviewing a statement is a decision, and it
    # has to survive the commit as one -- this is where most filing actually
    # happens, so treating it as a machine guess would waste the best
    # evidence the classifier ever gets.
    if "category_id" in fields and "category_source" not in fields:
        fields["category_source"] = "you" if fields["category_id"] else ""
    sets = ", ".join("%s=?" % k for k in fields)
    values = [int(v) if k == "excluded" else v for k, v in fields.items()]
    if "payee" in fields:
        sets += ", payee_norm=?"
        values.append(norm_payee(fields["payee"]))
    conn.execute("UPDATE import_rows SET %s WHERE id=?" % sets,
                 [*values, row_id])
    conn.commit()


def commit_batch(conn, batch_id, remember_as=""):
    """Write the included rows into the ledger.

    Idempotent by construction: every row carries a dedup key written to the
    transaction's imported_id, and a unique index refuses a second insert. A
    batch committed twice imports nothing the second time.
    """
    from . import ledger

    b = batch(conn, batch_id)
    if b is None:
        raise KeyError(batch_id)
    if b["state"] == "committed":
        return 0

    written = 0
    for row in batch_rows(conn, batch_id):
        if row["excluded"] or row["error"] or row["date"] is None:
            continue
        if row["amount_cents"] is None:
            continue
        try:
            txn = ledger.add_transaction(
                conn, b["account_id"], row["date"], int(row["amount_cents"]),
                payee=row["payee"], category_id=row["category_id"],
                category_source=row["category_source"],
                notes=row["notes"], imported_id=row["dedup_key"] or None,
                source="import", commit=False)
        except Exception:
            # Almost always the unique index refusing a row already imported.
            # Not an error: it is the guarantee working.
            conn.rollback()
            continue
        conn.execute("UPDATE import_rows SET txn_id=? WHERE id=?",
                     (txn, row["id"]))
        written += 1

    conn.execute("UPDATE import_batches SET state='committed', rows_imported=?"
                 " WHERE id=?", (written, batch_id))
    conn.commit()
    refresh_coverage(conn, b["account_id"])
    return written


def discard_batch(conn, batch_id):
    conn.execute("DELETE FROM import_rows WHERE batch_id=?", (batch_id,))
    conn.execute("UPDATE import_batches SET state='discarded' WHERE id=?",
                 (batch_id,))
    conn.commit()


# ── coverage ──────────────────────────────────────────────────────────────
def refresh_coverage(conn, account_id):
    """Recompute which months this account actually has data for.

    The budget engine consults this so it never averages over a month with no
    statement behind it. A missing month drags a mean toward zero, and a
    recommendation built on that is confidently wrong.
    """
    conn.execute("DELETE FROM month_coverage WHERE account_id=?", (account_id,))
    conn.execute(
        "INSERT INTO month_coverage (account_id, month, txn_count, first_day,"
        " last_day) SELECT ?, substr(date,1,7), COUNT(*), MIN(date), MAX(date)"
        " FROM transactions WHERE account_id=? AND deleted=0"
        "   AND parent_id IS NULL GROUP BY substr(date,1,7)",
        (account_id, account_id))
    conn.commit()


def coverage(conn, account_id=None):
    if account_id:
        rows = conn.execute(
            "SELECT month, SUM(txn_count) n, MIN(first_day) a, MAX(last_day) b"
            " FROM month_coverage WHERE account_id=? GROUP BY month"
            " ORDER BY month", (account_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT month, SUM(txn_count) n, MIN(first_day) a, MAX(last_day) b"
            " FROM month_coverage GROUP BY month ORDER BY month").fetchall()
    return [{"month": r["month"], "txn_count": r["n"],
             "first_day": r["a"], "last_day": r["b"]} for r in rows]


def gaps(conn, account_id=None):
    """Months between the first and last covered month that have nothing."""
    have = [c["month"] for c in coverage(conn, account_id)]
    if len(have) < 2:
        return []
    start, end = have[0], have[-1]
    present, missing = set(have), []
    y, m = int(start[:4]), int(start[5:7])
    while "%04d-%02d" % (y, m) <= end:
        key = "%04d-%02d" % (y, m)
        if key not in present:
            missing.append(key)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return missing
