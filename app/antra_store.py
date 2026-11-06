"""Antra SQLite queue and public state store.

Shared by API dispatcher and background download worker.
Preserves durable job/track status, atomic transitions, and worker heartbeats.
Nothing starts on import.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, Iterator, List, Optional
import uuid

DEFAULT_DB_DIR = Path(os.path.expanduser("~/.local/share/mobile-server/antra"))
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "queue.sqlite3"
DEFAULT_OUTPUT_DIR = Path("/storage/emulated/0/Music/Antra")

VALID_JOB_STATUSES = frozenset({
    "queued",
    "resolving",
    "downloading",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "blocked",
    "uncertain",
})

VALID_TRACK_STATUSES = frozenset({
    "queued",
    "downloading",
    "completed",
    "skipped",
    "failed",
    "cancelled",
})

CANCELLABLE_JOB_STATUSES = frozenset({
    "queued",
    "resolving",
    "downloading",
    "blocked",
    "uncertain",
})

RETRIABLE_JOB_STATUSES = frozenset({
    "failed",
    "cancelled",
    "uncertain",
    "partial",
})


class AntraStoreError(Exception):
    """Base error for Antra store operations."""
    pass


class InvalidTransitionError(AntraStoreError):
    """Raised when an illegal job or track state transition is requested."""
    pass


class JobNotFoundError(AntraStoreError):
    """Raised when the specified job identifier is not present."""
    pass


def _row_to_job_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"],
        "status": row["status"],
        "total": row["total"],
        "completed": row["completed"],
        "skipped": row["skipped"],
        "failed": row["failed"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message": row["message"],
    }


def _row_to_track_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "index": row["track_index"],
        "title": row["title"],
        "artist": row["artist"],
        "status": row["status"],
        "bit_depth": row["bit_depth"],
        "sample_rate": row["sample_rate"],
        "filename": row["filename"],
        "message": row["message"],
    }


class AntraStore:
    """Manages SQLite storage for Antra jobs, tracks, and worker state."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        output_dir: Optional[Path] = None,
    ):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.output_dir = Path(output_dir) if output_dir is not None else DEFAULT_OUTPUT_DIR
        self._init_db()

    @property
    def output_dir_str(self) -> str:
        return str(self.output_dir)

    def _init_db(self) -> None:
        db_dir = self.db_path.parent
        db_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(db_dir, 0o700)
        except OSError:
            pass

        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    total INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    upstream_job_id TEXT,
                    format TEXT NOT NULL DEFAULT 'flac24'
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tracks (
                    job_id TEXT NOT NULL,
                    track_index INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    artist TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    bit_depth INTEGER,
                    sample_rate INTEGER,
                    filename TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (job_id, track_index),
                    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS worker_heartbeat (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state TEXT NOT NULL,
                    updated_at REAL,
                    message TEXT NOT NULL DEFAULT ''
                );
            """)

        if self.db_path.exists():
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level="DEFERRED",
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get_worker_status(self) -> Dict[str, Any]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT state, updated_at, message FROM worker_heartbeat WHERE id = 1"
            )
            row = cur.fetchone()
            if row:
                return {
                    "state": row["state"],
                    "updated_at": row["updated_at"],
                    "message": row["message"],
                }
        return {
            "state": "idle",
            "updated_at": None,
            "message": "Worker not started",
        }

    def set_worker_status(self, state: str, message: str = "") -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO worker_heartbeat (id, state, updated_at, message)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    state = excluded.state,
                    updated_at = excluded.updated_at,
                    message = excluded.message
                """,
                (state, now, message),
            )

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC;")
            return [_row_to_job_dict(row) for row in cur.fetchall()]

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            return _row_to_job_dict(row) if row else None

    def get_internal_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return full job record including private worker fields."""
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def get_tracks(self, job_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM tracks WHERE job_id = ? ORDER BY track_index ASC;",
                (job_id,),
            )
            return [_row_to_track_dict(row) for row in cur.fetchall()]

    def create_job(
        self,
        url: str,
        title: str = "",
        format_pref: str = "flac24",
    ) -> Dict[str, Any]:
        job_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, url, title, status, total, completed, skipped, failed,
                    created_at, updated_at, message, format
                ) VALUES (?, ?, ?, 'queued', 0, 0, 0, 0, ?, ?, '', ?);
                """,
                (job_id, url, title, now, now, format_pref),
            )
        job = self.get_job(job_id)
        if job is None:
            raise AntraStoreError("Failed to create job")
        return job

    def update_job(self, job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get_job(job_id)

        allowed_fields = {
            "title", "status", "total", "completed", "skipped", "failed",
            "message", "upstream_job_id", "updated_at", "format",
        }
        updates = {}
        for k, v in fields.items():
            if k in allowed_fields:
                if k == "status" and v not in VALID_JOB_STATUSES:
                    raise InvalidTransitionError(f"Invalid job status: {v}")
                updates[k] = v

        if not updates:
            return self.get_job(job_id)

        if "updated_at" not in updates:
            updates["updated_at"] = time.time()

        set_clauses = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(job_id)

        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE jobs SET {set_clauses} WHERE id = ?;",
                values,
            )
            if cur.rowcount == 0:
                return None

        return self.get_job(job_id)

    def set_tracks(self, job_id: str, tracks: List[Dict[str, Any]]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM tracks WHERE job_id = ?;", (job_id,))
            for trk in tracks:
                idx = int(trk["index"])
                title = str(trk.get("title", ""))
                artist = str(trk.get("artist", ""))
                status = trk.get("status", "queued")
                if status not in VALID_TRACK_STATUSES:
                    status = "queued"
                bit_depth = trk.get("bit_depth")
                sample_rate = trk.get("sample_rate")
                filename = trk.get("filename")
                msg = str(trk.get("message", ""))
                conn.execute(
                    """
                    INSERT INTO tracks (
                        job_id, track_index, title, artist, status,
                        bit_depth, sample_rate, filename, message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (job_id, idx, title, artist, status, bit_depth, sample_rate, filename, msg),
                )
            conn.execute(
                "UPDATE jobs SET total = ?, updated_at = ? WHERE id = ?;",
                (len(tracks), time.time(), job_id),
            )

    def update_track(
        self,
        job_id: str,
        track_index: int,
        **fields: Any,
    ) -> None:
        allowed = {"title", "artist", "status", "bit_depth", "sample_rate", "filename", "message"}
        updates = {}
        for k, v in fields.items():
            if k in allowed:
                if k == "status" and v not in VALID_TRACK_STATUSES:
                    raise InvalidTransitionError(f"Invalid track status: {v}")
                updates[k] = v

        if not updates:
            return

        set_clauses = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.extend([job_id, track_index])

        with self._connect() as conn:
            conn.execute(
                f"UPDATE tracks SET {set_clauses} WHERE job_id = ? AND track_index = ?;",
                values,
            )
            # Recompute counts for job
            conn.execute(
                """
                UPDATE jobs SET
                    completed = (SELECT COUNT(*) FROM tracks WHERE job_id = ? AND status = 'completed'),
                    skipped = (SELECT COUNT(*) FROM tracks WHERE job_id = ? AND status = 'skipped'),
                    failed = (SELECT COUNT(*) FROM tracks WHERE job_id = ? AND status = 'failed'),
                    updated_at = ?
                WHERE id = ?;
                """,
                (job_id, job_id, job_id, time.time(), job_id),
            )

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            cur = conn.execute("SELECT status FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            if not row:
                raise JobNotFoundError(f"Job not found: {job_id}")

            current_status = row["status"]
            if current_status not in CANCELLABLE_JOB_STATUSES:
                raise InvalidTransitionError(
                    f"Cannot cancel job in status '{current_status}'"
                )

            now = time.time()
            conn.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ?;",
                (now, job_id),
            )
            # Cancel in-progress and queued tracks, preserve completed
            conn.execute(
                """
                UPDATE tracks SET status = 'cancelled', message = 'Job cancelled'
                WHERE job_id = ? AND status IN ('queued', 'downloading');
                """,
                (job_id,),
            )

        job = self.get_job(job_id)
        if job is None:
            raise JobNotFoundError(f"Job not found: {job_id}")
        return job

    def retry_job(self, job_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            cur = conn.execute("SELECT status FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            if not row:
                raise JobNotFoundError(f"Job not found: {job_id}")

            current_status = row["status"]
            if current_status not in RETRIABLE_JOB_STATUSES:
                raise InvalidTransitionError(
                    f"Cannot retry job in status '{current_status}'"
                )

            now = time.time()
            conn.execute(
                "UPDATE jobs SET status = 'queued', message = '', updated_at = ? WHERE id = ?;",
                (now, job_id),
            )
            # Reset non-completed tracks to queued, preserve completed ones
            conn.execute(
                """
                UPDATE tracks SET status = 'queued', message = ''
                WHERE job_id = ? AND status != 'completed';
                """,
                (job_id,),
            )

        job = self.get_job(job_id)
        if job is None:
            raise JobNotFoundError(f"Job not found: {job_id}")
        return job

    def get_next_queued_job(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1;"
            )
            row = cur.fetchone()
            return dict(row) if row else None
