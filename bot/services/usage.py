"""
SQLite-backed usage history for rate limiting.

Each successful queue acquisition records (user_id, ts). The queue then
counts rows in time windows (1 hour, 24 hours) to enforce per-user and
global daily caps.

Survives restarts — important for a public bot where in-memory state
would let users bypass limits by waiting for a redeploy.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    user_id INTEGER NOT NULL,
    ts      INTEGER NOT NULL,
    source  TEXT    NOT NULL DEFAULT 'manual'  -- manual | webhook
);
CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_events(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_ts      ON usage_events(ts);
"""


class UsageStore:
    """Persistent usage counter. One row per check started."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, user_id: int, source: str = "manual") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO usage_events (user_id, ts, source) VALUES (?, ?, ?)",
                (user_id, int(time.time()), source),
            )

    def count_user_since(self, user_id: int, since_ts: int) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM usage_events WHERE user_id=? AND ts>=?",
                (user_id, since_ts),
            ).fetchone()
            return int(row[0]) if row else 0

    def count_global_since(self, since_ts: int) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM usage_events WHERE ts>=?",
                (since_ts,),
            ).fetchone()
            return int(row[0]) if row else 0

    def oldest_user_ts_since(self, user_id: int, since_ts: int) -> int | None:
        """Earliest event timestamp for `user_id` in the window starting at
        `since_ts`. Lets the queue tell a rate-limited user when their oldest
        event will fall off the window."""
        with self._conn() as c:
            row = c.execute(
                "SELECT MIN(ts) FROM usage_events WHERE user_id=? AND ts>=?",
                (user_id, since_ts),
            ).fetchone()
            if row and row[0] is not None:
                return int(row[0])
            return None

    def cleanup_older_than(self, ts: int) -> int:
        """Periodic pruning. Returns rows deleted."""
        with self._conn() as c:
            cur = c.execute("DELETE FROM usage_events WHERE ts<?", (ts,))
            return int(cur.rowcount)
