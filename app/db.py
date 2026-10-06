"""
db.py — SQLite persistence for leads moving through the enrichment pipeline.

SQLite + a status column is the storage choice for this project, not Postgres
+ Redis — intentionally. It's a real, legitimate pattern (not a toy): a
status column plus a polling worker is how plenty of production systems
implement a job queue before traffic justifies a dedicated broker. See
README for the honest migration path to Redis/RQ or Celery at higher
throughput; the pipeline logic in pipeline.py doesn't change either way,
only how a lead gets picked up for processing.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).parent.parent / "leads.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    domain TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    raw_website_text TEXT,
    enrichment_summary TEXT,       -- JSON: {what_they_do, likely_industry, signals}
    qualified INTEGER,             -- 0/1/NULL
    qualification_reason TEXT,
    draft_subject TEXT,
    draft_body TEXT,
    grading_grounded INTEGER,      -- 0/1/NULL
    grading_personalized INTEGER,  -- 0/1/NULL
    grading_notes TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Valid pipeline states, roughly in the order a lead moves through them.
# "pending" is the only state the worker looks for new work in.
STATUSES = [
    "pending", "enriching", "qualifying", "disqualified",
    "drafting", "grading", "flagged_for_review", "pending_approval",
    "approved", "rejected", "failed",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db


async def init_db() -> None:
    async with get_db() as db:
        await db.execute(SCHEMA)
        await db.commit()


async def create_lead(company_name: str, domain: str) -> int:
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO leads (company_name, domain, status, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (company_name, domain, _now(), _now()),
        )
        await db.commit()
        return cur.lastrowid


async def get_lead(lead_id: int) -> dict | None:
    async with get_db() as db:
        async with db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_leads(status: str | None = None) -> list[dict]:
    async with get_db() as db:
        if status:
            query, params = "SELECT * FROM leads WHERE status = ? ORDER BY id DESC", (status,)
        else:
            query, params = "SELECT * FROM leads ORDER BY id DESC", ()
        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def claim_next_pending() -> dict | None:
    """Atomically claim one pending lead for processing.

    SQLite serializes writes, so this transaction can't race with another
    worker claiming the same row — the second claim attempt simply finds
    nothing left in 'pending' once the first has flipped the status.
    """
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM leads WHERE status = 'pending' ORDER BY id LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        lead = dict(row)
        await db.execute(
            "UPDATE leads SET status = 'enriching', updated_at = ? WHERE id = ?",
            (_now(), lead["id"]),
        )
        await db.commit()
        lead["status"] = "enriching"
        return lead


async def update_lead(lead_id: int, **fields) -> None:
    """Update arbitrary columns on a lead. dict/list values are JSON-encoded."""
    if not fields:
        return
    encoded = {
        k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
        for k, v in fields.items()
    }
    encoded["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in encoded)
    async with get_db() as db:
        await db.execute(
            f"UPDATE leads SET {set_clause} WHERE id = ?",
            (*encoded.values(), lead_id),
        )
        await db.commit()
