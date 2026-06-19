import sqlite3
import os
from typing import Optional, List
from .models import (
    Batch, Invoice, Payment, Match, RuleVersion, Adjudication,
    BatchStatus, MatchStatus,
)

DEFAULT_DB_PATH = "inv_recon.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL UNIQUE,
    tolerance REAL NOT NULL DEFAULT 0.01,
    require_vendor_match INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'imported'
        CHECK (status IN ('imported','matched','reviewed','exported','revoked')),
    rule_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    invoice_no TEXT NOT NULL,
    vendor TEXT NOT NULL,
    amount REAL NOT NULL,
    date TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES batches(id)
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    payment_no TEXT NOT NULL,
    vendor TEXT NOT NULL,
    amount REAL NOT NULL,
    date TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES batches(id)
);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    invoice_id INTEGER,
    payment_id INTEGER,
    match_type TEXT NOT NULL DEFAULT 'exact',
    amount_diff REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','confirmed','rejected','conflict')),
    review_note TEXT,
    adjudication TEXT,
    FOREIGN KEY (batch_id) REFERENCES batches(id),
    FOREIGN KEY (invoice_id) REFERENCES invoices(id),
    FOREIGN KEY (payment_id) REFERENCES payments(id)
);

CREATE TABLE IF NOT EXISTS adjudications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER,
    batch_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    note TEXT,
    prev_status TEXT,
    prev_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (match_id) REFERENCES matches(id),
    FOREIGN KEY (batch_id) REFERENCES batches(id)
);
"""


def get_db_path() -> str:
    return os.environ.get("INV_RECON_DB", DEFAULT_DB_PATH)


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO rule_versions (version, tolerance, require_vendor_match) "
                "VALUES ('v1', 0.01, 1)"
            )
            _migrate_adj_prev_status(conn)
    finally:
        conn.close()


def _migrate_adj_prev_status(conn) -> None:
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(adjudications)").fetchall()]
    if "prev_status" not in cols:
        conn.execute("ALTER TABLE adjudications ADD COLUMN prev_status TEXT")
    if "prev_note" not in cols:
        conn.execute("ALTER TABLE adjudications ADD COLUMN prev_note TEXT")


def get_current_rule(db_path: Optional[str] = None) -> RuleVersion:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM rule_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return RuleVersion(version="v1", tolerance=0.01, require_vendor_match=True)
        return _row_to_rule(row)
    finally:
        conn.close()


def get_rule_by_version(version: str, db_path: Optional[str] = None) -> Optional[RuleVersion]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM rule_versions WHERE version = ?", (version,)
        ).fetchone()
        return _row_to_rule(row) if row else None
    finally:
        conn.close()


def create_rule_version(tolerance: float, require_vendor_match: bool,
                        db_path: Optional[str] = None) -> RuleVersion:
    conn = connect(db_path)
    try:
        with conn:
            last = conn.execute(
                "SELECT version FROM rule_versions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last is None:
                new_ver = "v1"
            else:
                num = int(last["version"].lstrip("v")) + 1
                new_ver = f"v{num}"
            conn.execute(
                "INSERT INTO rule_versions (version, tolerance, require_vendor_match) "
                "VALUES (?, ?, ?)",
                (new_ver, tolerance, 1 if require_vendor_match else 0),
            )
        return get_rule_by_version(new_ver, db_path)
    finally:
        conn.close()


def create_batch(name: str, rule_version: str,
                 db_path: Optional[str] = None) -> Batch:
    conn = connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO batches (name, rule_version) VALUES (?, ?)",
                (name, rule_version),
            )
            bid = cur.lastrowid
        return get_batch(bid, db_path)
    finally:
        conn.close()


def get_batch(batch_id: int, db_path: Optional[str] = None) -> Optional[Batch]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        return _row_to_batch(row) if row else None
    finally:
        conn.close()


def list_batches(db_path: Optional[str] = None) -> List[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute("""
            SELECT b.*,
                   COUNT(DISTINCT i.id) AS invoice_count,
                   COUNT(DISTINCT p.id) AS payment_count,
                   COUNT(DISTINCT m.id) AS match_count,
                   COUNT(DISTINCT CASE WHEN m.status='confirmed' THEN m.id END) AS confirmed_count,
                   COUNT(DISTINCT CASE WHEN m.status='rejected' THEN m.id END) AS rejected_count,
                   COUNT(DISTINCT CASE WHEN m.status='conflict' THEN m.id END) AS conflict_count,
                   COUNT(DISTINCT CASE WHEN m.status='pending' THEN m.id END) AS pending_count
            FROM batches b
            LEFT JOIN invoices i ON i.batch_id = b.id
            LEFT JOIN payments p ON p.batch_id = b.id
            LEFT JOIN matches m ON m.batch_id = b.id
            GROUP BY b.id
            ORDER BY b.id DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def update_batch_status(batch_id: int, status: str,
                        db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE batches SET status = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (status, batch_id),
            )
    finally:
        conn.close()


def insert_invoices(invoices: List[Invoice], db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO invoices (batch_id, invoice_no, vendor, amount, date) "
                "VALUES (?, ?, ?, ?, ?)",
                [(i.batch_id, i.invoice_no, i.vendor, i.amount, i.date) for i in invoices],
            )
    finally:
        conn.close()


def insert_payments(payments: List[Payment], db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO payments (batch_id, payment_no, vendor, amount, date) "
                "VALUES (?, ?, ?, ?, ?)",
                [(p.batch_id, p.payment_no, p.vendor, p.amount, p.date) for p in payments],
            )
    finally:
        conn.close()


def get_invoices_by_batch(batch_id: int,
                          db_path: Optional[str] = None) -> List[Invoice]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM invoices WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall()
        return [_row_to_invoice(r) for r in rows]
    finally:
        conn.close()


def get_payments_by_batch(batch_id: int,
                          db_path: Optional[str] = None) -> List[Payment]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM payments WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall()
        return [_row_to_payment(r) for r in rows]
    finally:
        conn.close()


def insert_matches(matches: List[Match], db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO matches "
                "(batch_id, invoice_id, payment_id, match_type, amount_diff, status, review_note, adjudication) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (m.batch_id, m.invoice_id, m.payment_id, m.match_type,
                     m.amount_diff, m.status, m.review_note, m.adjudication)
                    for m in matches
                ],
            )
    finally:
        conn.close()


def get_matches_by_batch(batch_id: int,
                         db_path: Optional[str] = None) -> List[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute("""
            SELECT m.*,
                   i.invoice_no, i.vendor AS inv_vendor, i.amount AS inv_amount, i.date AS inv_date,
                   p.payment_no, p.vendor AS pay_vendor, p.amount AS pay_amount, p.date AS pay_date
            FROM matches m
            LEFT JOIN invoices i ON i.id = m.invoice_id
            LEFT JOIN payments p ON p.id = m.payment_id
            WHERE m.batch_id = ?
            ORDER BY m.id
        """, (batch_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_match(match_id: int, db_path: Optional[str] = None) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute("""
            SELECT m.*,
                   i.invoice_no, i.vendor AS inv_vendor, i.amount AS inv_amount, i.date AS inv_date,
                   p.payment_no, p.vendor AS pay_vendor, p.amount AS pay_amount, p.date AS pay_date
            FROM matches m
            LEFT JOIN invoices i ON i.id = m.invoice_id
            LEFT JOIN payments p ON p.id = m.payment_id
            WHERE m.id = ?
        """, (match_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_match(match_id: int, status: str, adjudication: Optional[str],
                 review_note: Optional[str],
                 db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE matches SET status = ?, adjudication = ?, review_note = ? "
                "WHERE id = ?",
                (status, adjudication, review_note, match_id),
            )
    finally:
        conn.close()


def insert_adjudication(match_id: int, batch_id: int, action: str,
                        note: Optional[str],
                        prev_status: Optional[str] = None,
                        prev_note: Optional[str] = None,
                        db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO adjudications (match_id, batch_id, action, note, prev_status, prev_note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (match_id, batch_id, action, note, prev_status, prev_note),
            )
    finally:
        conn.close()


def insert_adjudications_bulk(adjudications: List[dict],
                              db_path: Optional[str] = None) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.executemany(
                "INSERT INTO adjudications (match_id, batch_id, action, note, prev_status, prev_note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (a.get("match_id"), a.get("batch_id"), a.get("action"),
                     a.get("note"), a.get("prev_status"), a.get("prev_note"))
                    for a in adjudications
                ],
            )
    finally:
        conn.close()


def get_latest_adjudication(match_id: int,
                            db_path: Optional[str] = None) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM adjudications WHERE match_id = ? ORDER BY id DESC LIMIT 1",
            (match_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_auto_rejected_siblings(match_id: int,
                               db_path: Optional[str] = None) -> List[dict]:
    conn = connect(db_path)
    try:
        m = conn.execute(
            "SELECT invoice_id, payment_id, batch_id FROM matches WHERE id = ?",
            (match_id,),
        ).fetchone()
        if m is None:
            return []
        conditions = []
        params: list = []
        if m["invoice_id"] is not None:
            conditions.append("invoice_id = ?")
            params.append(m["invoice_id"])
        if m["payment_id"] is not None:
            conditions.append("payment_id = ?")
            params.append(m["payment_id"])
        if not conditions:
            return []
        params.append(match_id)
        rows = conn.execute(
            f"SELECT * FROM matches "
            f"WHERE ({' OR '.join(conditions)}) AND id != ? "
            f"AND adjudication = 'auto_rejected' AND status = 'rejected'",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_latest_adjudication_before(match_id: int, before_action: str,
                                   db_path: Optional[str] = None) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM adjudications "
            "WHERE match_id = ? AND action = ? "
            "ORDER BY id DESC LIMIT 1",
            (match_id, before_action),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_adjudication_by_id(adj_id: int,
                           db_path: Optional[str] = None) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM adjudications WHERE id = ?",
            (adj_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_adjudications_by_batch(batch_id: int,
                               db_path: Optional[str] = None) -> List[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM adjudications WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_conflict_matches_for_invoice(invoice_id: int, exclude_match_id: int,
                                     db_path: Optional[str] = None) -> List[Match]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM matches WHERE invoice_id = ? AND id != ? AND status = 'conflict'",
            (invoice_id, exclude_match_id),
        ).fetchall()
        return [_row_to_match(r) for r in rows]
    finally:
        conn.close()


def get_conflict_matches_for_payment(payment_id: int, exclude_match_id: int,
                                     db_path: Optional[str] = None) -> List[Match]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM matches WHERE payment_id = ? AND id != ? AND status = 'conflict'",
            (payment_id, exclude_match_id),
        ).fetchall()
        return [_row_to_match(r) for r in rows]
    finally:
        conn.close()


def count_pending_matches(batch_id: int,
                          db_path: Optional[str] = None) -> int:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM matches WHERE batch_id = ? AND status IN ('pending','conflict')",
            (batch_id,),
        ).fetchone()
        return row["cnt"]
    finally:
        conn.close()


def _row_to_rule(row) -> RuleVersion:
    return RuleVersion(
        id=row["id"],
        version=row["version"],
        tolerance=row["tolerance"],
        require_vendor_match=bool(row["require_vendor_match"]),
        created_at=row["created_at"],
    )


def _row_to_batch(row) -> Batch:
    return Batch(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        rule_version=row["rule_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_invoice(row) -> Invoice:
    return Invoice(
        id=row["id"],
        batch_id=row["batch_id"],
        invoice_no=row["invoice_no"],
        vendor=row["vendor"],
        amount=row["amount"],
        date=row["date"],
    )


def _row_to_payment(row) -> Payment:
    return Payment(
        id=row["id"],
        batch_id=row["batch_id"],
        payment_no=row["payment_no"],
        vendor=row["vendor"],
        amount=row["amount"],
        date=row["date"],
    )


def get_batch_raw(batch_id: int,
                  db_path: Optional[str] = None) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_invoices_raw_by_batch(batch_id: int,
                              db_path: Optional[str] = None) -> List[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM invoices WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_payments_raw_by_batch(batch_id: int,
                              db_path: Optional[str] = None) -> List[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM payments WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_matches_raw_by_batch(batch_id: int,
                             db_path: Optional[str] = None) -> List[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM matches WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_rule_version_raw(version: str,
                         db_path: Optional[str] = None) -> Optional[dict]:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM rule_versions WHERE version = ?", (version,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _row_to_match(row) -> Match:
    return Match(
        id=row["id"],
        batch_id=row["batch_id"],
        invoice_id=row["invoice_id"],
        payment_id=row["payment_id"],
        match_type=row["match_type"],
        amount_diff=row["amount_diff"],
        status=row["status"],
        review_note=row["review_note"],
        adjudication=row["adjudication"],
    )
