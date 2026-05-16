"""
SQLite-backed subscription store.

A subscription maps a GitHub repo (e.g. `owner/name`) to a Telegram chat
that should receive automatic CI reports for that repo's webhook events.

The same repo can be subscribed by many chats; each chat can subscribe
to many repos.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Subscription:
    chat_id: int
    repo_full_name: str
    added_by_user_id: int
    added_at: int  # unix seconds


_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id          INTEGER NOT NULL,
    repo_full_name   TEXT    NOT NULL COLLATE NOCASE,
    added_by_user_id INTEGER NOT NULL,
    added_at         INTEGER NOT NULL,
    PRIMARY KEY (chat_id, repo_full_name)
);
CREATE INDEX IF NOT EXISTS idx_subs_repo ON subscriptions(repo_full_name);
"""


class SubscriptionStore:
    """Thread-safe-ish subscription store. SQLite connections are per-call
    so we don't have to manage connection lifecycle across asyncio tasks."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def add(self, chat_id: int, repo_full_name: str, user_id: int) -> bool:
        """Returns True if newly added, False if already subscribed."""
        repo = repo_full_name.lower()
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO subscriptions "
                "(chat_id, repo_full_name, added_by_user_id, added_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, repo, user_id, int(time.time())),
            )
            return cur.rowcount > 0

    def remove(self, chat_id: int, repo_full_name: str) -> bool:
        """Returns True if removed, False if it wasn't subscribed."""
        repo = repo_full_name.lower()
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM subscriptions WHERE chat_id=? AND repo_full_name=?",
                (chat_id, repo),
            )
            return cur.rowcount > 0

    def list_for_chat(self, chat_id: int) -> list[Subscription]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT chat_id, repo_full_name, added_by_user_id, added_at "
                "FROM subscriptions WHERE chat_id=? "
                "ORDER BY repo_full_name",
                (chat_id,),
            ).fetchall()
        return [Subscription(**dict(r)) for r in rows]

    def list_chats_for_repo(self, repo_full_name: str) -> list[int]:
        """All chat ids subscribed to a repo. Used by the webhook handler
        to fan-out one pipeline run to N chats."""
        repo = repo_full_name.lower()
        with self._conn() as c:
            rows = c.execute(
                "SELECT chat_id FROM subscriptions WHERE repo_full_name=?",
                (repo,),
            ).fetchall()
        return [r["chat_id"] for r in rows]

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
