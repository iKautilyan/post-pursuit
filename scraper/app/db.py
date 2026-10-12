import sqlite3
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "jobs.db"


# --------------------------------------------------
# Connection
# --------------------------------------------------

def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    return conn


# --------------------------------------------------
# Schema
# --------------------------------------------------

def init_db():
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id          TEXT PRIMARY KEY,
            title           TEXT,
            company         TEXT,
            location        TEXT,
            url             TEXT,
            description     TEXT,
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            search_keywords TEXT
        )
    """)

    # Migration: add filter + scoring columns.
    migrations = [
        "seniority TEXT",
        "work_model TEXT",
        "employment_type TEXT",
        "requires_german TEXT",
        "visa_mentioned INTEGER DEFAULT 0",
        "tech_stack TEXT",
        "filter_status TEXT",
        "filter_reasons TEXT",
        "score REAL",
        "scored_at TEXT",
        "resume_path TEXT",
        "cover_letter_path TEXT",
        "tailored_at TEXT",
        "application_status TEXT DEFAULT 'not_applied'",
        "applied_at TEXT",
        "application_notes TEXT",
    ]

    for col_def in migrations:
        col_name = col_def.split()[0]

        try:
            conn.execute(
                f"ALTER TABLE jobs ADD COLUMN {col_def}"
            )
        except Exception:
            pass  # Column already exists.

    conn.commit()
    conn.close()


# --------------------------------------------------
# Upsert
# --------------------------------------------------

def upsert_job(conn, job):
    """Insert a new job or update an existing one.

    - first_seen_at is set only on first insert.
    - last_seen_at is always refreshed.
    - All other fields are updated to the latest values.
    """
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO jobs (
            job_id,
            title,
            company,
            location,
            url,
            description,
            first_seen_at,
            last_seen_at,
            search_keywords
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            title           = excluded.title,
            company         = excluded.company,
            location        = excluded.location,
            url             = excluded.url,
            description     = COALESCE(
                excluded.description,
                jobs.description
            ),
            last_seen_at    = excluded.last_seen_at,
            search_keywords = excluded.search_keywords
        """,
        (
            job.get("job_id"),
            job.get("title"),
            job.get("company"),
            job.get("location"),
            job.get("url"),
            job.get("description"),
            now,
            now,
            job.get("search_keywords"),
        ),
    )


def save_jobs(jobs):
    """Upsert a list of jobs into the database."""
    init_db()

    conn = get_connection()

    count_before = conn.execute(
        "SELECT COUNT(*) FROM jobs"
    ).fetchone()[0]

    for job in jobs:
        upsert_job(conn, job)

    conn.commit()

    count_after = conn.execute(
        "SELECT COUNT(*) FROM jobs"
    ).fetchone()[0]

    conn.close()

    new_count = count_after - count_before

    print()
    print(
        f"DB: {count_after} total jobs "
        f"({new_count} new, "
        f"{count_after - new_count} updated/existing)"
    )


# --------------------------------------------------
# Queries
# --------------------------------------------------

def get_all_jobs():
    init_db()

    conn = get_connection()

    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY last_seen_at DESC"
    ).fetchall()

    conn.close()

    return [dict(row) for row in rows]


def get_job(job_id):
    init_db()

    import urllib.parse
    clean_id = urllib.parse.unquote(str(job_id)).strip().strip('"').strip("'") if job_id is not None else ""
    conn = get_connection()

    row = conn.execute(
        "SELECT * FROM jobs WHERE job_id = ?",
        (clean_id,),
    ).fetchone()

    conn.close()

    return dict(row) if row else None


def get_unfiltered_jobs():
    """Jobs that haven't been filtered yet."""
    init_db()

    conn = get_connection()

    rows = conn.execute(
        "SELECT * FROM jobs "
        "WHERE filter_status IS NULL "
        "ORDER BY last_seen_at DESC"
    ).fetchall()

    conn.close()

    return [dict(row) for row in rows]


def get_passed_jobs():
    """Jobs that passed hard filters."""
    init_db()

    conn = get_connection()

    rows = conn.execute(
        "SELECT * FROM jobs "
        "WHERE filter_status = 'passed' "
        "ORDER BY score DESC"
    ).fetchall()

    conn.close()

    return [dict(row) for row in rows]


def get_jobs_missing_description(limit=None):
    """Retrieve jobs that don't have a description scraped yet."""
    init_db()

    conn = get_connection()

    query = (
        "SELECT * FROM jobs "
        "WHERE description IS NULL "
        "ORDER BY last_seen_at DESC"
    )
    if limit is not None:
        query += f" LIMIT {int(limit)}"

    rows = conn.execute(query).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def update_job_description(job_id, description):
    """Update a single job's description in the database immediately."""
    init_db()

    clean_id = str(job_id).strip().strip('"').strip("'") if job_id is not None else ""
    conn = get_connection()

    conn.execute(
        "UPDATE jobs SET description = ? WHERE job_id = ?",
        (description, clean_id),
    )

    conn.commit()
    conn.close()


def get_shortlist(min_score=7.0, only_pending=False, only_unapplied=True):
    """Jobs above the score threshold.
    If only_unapplied=True, excludes jobs already marked as applied (so pending reminders continue).
    If only_pending=True, only jobs without generated resume.
    """
    init_db()

    conn = get_connection()

    sql = """
        SELECT * FROM jobs 
        WHERE filter_status = 'passed' 
        AND score >= ?
    """
    if only_pending:
        sql += " AND (resume_path IS NULL OR resume_path = '')"
    if only_unapplied:
        sql += " AND (application_status IS NULL OR application_status != 'applied')"
    sql += " ORDER BY score DESC, last_seen_at DESC"

    rows = conn.execute(sql, (min_score,)).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def get_new_shortlist(min_score=7.0, hours=12, only_pending=False, only_unapplied=True):
    """Jobs first discovered within the last N hours that pass the score threshold.
    If only_unapplied=True, excludes jobs already marked as applied.
    """
    init_db()

    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    conn = get_connection()

    sql = """
        SELECT * FROM jobs 
        WHERE filter_status = 'passed' 
        AND score >= ? 
        AND first_seen_at >= ?
    """
    if only_pending:
        sql += " AND (resume_path IS NULL OR resume_path = '')"
    if only_unapplied:
        sql += " AND (application_status IS NULL OR application_status != 'applied')"
    sql += " ORDER BY score DESC, first_seen_at DESC"

    rows = conn.execute(sql, (min_score, cutoff)).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def save_tailored_docs(job_id, resume_path, cover_letter_path):
    """Save file paths of generated tailored documents for a job."""
    init_db()

    clean_id = str(job_id).strip().strip('"').strip("'") if job_id is not None else ""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()

    conn.execute(
        """
        UPDATE jobs SET
            resume_path = ?,
            cover_letter_path = ?,
            tailored_at = ?
        WHERE job_id = ?
        """,
        (resume_path, cover_letter_path, now, clean_id),
    )

    conn.commit()
    conn.close()


def update_filter_result(job_id, result):
    """Persist a FilterResult back to the DB."""
    import json

    now = datetime.now(timezone.utc).isoformat()

    conn = get_connection()

    conn.execute(
        """
        UPDATE jobs SET
            seniority       = ?,
            work_model      = ?,
            employment_type = ?,
            requires_german = ?,
            visa_mentioned  = ?,
            tech_stack      = ?,
            filter_status   = ?,
            filter_reasons  = ?,
            score           = ?,
            scored_at       = ?
        WHERE job_id = ?
        """,
        (
            result.seniority,
            result.work_model,
            result.employment_type,
            result.requires_german,
            1 if result.visa_mentioned else 0,
            json.dumps(result.tech_stack),
            result.status,
            json.dumps(result.reasons),
            result.score,
            now,
            job_id,
        ),
    )

    conn.commit()
    conn.close()


def update_application_status(job_id, status, notes=None):
    """Update application status of a job (e.g. 'applied', 'ready_to_submit', 'external_redirect', 'failed')."""
    init_db()

    import urllib.parse
    clean_id = urllib.parse.unquote(str(job_id)).strip().strip('"').strip("'") if job_id is not None else ""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()

    applied_time = now if status in ("applied", "ready_to_submit") else None

    conn.execute(
        """
        UPDATE jobs SET
            application_status = ?,
            applied_at = COALESCE(?, applied_at),
            application_notes = ?
        WHERE job_id = ?
        """,
        (status, applied_time, notes, clean_id),
    )

    conn.commit()
    conn.close()


def get_applied_jobs():
    """Retrieve jobs that have been applied or are ready for review."""
    init_db()

    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE application_status IN ('applied', 'ready_to_submit', 'external_redirect') ORDER BY applied_at DESC"
    ).fetchall()
    conn.close()

    return [dict(row) for row in rows]
