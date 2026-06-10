"""
Warstwa bazy danych (SQLite) dla aplikacji Fakturownia.

Tabele:
  bank_transactions  – transakcje bankowe z importów CSV
  invoices           – faktury ze wszystkich źródeł (ksef_cost / ksef_income / drive)
  tx_invoice_links   – powiązania wiele-do-wielu transakcja ↔ faktura
  manual_actions     – ręczne akcje na transakcjach (skip / dysk)
  keywords           – słowa kluczowe do szarego podświetlenia
  cache_meta         – znaczniki czasu ostatniej aktualizacji
"""

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakturownia.db")
_TZ_PL  = ZoneInfo("Europe/Warsaw")

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS bank_transactions (
    tx_id        TEXT PRIMARY KEY,
    full_date    TEXT NOT NULL DEFAULT '',
    date         TEXT NOT NULL DEFAULT '',
    month        TEXT NOT NULL DEFAULT '',
    kontrahent   TEXT NOT NULL DEFAULT '',
    details      TEXT NOT NULL DEFAULT '',
    amount       REAL NOT NULL DEFAULT 0,
    amount_op    REAL,
    currency     TEXT NOT NULL DEFAULT 'PLN',
    currency_op  TEXT,
    foreign_txt  TEXT NOT NULL DEFAULT '',
    typ          TEXT NOT NULL DEFAULT 'cost',
    document     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id  TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    number      TEXT NOT NULL DEFAULT '',
    full_date   TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL DEFAULT '',
    month       TEXT NOT NULL DEFAULT '',
    who         TEXT NOT NULL DEFAULT '',
    net         REAL,
    vat         REAL,
    gross       REAL NOT NULL DEFAULT 0,
    currency    TEXT NOT NULL DEFAULT 'PLN',
    status      TEXT NOT NULL DEFAULT '',
    file_id     TEXT NOT NULL DEFAULT '',
    filename    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tx_invoice_links (
    link_id    TEXT PRIMARY KEY,
    tx_id      TEXT NOT NULL REFERENCES bank_transactions(tx_id) ON DELETE CASCADE,
    invoice_id TEXT NOT NULL REFERENCES invoices(invoice_id)     ON DELETE CASCADE,
    amount     REAL,
    method     TEXT NOT NULL DEFAULT 'auto',
    created_at TEXT NOT NULL,
    UNIQUE(tx_id, invoice_id)
);

CREATE TABLE IF NOT EXISTS manual_actions (
    tx_id  TEXT PRIMARY KEY,
    action TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keywords (
    keyword TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS cache_meta (
    key        TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_links_tx   ON tx_invoice_links(tx_id);
CREATE INDEX IF NOT EXISTS idx_links_inv  ON tx_invoice_links(invoice_id);
CREATE INDEX IF NOT EXISTS idx_tx_month   ON bank_transactions(month);
CREATE INDEX IF NOT EXISTS idx_inv_month  ON invoices(month);
CREATE INDEX IF NOT EXISTS idx_inv_source ON invoices(source);
"""

_DEFAULT_KEYWORDS = [
    "POBRANIE OPŁATY/PROWIZJI",
    "składka",
    "Urząd Skarbowy",
    "wypłata",
]

# ─────────────────────────────────────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────────────────────────────────────

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def init_db():
    """Create tables if they do not exist yet."""
    with _conn() as c:
        c.executescript(_SCHEMA)

# ─────────────────────────────────────────────────────────────────────────────
# Stable invoice IDs
# ─────────────────────────────────────────────────────────────────────────────

def make_invoice_id(source: str, number: str = "", full_date: str = "",
                    file_id: str = "") -> str:
    """Generate a stable, deterministic invoice_id.

    source:  'ksef_cost' | 'ksef_income' | 'drive'
    """
    if source == "drive":
        return f"drive_{file_id}"
    raw    = f"{source}\x00{(number or '').strip()}\x00{(full_date or '').strip()}"
    hx     = hashlib.md5(raw.encode()).hexdigest()[:12]
    prefix = {"ksef_cost": "kc", "ksef_income": "ki"}.get(source, "inv")
    return f"{prefix}_{hx}"

# ─────────────────────────────────────────────────────────────────────────────
# Sync helpers  (called after each cache refresh)
# ─────────────────────────────────────────────────────────────────────────────

def sync_transactions(transactions: list):
    """Upsert bank transactions from JSON cache list."""
    with _conn() as c:
        for tx in transactions:
            c.execute("""
                INSERT OR REPLACE INTO bank_transactions
                  (tx_id, full_date, date, month, kontrahent, details,
                   amount, amount_op, currency, currency_op, foreign_txt, typ, document)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                tx.get("tx_id", ""),
                tx.get("full_date", ""),
                tx.get("date", ""),
                tx.get("month", ""),
                tx.get("kontrahent", ""),
                tx.get("details", ""),
                float(tx.get("amount", 0)),
                tx.get("amount_op"),
                tx.get("currency", "PLN"),
                tx.get("currency_op"),
                tx.get("foreign", ""),
                tx.get("typ", "cost"),
                tx.get("document", ""),
            ))


def sync_ksef_invoices(invoices: list, source: str):
    """Upsert KSeF invoices.  source = 'ksef_cost' | 'ksef_income'."""
    with _conn() as c:
        for inv in invoices:
            number    = (inv.get("number")    or "").strip()
            full_date = (inv.get("full_date") or "").strip()
            iid       = make_invoice_id(source, number, full_date)
            who       = (inv.get("seller") or inv.get("buyer") or "").strip()
            c.execute("""
                INSERT INTO invoices
                  (invoice_id, source, number, full_date, date, month,
                   who, net, vat, gross, currency, status, file_id, filename)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(invoice_id) DO UPDATE SET
                    status   = excluded.status,
                    who      = excluded.who,
                    net      = excluded.net,
                    vat      = excluded.vat,
                    gross    = excluded.gross,
                    currency = excluded.currency
            """, (
                iid, source, number, full_date,
                inv.get("date", ""),
                inv.get("month", ""),
                who,
                inv.get("net"),
                inv.get("vat"),
                float(inv.get("gross") or 0),
                (inv.get("currency") or "PLN").strip(),
                (inv.get("status")   or "").strip(),
                "", "",
            ))


def sync_drive_invoices(ocr_entries: list):
    """Upsert drive invoices from OCR data list."""
    with _conn() as c:
        for e in ocr_entries:
            file_id = (e.get("file_id") or "").strip()
            if not file_id:
                continue
            iid   = make_invoice_id("drive", file_id=file_id)
            date  = (e.get("date") or "").strip()
            month = date[:7] if len(date) >= 7 else ""
            c.execute("""
                INSERT INTO invoices
                  (invoice_id, source, number, full_date, date, month,
                   who, net, vat, gross, currency, status, file_id, filename)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(invoice_id) DO UPDATE SET
                    who      = excluded.who,
                    net      = excluded.net,
                    vat      = excluded.vat,
                    gross    = excluded.gross,
                    currency = excluded.currency,
                    filename = excluded.filename
            """, (
                iid, "drive",
                (e.get("number")   or "").strip(),
                date, date, month,
                (e.get("seller")   or "").strip(),
                e.get("net"),
                e.get("vat_amount"),
                float(e.get("gross") or 0),
                (e.get("currency") or "PLN").strip(),
                "",
                file_id,
                (e.get("filename") or "").strip(),
            ))

# ─────────────────────────────────────────────────────────────────────────────
# Manual actions
# ─────────────────────────────────────────────────────────────────────────────

def get_manual_actions() -> dict:
    with _conn() as c:
        return {r["tx_id"]: r["action"]
                for r in c.execute("SELECT tx_id, action FROM manual_actions").fetchall()}


def set_manual_action(tx_id: str, action: str | None):
    """Set or remove a manual action.  action=None removes it."""
    with _conn() as c:
        if action:
            c.execute("INSERT OR REPLACE INTO manual_actions (tx_id, action) VALUES (?,?)",
                      (tx_id, action))
        else:
            c.execute("DELETE FROM manual_actions WHERE tx_id=?", (tx_id,))

# ─────────────────────────────────────────────────────────────────────────────
# Keywords
# ─────────────────────────────────────────────────────────────────────────────

def get_keywords() -> list:
    with _conn() as c:
        rows = c.execute("SELECT keyword FROM keywords ORDER BY keyword").fetchall()
    return [r["keyword"] for r in rows]


def add_keyword(kw: str):
    with _conn() as c:
        c.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (kw.strip(),))


def remove_keyword(kw: str):
    with _conn() as c:
        c.execute("DELETE FROM keywords WHERE keyword=?", (kw,))

# ─────────────────────────────────────────────────────────────────────────────
# Links (wiele-do-wielu)
# ─────────────────────────────────────────────────────────────────────────────

def add_link(tx_id: str, invoice_id: str, amount=None,
             method: str = "manual", created_at: str = "") -> bool:
    """Insert a link.  Returns True if a new row was inserted."""
    if not created_at:
        created_at = datetime.now(_TZ_PL).isoformat(timespec="seconds")
    link_id = str(uuid.uuid4())
    try:
        with _conn() as c:
            c.execute("""
                INSERT OR IGNORE INTO tx_invoice_links
                  (link_id, tx_id, invoice_id, amount, method, created_at)
                VALUES (?,?,?,?,?,?)
            """, (link_id, tx_id, invoice_id, amount, method, created_at))
            return c.execute("SELECT changes()").fetchone()[0] > 0
    except sqlite3.IntegrityError:
        return False


def remove_link(tx_id: str, invoice_id: str):
    with _conn() as c:
        c.execute("DELETE FROM tx_invoice_links WHERE tx_id=? AND invoice_id=?",
                  (tx_id, invoice_id))


def remove_auto_links():
    """Remove all automatically created links (before re-running auto-match)."""
    with _conn() as c:
        c.execute("DELETE FROM tx_invoice_links WHERE method='auto'")


def has_any_links() -> bool:
    with _conn() as c:
        return c.execute("SELECT 1 FROM tx_invoice_links LIMIT 1").fetchone() is not None


def get_tx_link_map() -> dict:
    """Return {tx_id: [{'invoice_id', 'number', 'source', 'gross', 'currency',
                         'status', 'who', 'method', 'amount'}, ...]}."""
    with _conn() as c:
        rows = c.execute("""
            SELECT l.tx_id, l.invoice_id, l.method, l.amount,
                   i.number, i.source, i.gross, i.currency, i.status, i.who
            FROM tx_invoice_links l
            JOIN invoices i ON i.invoice_id = l.invoice_id
        """).fetchall()
    result: dict = {}
    for r in rows:
        result.setdefault(r["tx_id"], []).append(dict(r))
    return result


def get_invoice_link_map() -> dict:
    """Return {invoice_id: [{'tx_id', 'method', 'amount', 'full_date',
                              'kontrahent', 'tx_amount'}, ...]}."""
    with _conn() as c:
        rows = c.execute("""
            SELECT l.invoice_id, l.tx_id, l.method, l.amount,
                   t.full_date, t.kontrahent, t.amount AS tx_amount
            FROM tx_invoice_links l
            JOIN bank_transactions t ON t.tx_id = l.tx_id
        """).fetchall()
    result: dict = {}
    for r in rows:
        result.setdefault(r["invoice_id"], []).append(dict(r))
    return result


def delete_invoice(invoice_id: str):
    """Delete an invoice and cascade-delete its tx links."""
    with _conn() as c:
        c.execute("DELETE FROM invoices WHERE invoice_id=?", (invoice_id,))


def update_link_amount(tx_id: str, invoice_id: str, amount: float | None):
    """Set or clear the explicit PLN coverage amount for an existing link."""
    with _conn() as c:
        c.execute(
            "UPDATE tx_invoice_links SET amount=? WHERE tx_id=? AND invoice_id=?",
            (amount, tx_id, invoice_id),
        )


def get_transaction(tx_id: str) -> dict | None:
    """Return a single bank transaction by tx_id."""
    with _conn() as c:
        row = c.execute("SELECT * FROM bank_transactions WHERE tx_id=?", (tx_id,)).fetchone()
    return dict(row) if row else None


def get_links_for_tx(tx_id: str) -> list:
    """Return all invoices linked to a transaction with invoice details."""
    with _conn() as c:
        rows = c.execute("""
            SELECT l.invoice_id, l.method, l.amount AS link_amount,
                   i.source, i.number, i.date, i.full_date, i.who, i.gross, i.currency, i.status
            FROM tx_invoice_links l
            JOIN invoices i ON i.invoice_id = l.invoice_id
            WHERE l.tx_id = ?
            ORDER BY i.full_date DESC
        """, (tx_id,)).fetchall()
    return [dict(r) for r in rows]


def get_invoices_by_source(sources: list, exclude_ids: set | None = None) -> list:
    """Return invoices from given sources, optionally excluding some IDs."""
    if not sources:
        return []
    placeholders = ",".join("?" * len(sources))
    with _conn() as c:
        rows = c.execute(f"""
            SELECT invoice_id, source, number, date, full_date, who, gross, currency, status
            FROM invoices
            WHERE source IN ({placeholders})
            ORDER BY full_date DESC
        """, list(sources)).fetchall()
    result = [dict(r) for r in rows]
    if exclude_ids:
        result = [r for r in result if r["invoice_id"] not in exclude_ids]
    return result


def get_matched_invoice_ids() -> set:
    """Return set of invoice_ids that have at least one link."""
    with _conn() as c:
        return {r["invoice_id"] for r in
                c.execute("SELECT DISTINCT invoice_id FROM tx_invoice_links").fetchall()}


def get_invoices_for_match() -> list:
    """Return invoice list compatible with find_matching_invoice()."""
    with _conn() as c:
        rows = c.execute(
            "SELECT invoice_id, number, gross, full_date, currency, source FROM invoices"
        ).fetchall()
    return [{
        "number":      r["number"],
        "gross":       r["gross"],
        "full_date":   r["full_date"],
        "currency":    r["currency"],
        "ksef":        r["source"] in ("ksef_cost", "ksef_income"),
        "_invoice_id": r["invoice_id"],
    } for r in rows]

# ─────────────────────────────────────────────────────────────────────────────
# Cache metadata
# ─────────────────────────────────────────────────────────────────────────────

def get_cache_updated(key: str) -> str:
    with _conn() as c:
        row = c.execute("SELECT updated_at FROM cache_meta WHERE key=?", (key,)).fetchone()
    return row["updated_at"] if row else ""


def set_cache_updated(key: str, ts: str):
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO cache_meta (key, updated_at) VALUES (?,?)", (key, ts))

# ─────────────────────────────────────────────────────────────────────────────
# One-time migration from existing JSON files
# ─────────────────────────────────────────────────────────────────────────────

def migrate_from_json(app_dir: str):
    """Migrate data from legacy JSON files into SQLite.
    Safe to call multiple times (uses INSERT OR IGNORE / REPLACE).
    """

    # 1. Bank transactions
    path = os.path.join(app_dir, "transakcje_cache.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        sync_transactions(data.get("transactions", []))
        if ts := data.get("updated"):
            set_cache_updated("transactions", ts)

    # 2. KSeF cost
    path = os.path.join(app_dir, "faktury_kosztowe_cache.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        sync_ksef_invoices(data.get("invoices", []), "ksef_cost")
        if ts := data.get("updated"):
            set_cache_updated("ksef_cost", ts)

    # 3. KSeF income
    path = os.path.join(app_dir, "faktury_przychodowe_cache.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        sync_ksef_invoices(data.get("invoices", []), "ksef_income")
        if ts := data.get("updated"):
            set_cache_updated("ksef_income", ts)

    # 4. Drive / OCR invoices
    path = os.path.join(app_dir, "faktury_ocr.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        sync_drive_invoices(data if isinstance(data, list) else [])

    # 5. Manual actions
    path = os.path.join(app_dir, "manual_actions.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        with _conn() as c:
            for tx_id, action in (data if isinstance(data, dict) else {}).items():
                c.execute("INSERT OR IGNORE INTO manual_actions (tx_id, action) VALUES (?,?)",
                          (tx_id, action))

    # 6. Keywords — prefer existing file, else seed defaults
    path = os.path.join(app_dir, "grey_keywords.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        with _conn() as c:
            for kw in (data if isinstance(data, list) else []):
                c.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (kw,))
    else:
        with _conn() as c:
            for kw in _DEFAULT_KEYWORDS:
                c.execute("INSERT OR IGNORE INTO keywords (keyword) VALUES (?)", (kw,))
