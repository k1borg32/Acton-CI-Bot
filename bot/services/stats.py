"""
In-memory operational counters surfaced via /admin stats.

Lives for the bot's process lifetime — restarts reset to zero. Good enough
for "is the bot doing anything?" visibility; persistence would mean another
table and adds little.
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock


class Stats:
    def __init__(self, max_recent_errors: int = 5) -> None:
        self._started_at = time.time()
        self._lock = Lock()
        self.checks_total = 0
        self.checks_manual = 0
        self.checks_webhook = 0
        self.checks_success = 0
        self.checks_failed = 0
        self._recent_errors: deque[tuple[float, str]] = deque(maxlen=max_recent_errors)

    @property
    def uptime_s(self) -> float:
        return time.time() - self._started_at

    def record_check(self, source: str, success: bool) -> None:
        with self._lock:
            self.checks_total += 1
            if source == "webhook":
                self.checks_webhook += 1
            else:
                self.checks_manual += 1
            if success:
                self.checks_success += 1
            else:
                self.checks_failed += 1

    def record_error(self, where: str, exc: BaseException) -> None:
        with self._lock:
            self._recent_errors.append(
                (time.time(), f"{where}: {type(exc).__name__}: {str(exc)[:120]}")
            )

    def recent_errors(self) -> list[tuple[float, str]]:
        with self._lock:
            return list(self._recent_errors)
