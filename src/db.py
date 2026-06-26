"""
db.py
Database layer for the Job Discovery & Outreach Drafting Pipeline.

Handles schema creation, lead insertion with domain normalization and
fuzzy duplicate detection, email validation, status transitions, and
funnel statistics. No LLM or network calls live in this module, it is
fully testable in isolation with an in-memory SQLite database.

Dependencies: rapidfuzz (pip install rapidfuzz)
Expected location: src/db.py (DEFAULT_DB_PATH assumes a sibling data/ folder)
"""

import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz

logger = logging.getLogger("db")

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "leads.db"

CATEGORIES = {"AI Engineer"}
PROCESSING_MODES = {"Automatic", "Manual"}
EMAIL_SOURCES = {"ddg_snippet", "crawler", "manual"}
STATUSES = {
    "Pending Email",
    "Ready for Draft",
    "Drafted",
    "Sent",
    "Replied",
    "No Response",
    "Interview",
}

EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# Multi-part TLDs that need an extra label to find the real root domain.
# Not exhaustive, covers the suffixes most likely to show up in this pipeline.
COMPOUND_SUFFIXES = {
    "co.uk", "co.in", "co.nz", "co.za", "co.jp",
    "com.pk", "com.au", "com.br", "com.sg",
    "net.pk", "org.pk", "gov.pk", "edu.pk", "ac.uk",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    domain TEXT UNIQUE NOT NULL,
    category TEXT CHECK(category IN ('AI Engineer', 'Python Developer')) NOT NULL,
    contact_email TEXT DEFAULT NULL,
    email_source TEXT CHECK(email_source IN ('ddg_snippet', 'crawler', 'manual')) DEFAULT NULL,
    processing_mode TEXT CHECK(processing_mode IN ('Automatic', 'Manual')) NOT NULL,
    status TEXT CHECK(status IN (
        'Pending Email', 'Ready for Draft', 'Drafted',
        'Sent', 'Replied', 'No Response', 'Interview'
    )) NOT NULL DEFAULT 'Pending Email',
    source_query TEXT DEFAULT NULL,
    date_added TEXT DEFAULT CURRENT_TIMESTAMP,
    date_sent TEXT DEFAULT NULL,
    notes TEXT DEFAULT NULL
);
"""

# ---------------------------------------------------------------------------
# Connection / schema setup
# ---------------------------------------------------------------------------


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection, creating the parent directory if needed."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the leads table if it does not already exist."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ---------------------------------------------------------------------------
# Domain normalization
# ---------------------------------------------------------------------------


def normalize_domain(raw: str) -> str:
    """
    Reduce a URL or domain string to its root domain.

    Strips scheme, path, port, and 'www.', then collapses subdomains
    like 'careers.acme.com' down to 'acme.com'. Handles a small set of
    common compound TLDs (e.g. 'acme.com.pk' stays intact instead of
    being cut to 'com.pk').
    """
    value = raw.strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = value.split("/")[0]
    value = value.split(":")[0]
    if value.startswith("www."):
        value = value[4:]

    labels = value.split(".")
    if len(labels) <= 2:
        return value

    last_two = ".".join(labels[-2:])
    last_three = ".".join(labels[-3:])
    if last_two in COMPOUND_SUFFIXES:
        return last_three
    return last_two


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def find_probable_duplicate(
    conn: sqlite3.Connection,
    company_name: str,
    domain: str,
    threshold: int = 90,
) -> Optional[sqlite3.Row]:
    """
    Fuzzy-match company_name against existing rows to catch duplicates
    the UNIQUE domain constraint misses (same company under a different
    domain, or the same name with a legal suffix added/dropped, e.g.
    'Acme Software' vs 'Acme Software Pvt Ltd'). Uses token_set_ratio
    rather than token_sort_ratio specifically because it ignores extra
    tokens like 'Pvt Ltd' instead of penalizing the length mismatch.

    Known limitation: this will not catch abbreviation-style variants
    like 'Acme Tech' vs 'Acme Technologies', those still need a manual
    pass in the SQLite Viewer (see Phase 3 of AppFlow.md).
    """
    existing = conn.execute("SELECT id, company_name, domain FROM leads").fetchall()
    for row in existing:
        if row["domain"] == domain:
            continue
        score = fuzz.token_set_ratio(company_name.lower(), row["company_name"].lower())
        if score >= threshold:
            return row
    return None


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------


def insert_lead(
    conn: sqlite3.Connection,
    company_name: str,
    raw_domain: str,
    category: str,
    processing_mode: str = "Automatic",
    contact_email: Optional[str] = None,
    email_source: Optional[str] = None,
    source_query: Optional[str] = None,
    fuzzy_threshold: int = 90,
) -> Optional[int]:
    """
    Insert a new lead. Returns the new row id, or None if the lead was
    skipped as a duplicate (exact domain match or fuzzy company name match).

    Raises ValueError on invalid category/processing_mode/email_source.
    """
    if category not in CATEGORIES:
        raise ValueError(f"Invalid category: {category!r}")
    if processing_mode not in PROCESSING_MODES:
        raise ValueError(f"Invalid processing_mode: {processing_mode!r}")
    if email_source is not None and email_source not in EMAIL_SOURCES:
        raise ValueError(f"Invalid email_source: {email_source!r}")

    domain = normalize_domain(raw_domain)

    duplicate = find_probable_duplicate(conn, company_name, domain, fuzzy_threshold)
    if duplicate is not None:
        logger.info(
            "Skipped probable duplicate '%s' (matches existing '%s', id=%d)",
            company_name, duplicate["company_name"], duplicate["id"],
        )
        return None

    status = "Pending Email"
    if contact_email:
        if EMAIL_RE.match(contact_email):
            status = "Ready for Draft"
        else:
            logger.warning(
                "Discarding invalid email for '%s': %s", company_name, contact_email
            )
            contact_email = None
            email_source = None

    try:
        cur = conn.execute(
            """
            INSERT INTO leads (
                company_name, domain, category, contact_email,
                email_source, processing_mode, status, source_query
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_name, domain, category, contact_email,
                email_source, processing_mode, status, source_query,
            ),
        )
        conn.commit()
        logger.info("Inserted lead '%s' (%s), id=%d", company_name, domain, cur.lastrowid)
        return cur.lastrowid
    except sqlite3.IntegrityError:
        logger.info("Domain already exists, skipping: %s", domain)
        return None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_leads_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    if status not in STATUSES:
        raise ValueError(f"Invalid status: {status!r}")
    return conn.execute(
        "SELECT * FROM leads WHERE status = ? ORDER BY date_added", (status,)
    ).fetchall()


def get_lead_by_id(conn: sqlite3.Connection, lead_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


def get_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Funnel counts by status, used by the `stats` CLI command."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM leads GROUP BY status"
    ).fetchall()
    counts = {row["status"]: row["count"] for row in rows}
    for status in STATUSES:
        counts.setdefault(status, 0)
    counts["Total"] = sum(counts[s] for s in STATUSES)
    return counts


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------


def update_email(
    conn: sqlite3.Connection,
    lead_id: int,
    email: str,
    source: str = "manual",
) -> None:
    """
    Attach a verified email to a lead and advance it to 'Ready for Draft'.
    Used by both the enrich crawler (source='crawler') and manual entry
    (source='manual').
    """
    if not EMAIL_RE.match(email):
        raise ValueError(f"Invalid email format: {email!r}")
    if source not in EMAIL_SOURCES:
        raise ValueError(f"Invalid email_source: {source!r}")

    conn.execute(
        """
        UPDATE leads
        SET contact_email = ?, email_source = ?, status = 'Ready for Draft'
        WHERE id = ?
        """,
        (email, source, lead_id),
    )
    conn.commit()
    logger.info("Updated email for lead id=%d, source=%s", lead_id, source)


def update_status(
    conn: sqlite3.Connection,
    lead_id: int,
    new_status: str,
    notes: Optional[str] = None,
) -> None:
    """
    Move a lead to a new status. Stamps date_sent automatically when
    moving to 'Sent'. Used for both pipeline transitions (Drafted) and
    manual outcome tracking (Sent, Replied, No Response, Interview).
    """
    if new_status not in STATUSES:
        raise ValueError(f"Invalid status: {new_status!r}")

    if new_status == "Sent":
        conn.execute(
            """
            UPDATE leads
            SET status = ?, date_sent = CURRENT_TIMESTAMP, notes = COALESCE(?, notes)
            WHERE id = ?
            """,
            (new_status, notes, lead_id),
        )
    else:
        conn.execute(
            "UPDATE leads SET status = ?, notes = COALESCE(?, notes) WHERE id = ?",
            (new_status, notes, lead_id),
        )
    conn.commit()
    logger.info("Lead id=%d moved to status '%s'", lead_id, new_status)


# ---------------------------------------------------------------------------
# Manual run: initialize the database
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    connection = get_connection()
    init_db(connection)
    logger.info("Database ready at %s", DEFAULT_DB_PATH)
    connection.close()