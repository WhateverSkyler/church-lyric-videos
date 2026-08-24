"""SQLite-backed job store shared by the web UI and the worker API.

SQLite rather than Redis or Postgres because the whole point of this queue is
that it survives a reboot of a 2 GB VPS without needing another daemon
resident in memory. Throughput is a few songs a week; durability and zero
operational weight matter far more than speed.

The one piece of real concurrency — a worker claiming the next job — is done
with a single conditional UPDATE, so two workers polling at the same instant
can never take the same job.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    artist        TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'lyric_video',
    source_ref    TEXT NOT NULL DEFAULT '',
    original_ref  TEXT NOT NULL DEFAULT '',
    theme         TEXT NOT NULL DEFAULT 'cinematic-warm',
    transpose     INTEGER NOT NULL DEFAULT 0,
    stage         TEXT NOT NULL DEFAULT 'queued',
    error         TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    lyrics        TEXT NOT NULL DEFAULT '',
    requested_by  TEXT NOT NULL DEFAULT '',
    alignment_confidence REAL NOT NULL DEFAULT 0,
    output_name   TEXT NOT NULL DEFAULT '',
    output_bytes  INTEGER NOT NULL DEFAULT 0,
    claimed_by    TEXT NOT NULL DEFAULT '',
    claimed_at    REAL NOT NULL DEFAULT 0,
    progress      TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_stage ON jobs(stage);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC);
"""

#: Stages a worker is allowed to pick up, and what it moves them to.
CLAIMABLE = {"queued": "fetching", "approved": "rendering"}

#: A job claimed but silent for this long is assumed dead and re-queued.
STALE_CLAIM_SECONDS = 45 * 60


#: Columns added after the first release. CREATE TABLE IF NOT EXISTS does
#: nothing to a database that already exists, so a new column has to be added
#: explicitly or an upgraded dashboard fails on every query against a queue
#: that predates it.
MIGRATIONS = (
    ("transpose", "INTEGER NOT NULL DEFAULT 0"),
)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            existing = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
            for column, spec in MIGRATIONS:
                if column not in existing:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {column} {spec}")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=20.0,
                               detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        # WAL lets the web UI read while a worker is writing progress.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- writing ------------------------------------------------------

    def create(self, **fields) -> str:
        job_id = fields.pop("id", None) or uuid.uuid4().hex[:10]
        now = time.time()
        cols = {
            "id": job_id,
            "created_at": now,
            "updated_at": now,
            **{k: v for k, v in fields.items() if v is not None},
        }
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        with self._conn() as c:
            c.execute(f"INSERT INTO jobs ({names}) VALUES ({marks})",
                      list(cols.values()))
        return job_id

    def update(self, job_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE jobs SET {sets} WHERE id = ?",
                      [*fields.values(), job_id])

    def delete(self, job_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    # ---- reading ------------------------------------------------------

    def get(self, job_id: str):
        with self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list(self, stage: str | None = None, limit: int = 100) -> list:
        sql = "SELECT * FROM jobs"
        args = []
        if stage:
            sql += " WHERE stage = ?"
            args.append(stage)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def counts(self) -> dict:
        with self._conn() as c:
            rows = c.execute(
                "SELECT stage, COUNT(*) n FROM jobs GROUP BY stage").fetchall()
        return {r["stage"]: r["n"] for r in rows}

    # ---- the worker handshake -----------------------------------------

    def claim_next(self, worker: str) -> dict | None:
        """Atomically take the oldest claimable job, or return None.

        The UPDATE ... WHERE stage = ? guard is what makes this safe: if two
        workers race, only one UPDATE matches a row still in the old stage.
        """
        self.release_stale()
        now = time.time()
        with self._conn() as c:
            for from_stage, to_stage in CLAIMABLE.items():
                row = c.execute(
                    "SELECT id FROM jobs WHERE stage = ? "
                    "ORDER BY created_at ASC LIMIT 1", (from_stage,)).fetchone()
                if row is None:
                    continue
                changed = c.execute(
                    "UPDATE jobs SET stage = ?, claimed_by = ?, claimed_at = ?, "
                    "updated_at = ? WHERE id = ? AND stage = ?",
                    (to_stage, worker, now, now, row["id"], from_stage)).rowcount
                if changed:
                    got = c.execute("SELECT * FROM jobs WHERE id = ?",
                                    (row["id"],)).fetchone()
                    return dict(got)
        return None

    def release_stale(self) -> int:
        """Re-queue jobs whose worker went away mid-render."""
        cutoff = time.time() - STALE_CLAIM_SECONDS
        with self._conn() as c:
            return c.execute(
                "UPDATE jobs SET stage = CASE stage "
                "  WHEN 'fetching' THEN 'queued' "
                "  WHEN 'extracting' THEN 'queued' "
                "  WHEN 'rendering' THEN 'approved' END, "
                "claimed_by = '', progress = '' "
                "WHERE stage IN ('fetching','extracting','rendering') "
                "AND claimed_at > 0 AND claimed_at < ?", (cutoff,)).rowcount

    def heartbeat(self, job_id: str, progress: str = "") -> None:
        with self._conn() as c:
            c.execute("UPDATE jobs SET claimed_at = ?, progress = ?, updated_at = ? "
                      "WHERE id = ?", (time.time(), progress, time.time(), job_id))

    # ---- housekeeping --------------------------------------------------

    def prune(self, keep_days: int, media_dir: Path) -> int:
        """Delete finished jobs and their files past the retention window.

        The VPS has ~13 GB free and a finished song is 150-250 MB, so without
        this the disk fills in a couple of months. The church PC keeps the
        masters; this copy exists only so people can download it.
        """
        cutoff = time.time() - keep_days * 86400
        removed = 0
        for job in self.list(stage="done", limit=1000):
            if job["updated_at"] >= cutoff:
                continue
            if job["output_name"]:
                (Path(media_dir) / job["output_name"]).unlink(missing_ok=True)
            self.update(job["id"], stage="expired", output_name="", output_bytes=0)
            removed += 1
        return removed
