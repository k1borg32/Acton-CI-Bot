"""
Async job queue with persistent rate limiting.

In-memory state (active_jobs, semaphore) is for live concurrency; the
hourly/daily quota counts come from a SQLite-backed UsageStore so users
can't bypass limits by waiting for a redeploy.
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from bot.config import RateLimitConfig
from bot.services.usage import UsageStore

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        super().__init__(user_message)


@dataclass
class _UserState:
    active_jobs: int = 0


class JobQueue:
    def __init__(
        self,
        config: RateLimitConfig,
        usage: UsageStore,
    ) -> None:
        self._config = config
        self._usage = usage
        self._users: dict[int, _UserState] = defaultdict(_UserState)
        self._semaphore = asyncio.Semaphore(config.max_concurrent_global)
        self._lock = asyncio.Lock()
        self._pending_count = 0
        self._active_count = 0

    def _check_rate_limit_locked(self, user_id: int) -> None:
        now = int(time.time())
        hour_ago = now - 3600
        day_ago = now - 86400

        # Per-user hourly cap
        per_hour = self._usage.count_user_since(user_id, hour_ago)
        if per_hour >= self._config.max_checks_per_hour:
            oldest = self._usage.oldest_user_ts_since(user_id, hour_ago)
            remaining = (oldest + 3600 - now) if oldest else 60
            minutes = max(1, int(remaining / 60))
            raise RateLimitExceeded(
                f"⏰ Hourly limit: {self._config.max_checks_per_hour} "
                f"checks/hour. Try again in ~{minutes} min."
            )

        # Per-user daily cap
        per_day = self._usage.count_user_since(user_id, day_ago)
        if per_day >= self._config.max_checks_per_day:
            oldest = self._usage.oldest_user_ts_since(user_id, day_ago)
            remaining = (oldest + 86400 - now) if oldest else 3600
            hours = max(1, int(remaining / 3600))
            raise RateLimitExceeded(
                f"📅 Daily limit: {self._config.max_checks_per_day} "
                f"checks/24h. Try again in ~{hours} h."
            )

        # Global daily cap — capacity guard for the VPS
        global_per_day = self._usage.count_global_since(day_ago)
        if global_per_day >= self._config.max_checks_global_per_day:
            raise RateLimitExceeded(
                "🛑 The bot has hit its global daily capacity. "
                "Please try again in a few hours — single VPS, single dev. 🙏"
            )

        # Per-user concurrent jobs
        state = self._users[user_id]
        if state.active_jobs >= self._config.max_concurrent_per_user:
            raise RateLimitExceeded(
                "⏳ You already have an active check. Wait for it to finish."
            )

    async def acquire(self, user_id: int, source: str = "manual") -> int:
        async with self._lock:
            self._check_rate_limit_locked(user_id)
            state = self._users[user_id]
            self._usage.record(user_id, source=source)
            state.active_jobs += 1
            self._pending_count += 1
            position = max(
                0,
                self._pending_count - self._config.max_concurrent_global,
            )
        await self._semaphore.acquire()
        async with self._lock:
            self._pending_count = max(0, self._pending_count - 1)
            self._active_count += 1
        return position

    def release(self, user_id: int) -> None:
        state = self._users[user_id]
        state.active_jobs = max(0, state.active_jobs - 1)
        self._active_count = max(0, self._active_count - 1)
        self._semaphore.release()

    @property
    def active_jobs(self) -> int:
        return self._active_count

    @property
    def pending_jobs(self) -> int:
        return self._pending_count
