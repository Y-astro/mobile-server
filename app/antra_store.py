"""Antra SQLite queue and public state store.

Shared by API dispatcher and background download worker.
Preserves durable job/track status, atomic transitions, and worker heartbeats.
Nothing starts on import.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, Iterator, List, Optional
import urllib.parse
import uuid

DEFAULT_DB_DIR = Path(os.path.expanduser("~/.local/share/mobile-server/antra"))
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "queue.sqlite3"

# Logical output labels exposed to API (no physical paths)
LOGICAL_OUTPUT_DIRECTORIES = {
    "private": "Music/Astro",
    "family": "Music/Family",
}
DEFAULT_OUTPUT_ROOTS = {
    "private": Path("/storage/emulated/0/Music/Astro"),
    "family": Path("/storage/emulated/0/Music/Family"),
}

VALID_DESTINATIONS = frozenset({"private", "family"})

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

MAX_AUTOMATIC_RETRY_ROUNDS = 3
RETRIABLE_JOB_STATUSES = frozenset({
    "failed",
    "cancelled",
    "blocked",
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
        "destination": row["destination"] if "destination" in row.keys() else "private",
        "status": row["status"],
        "total": row["total"],
        "completed": row["completed"],
        "skipped": row["skipped"],
        "failed": row["failed"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message": row["message"],
        "automatic_retry_rounds": row["automatic_retry_rounds"] if "automatic_retry_rounds" in row.keys() else 0,
        "removed": row["removed"] if "removed" in row.keys() else 0,
        "duplicate_confirmed": row["duplicate_confirmed"] if "duplicate_confirmed" in row.keys() else 0,
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
        output_roots: Optional[Dict[str, Path]] = None,
    ):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.output_roots = (
            {k: Path(v) for k, v in output_roots.items()}
            if output_roots is not None
            else dict(DEFAULT_OUTPUT_ROOTS)
        )
        self._init_db()

    def get_output_root(self, destination: str = "private") -> Path:
        return self.output_roots.get(destination, self.output_roots["private"])

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
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    destination TEXT NOT NULL DEFAULT 'private',
                    status TEXT NOT NULL DEFAULT 'queued',
                    total INTEGER NOT NULL DEFAULT 0,
                    completed INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    upstream_job_id TEXT,
                    format TEXT NOT NULL DEFAULT 'flac24',
                    chunk_start INTEGER NOT NULL DEFAULT 0,
                    chunk_end INTEGER NOT NULL DEFAULT 0,
                    submission_intent INTEGER NOT NULL DEFAULT 0,
                    concurrency_retry_after REAL NOT NULL DEFAULT 0,
                    retry_attempts INTEGER NOT NULL DEFAULT 0,
                    automatic_retry_rounds INTEGER NOT NULL DEFAULT 0,
                    removed INTEGER NOT NULL DEFAULT 0,
                    duplicate_confirmed INTEGER NOT NULL DEFAULT 0
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
                    ack_pending INTEGER NOT NULL DEFAULT 0,
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

            # Safe schema upgrades for existing DBs
            cursor = conn.execute("PRAGMA table_info(jobs);")
            columns = {row["name"] for row in cursor.fetchall()}
            if "destination" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN destination TEXT NOT NULL DEFAULT 'private';")
            if "chunk_start" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN chunk_start INTEGER NOT NULL DEFAULT 0;")
            if "chunk_end" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN chunk_end INTEGER NOT NULL DEFAULT 0;")
            if "submission_intent" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN submission_intent INTEGER NOT NULL DEFAULT 0;")
            if "automatic_retry_rounds" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN automatic_retry_rounds INTEGER NOT NULL DEFAULT 0;")
            if "concurrency_retry_after" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN concurrency_retry_after REAL NOT NULL DEFAULT 0;")
            if "retry_attempts" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN retry_attempts INTEGER NOT NULL DEFAULT 0;")
            if "removed" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN removed INTEGER NOT NULL DEFAULT 0;")
            if "duplicate_confirmed" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN duplicate_confirmed INTEGER NOT NULL DEFAULT 0;")

            cursor_trk = conn.execute("PRAGMA table_info(tracks);")
            trk_columns = {row["name"] for row in cursor_trk.fetchall()}
            if "ack_pending" not in trk_columns:
                conn.execute("ALTER TABLE tracks ADD COLUMN ack_pending INTEGER NOT NULL DEFAULT 0;")
            for column, sql_type in (("publication_path", "TEXT"), ("temporary_path", "TEXT"),
                                     ("file_sha256", "TEXT"), ("file_size", "INTEGER"),
                                     ("temporary_dev", "INTEGER"), ("temporary_ino", "INTEGER"),
                                     ("ack_retry_after", "REAL NOT NULL DEFAULT 0")):
                if column not in trk_columns:
                    conn.execute(f"ALTER TABLE tracks ADD COLUMN {column} {sql_type};")

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

    def list_jobs(self, include_removed: bool = False) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if include_removed:
                cur = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC;")
            else:
                cur = conn.execute("SELECT * FROM jobs WHERE removed = 0 ORDER BY created_at DESC;")
            return [_row_to_job_dict(row) for row in cur.fetchall()]

    def get_job(self, job_id: str, include_removed: bool = False) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            if include_removed:
                cur = conn.execute("SELECT * FROM jobs WHERE id = ?;", (job_id,))
            else:
                cur = conn.execute("SELECT * FROM jobs WHERE id = ? AND removed = 0;", (job_id,))
            row = cur.fetchone()
            return _row_to_job_dict(row) if row else None

    def get_internal_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return full job record including private worker fields."""
        with self._connect() as conn:
            cur = conn.execute("SELECT * FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def is_job_cancelled(self, job_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("SELECT status FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            return bool(row and row["status"] == "cancelled")

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
        destination: str = "private",
        format_pref: str = "flac24",
    ) -> Dict[str, Any]:
        if destination not in VALID_DESTINATIONS:
            destination = "private"
        job_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, url, title, destination, status, total, completed, skipped, failed,
                    created_at, updated_at, message, format, chunk_start, chunk_end,
                    submission_intent, concurrency_retry_after, retry_attempts
                ) VALUES (?, ?, ?, ?, 'queued', 0, 0, 0, 0, ?, ?, '', ?, 0, 0, 0, 0, 0);
                """,
                (job_id, url, title, destination, now, now, format_pref),
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
            "chunk_start", "chunk_end", "submission_intent",
            "concurrency_retry_after", "retry_attempts",
            "duplicate_confirmed", "removed",
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
                        bit_depth, sample_rate, filename, message, ack_pending
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0);
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
        allowed = {
            "title", "artist", "status", "bit_depth", "sample_rate",
            "filename", "message", "ack_pending", "publication_path", "temporary_path", "file_sha256", "file_size", "ack_retry_after", "temporary_dev", "temporary_ino"
        }
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
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("SELECT status, removed FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            if not row or row["removed"]:
                raise JobNotFoundError(f"Job not found: {job_id}")

            current_status = row["status"]
            if current_status not in CANCELLABLE_JOB_STATUSES:
                raise InvalidTransitionError(
                    f"Cannot cancel job in status '{current_status}'"
                )

            now = time.time()
            conn.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND removed = 0;",
                (now, job_id),
            )
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

    def remove_job(self, job_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("SELECT status, removed FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            if not row:
                raise JobNotFoundError(f"Job not found: {job_id}")
            if row["removed"]:
                raise InvalidTransitionError("Job already removed")

            current_status = row["status"]
            if current_status not in ("completed", "partial", "failed", "cancelled"):
                raise InvalidTransitionError("Active jobs must be cancelled before removal")

            now = time.time()
            conn.execute(
                "UPDATE jobs SET removed = 1, updated_at = ? WHERE id = ?;",
                (now, job_id),
            )
        return {"id": job_id, "status": "removed"}

    def finish_job(self, job_id: str, status: str, message: str = "") -> None:
        if status not in ("completed", "partial", "failed"):
            raise ValueError("Invalid terminal job status")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("SELECT status, removed FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            if not row or row["removed"] or row["status"] not in ('queued', 'resolving', 'downloading', 'blocked'):
                return
            conn.execute(
                """
                UPDATE tracks SET
                    status = 'failed',
                    message = CASE WHEN message IS NOT NULL AND message != '' THEN message ELSE 'Provider ended before track was downloaded' END
                WHERE job_id = ? AND status IN ('queued', 'downloading');
                """,
                (job_id,),
            )
            conn.execute(
                """
                UPDATE jobs SET
                    status = ?,
                    message = ?,
                    completed = (SELECT COUNT(*) FROM tracks WHERE job_id = ? AND status = 'completed'),
                    skipped = (SELECT COUNT(*) FROM tracks WHERE job_id = ? AND status = 'skipped'),
                    failed = (SELECT COUNT(*) FROM tracks WHERE job_id = ? AND status = 'failed'),
                    updated_at = ?
                WHERE id = ? AND removed = 0 AND status IN ('queued', 'resolving', 'downloading', 'blocked');
                """,
                (status, message, job_id, job_id, job_id, time.time(), job_id),
            )

    def schedule_automatic_retry(
        self, job_id: str, delay: float, round_number: int, message: str = ""
    ) -> bool:
        if type(round_number) is not int or not 1 <= round_number <= MAX_AUTOMATIC_RETRY_ROUNDS:
            raise ValueError("Invalid automatic retry round")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            row = conn.execute(
                "SELECT status, automatic_retry_rounds, submission_intent, upstream_job_id FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if (not row or row["status"] != "downloading" or row["submission_intent"]
                    or not row["upstream_job_id"] or row["automatic_retry_rounds"] + 1 != round_number):
                return False
            tracks = conn.execute("SELECT status FROM tracks WHERE job_id = ?", (job_id,)).fetchall()
            if not any(t["status"] == "failed" for t in tracks) or any(t["status"] not in ("completed", "skipped", "failed") for t in tracks):
                return False
            now = time.time()
            msg = message or f"Automatic retry pass {round_number} of {MAX_AUTOMATIC_RETRY_ROUNDS} scheduled."
            conn.execute(
                """
                UPDATE jobs SET status = 'queued', message = ?, updated_at = ?,
                    chunk_start = 0, chunk_end = 0, submission_intent = 0,
                    concurrency_retry_after = ?, retry_attempts = 0,
                    automatic_retry_rounds = ?, upstream_job_id = NULL
                WHERE id = ?;
                """,
                (msg, now, now + max(0.0, float(delay)), round_number, job_id),
            )
            return True

    def retry_job(self, job_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("SELECT status, destination, removed FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            if not row or row["removed"]:
                raise JobNotFoundError(f"Job not found: {job_id}")

            current_status = row["status"]
            if current_status not in RETRIABLE_JOB_STATUSES:
                raise InvalidTransitionError(
                    f"Cannot retry job in status '{current_status}'"
                )

            now = time.time()
            # Preserves destination, resets chunking and intent
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'queued',
                    message = '',
                    updated_at = ?,
                    chunk_start = 0,
                    chunk_end = 0,
                    submission_intent = 0,
                    concurrency_retry_after = 0,
                    retry_attempts = 0,
                    automatic_retry_rounds = 0,
                    upstream_job_id = NULL
                WHERE id = ? AND removed = 0;
                """,
                (now, job_id),
            )
            conn.execute(
                """
                UPDATE tracks SET status = 'queued', message = ''
                WHERE job_id = ? AND status NOT IN ('completed', 'skipped');
                """,
                (job_id,),
            )

        job = self.get_job(job_id)
        if job is None:
            raise JobNotFoundError(f"Job not found: {job_id}")
        return job

    def continue_job(self, job_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.execute("SELECT status, removed FROM jobs WHERE id = ?;", (job_id,))
            row = cur.fetchone()
            if not row or row["removed"]:
                raise JobNotFoundError(f"Job not found: {job_id}")

            current_status = row["status"]
            if current_status not in ("blocked", "queued"):
                raise InvalidTransitionError(
                    f"Cannot continue job in status '{current_status}'"
                )

            now = time.time()
            conn.execute(
                """
                UPDATE jobs SET
                    status = 'queued',
                    duplicate_confirmed = 1,
                    concurrency_retry_after = 0,
                    updated_at = ?
                WHERE id = ? AND removed = 0;
                """,
                (now, job_id),
            )

        job = self.get_job(job_id)
        if job is None:
            raise JobNotFoundError(f"Job not found: {job_id}")
        return job

    def get_next_runnable_job(self) -> Optional[Dict[str, Any]]:
        """Find next runnable job (queued, or interrupted resolving/downloading) whose backoff has passed."""
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT * FROM jobs
                WHERE (status IN ('queued', 'resolving', 'downloading')
                       OR (status = 'blocked' AND concurrency_retry_after > 0))
                  AND concurrency_retry_after <= ?
                  AND removed = 0
                ORDER BY created_at ASC
                LIMIT 1;
                """,
                (now,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


    def get_internal_track(self, job_id: str, index: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tracks WHERE job_id = ? AND track_index = ?", (job_id, index)).fetchone()
            return dict(row) if row else None

    def pending_acknowledgements(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT tracks.*, jobs.upstream_job_id FROM tracks JOIN jobs ON jobs.id = tracks.job_id "
                "WHERE ack_pending = 1 AND ack_retry_after <= ? AND upstream_job_id IS NOT NULL "
                "AND tracks.status IN ('completed', 'skipped') "
                "AND jobs.status NOT IN ('cancelled', 'failed') AND jobs.removed = 0 "
                "LIMIT 8", (time.time(),)
            ).fetchall()]

    def find_duplicate_job(self, url: str, destination: str) -> Optional[Dict[str, Any]]:
        """Detect existing job in destination matching normalized URL, even if removed or differing in tracking query."""
        canon = canonical_music_url(url)
        if not canon:
            return None
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM jobs WHERE destination = ? ORDER BY created_at DESC;",
                (destination,),
            )
            for row in cur.fetchall():
                if canonical_music_url(row["url"]) == canon:
                    return _row_to_job_dict(row)
        return None

    def check_track_duplicates(
        self,
        job_id: str,
        destination: str,
        tracks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Annotate tracks with duplicate warnings if present in this destination or within playlist.

        Compares within selected destination only. Preserves evidence after Remove.
        Avoids false positives for different artists or versions.
        Distinguishes saved library history vs queued matches, and within-playlist duplicates.
        Labels possible duplicates when reliable artist identity is absent.
        """
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT tracks.title, tracks.artist, tracks.status, tracks.message, jobs.id AS job_id
                FROM tracks
                JOIN jobs ON jobs.id = tracks.job_id
                WHERE jobs.destination = ? AND jobs.id != ?
                  AND tracks.status IN ('completed', 'downloading', 'queued');
                """,
                (destination, job_id),
            )
            existing = cur.fetchall()

        def version_tag(title: str) -> str:
            match = re.search(r"\b(live|remix|acoustic|remastered|deluxe|instrumental|edit|extended)\b", title, re.IGNORECASE)
            return match.group(1).lower() if match else ""

        def norm(s: Optional[str]) -> str:
            if not s:
                return ""
            return re.sub(r"[^\w\s]", "", str(s)).strip().lower()

        saved_exact = set()
        saved_titles = set()
        queued_exact = set()
        queued_titles = set()

        for row in existing:
            t_norm = norm(row["title"])
            a_norm = norm(row["artist"])
            v_tag = version_tag(row["title"])
            is_saved = row["status"] == "completed"
            exact_set = saved_exact if is_saved else queued_exact
            title_set = saved_titles if is_saved else queued_titles

            if t_norm:
                title_set.add((t_norm, v_tag))
                if a_norm and a_norm not in ("unknown", "various artists", "various"):
                    exact_set.add((t_norm, a_norm, v_tag))

        seen_in_playlist_exact = set()
        seen_in_playlist_titles = set()

        annotated = []
        for trk in tracks:
            trk_copy = dict(trk)
            t_norm = norm(trk_copy.get("title", ""))
            a_norm = norm(trk_copy.get("artist", ""))
            v_tag = version_tag(trk_copy.get("title", ""))

            if not t_norm:
                annotated.append(trk_copy)
                continue

            has_reliable_artist = bool(a_norm and a_norm not in ("unknown", "various artists", "various"))
            warning = None

            if has_reliable_artist:
                key = (t_norm, a_norm, v_tag)
                if key in seen_in_playlist_exact:
                    warning = "Possible duplicate: duplicate song within this playlist."
                elif key in saved_exact:
                    warning = "Warning: duplicate song already in library."
                elif key in queued_exact:
                    warning = "Possible duplicate: song already in queued/downloading job."
                seen_in_playlist_exact.add(key)
            else:
                key = (t_norm, v_tag)
                if key in seen_in_playlist_titles:
                    warning = "Possible duplicate: duplicate song title within this playlist."
                elif key in saved_titles:
                    warning = "Possible duplicate: song title matches saved library record."
                elif key in queued_titles:
                    warning = "Possible duplicate: song title matches queued/downloading job."
                seen_in_playlist_titles.add(key)

            if warning:
                msg = trk_copy.get("message", "")
                trk_copy["message"] = f"{warning} {msg}".strip() if msg else warning

            annotated.append(trk_copy)

        return annotated


def canonical_music_url(url: str) -> str:
    """Canonicalize music URL for duplicate detection, stripping tracking query parameters and ordering remaining."""
    if not isinstance(url, str):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        filtered = []
        if parsed.query:
            for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                k_lower = k.lower()
                if (k_lower in ("pi", "si", "sp_cid", "feature", "context")
                        or k_lower.startswith("utm_")):
                    continue
                filtered.append((k, v))
        filtered.sort(key=lambda item: (item[0], item[1]))
        new_query = urllib.parse.urlencode(filtered)
        return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), new_query, ""))
    except Exception:
        return url.strip().lower()
