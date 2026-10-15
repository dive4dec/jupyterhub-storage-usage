"""SQLite cache for per-user storage usage data.

Schema:
  usage(username TEXT PRIMARY KEY, used_bytes INTEGER, status TEXT,
        timestamp REAL, server_url TEXT, first_over_limit REAL,
        last_culled REAL, warn_bytes INTEGER, hard_bytes INTEGER)

The `first_over_limit` column tracks when a user first crossed the hard limit,
enabling the grace period logic.

The `warn_bytes`/`hard_bytes` columns store the resolved per-user/group limits
so the dashboard and report handler can display correct limits without needing
group membership info (which is only available in the pre-spawn hook and poller).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import CACHE_DB_PATH

logger = logging.getLogger("jupyterhub_storage_usage")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    username TEXT PRIMARY KEY,
    used_bytes INTEGER DEFAULT 0,
    status TEXT DEFAULT 'unknown',
    timestamp REAL DEFAULT 0,
    server_url TEXT DEFAULT '',
    first_over_limit REAL DEFAULT 0,
    last_culled REAL DEFAULT 0,
    warn_bytes INTEGER DEFAULT 0,
    hard_bytes INTEGER DEFAULT 0
);
"""


class UsageCache:
    """Thread-safe SQLite cache for storage usage data."""

    def __init__(self, db_path: str = CACHE_DB_PATH):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            # Migration: add last_culled column if missing (old DBs)
            try:
                conn.execute("SELECT last_culled FROM usage LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE usage ADD COLUMN last_culled REAL DEFAULT 0")
            # Migration: add warn_bytes/hard_bytes columns if missing
            try:
                conn.execute("SELECT warn_bytes FROM usage LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE usage ADD COLUMN warn_bytes INTEGER DEFAULT 0")
            try:
                conn.execute("SELECT hard_bytes FROM usage LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE usage ADD COLUMN hard_bytes INTEGER DEFAULT 0")
            conn.commit()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def update(self, username: str, used_bytes: int, status: str,
               server_url: str = "", warn_bytes: int = 0, hard_bytes: int = 0):
        """Upsert usage data for a user.

        Parameters
        ----------
        warn_bytes, hard_bytes : optional resolved per-user limits.
            Stored so the dashboard and report handler can display the
            correct limits without needing group membership info.
        """
        now = time.time()
        with self._lock:
            with self._conn() as conn:
                # Check if user is already over limit (for grace period tracking)
                row = conn.execute(
                    "SELECT first_over_limit FROM usage WHERE username=?",
                    (username,),
                ).fetchone()

                if status == "over_limit":
                    first_over = row[0] if row and row[0] else now
                else:
                    first_over = 0.0  # Reset when back under limit

                conn.execute(
                    """INSERT INTO usage (username, used_bytes, status, timestamp, server_url, first_over_limit, warn_bytes, hard_bytes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(username) DO UPDATE SET
                         used_bytes=excluded.used_bytes,
                         status=excluded.status,
                         timestamp=excluded.timestamp,
                         server_url=excluded.server_url,
                         first_over_limit=CASE
                           WHEN excluded.status = 'over_limit' THEN COALESCE(NULLIF(usage.first_over_limit, 0), excluded.first_over_limit)
                           WHEN excluded.status != 'over_limit' AND usage.first_over_limit = 0 THEN 0
                           ELSE usage.first_over_limit END,
                         warn_bytes=excluded.warn_bytes,
                         hard_bytes=excluded.hard_bytes""",
                    (username, used_bytes, status, now, server_url, first_over, warn_bytes, hard_bytes),
                )

    def get(self, username: str) -> Optional[dict]:
        """Get cached usage for a user."""
        with self._lock:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT username, used_bytes, status, timestamp, server_url, first_over_limit, warn_bytes, hard_bytes FROM usage WHERE username=?",
                    (username,),
                ).fetchone()
        if not row:
            return None
        return {
            "username": row[0],
            "used_bytes": row[1],
            "status": row[2],
            "timestamp": row[3],
            "server_url": row[4],
            "first_over_limit": row[5],
            "warn_bytes": row[6],
            "hard_bytes": row[7],
        }

    def get_all(self) -> list[dict]:
        """Get all cached usage data, sorted by used_bytes descending."""
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT username, used_bytes, status, timestamp, server_url, first_over_limit, warn_bytes, hard_bytes FROM usage ORDER BY used_bytes DESC"
                ).fetchall()
        return [
            {
                "username": r[0],
                "used_bytes": r[1],
                "status": r[2],
                "timestamp": r[3],
                "server_url": r[4],
                "first_over_limit": r[5],
                "warn_bytes": r[6],
                "hard_bytes": r[7],
            }
            for r in rows
        ]

    def get_over_limit(self) -> list[dict]:
        """Get users currently over the hard limit (for culling)."""
        with self._lock:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT username, used_bytes, status, timestamp, server_url, first_over_limit, last_culled FROM usage WHERE status='over_limit'"
                ).fetchall()
        return [
            {
                "username": r[0],
                "used_bytes": r[1],
                "status": r[2],
                "timestamp": r[3],
                "server_url": r[4],
                "first_over_limit": r[5],
                "last_culled": r[6],
            }
            for r in rows
        ]

    def set_culled(self, username: str, timestamp: float):
        """Record when a user's server was culled (for cooldown)."""
        with self._lock:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE usage SET last_culled=? WHERE username=?",
                    (timestamp, username),
                )

    def remove(self, username: str):
        """Remove a user from the cache (e.g., after PVC deletion)."""
        with self._lock:
            with self._conn() as conn:
                conn.execute("DELETE FROM usage WHERE username=?", (username,))

    def cleanup_stale(self, max_age_sec: int = 86400):
        """Remove entries not updated in max_age_sec (e.g., 24h)."""
        cutoff = time.time() - max_age_sec
        with self._lock:
            with self._conn() as conn:
                conn.execute("DELETE FROM usage WHERE timestamp < ?", (cutoff,))
