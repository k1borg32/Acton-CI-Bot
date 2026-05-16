"""
Async job queue with per-user and global rate limiting.
Uses asyncio primitives — no Redis needed for MVP.
"""

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from time import monotonic

from bot.config import RateLimitConfig

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    def __init__(self, user_message: str) -> None:
        self.user_message = user_message
        super().__init__(user_message)


@dataclass
class _UserState:
    timestamps: list[float] = field(default_factory=list)
    active_jobs: int = 0


class JobQueue:
    def __init__(self, config: RateLimitConfig) -> None:
        self._config = config
        self._users: dict[int, _UserState] = defaultdict(_UserState)
        self._semaphore = asyncio.Semaphore(config.max_concurrent_global)
        self._lock = asyncio.Lock()
        self._pending_count = 0
        self._active_count = 0

    def _cleanup_timestamps(self, state: _UserState) -> None:
        cutoff = monotonic() - 3600
        state.timestamps = [t for t in state.timestamps if t > cutoff]

    def _check_rate_limit_locked(self, user_id: int) -> None:
        state = self._users[user_id]
        self._cleanup_timestamps(state)

        if len(state.timestamps) >= self._config.max_checks_per_hour:
            remaining = state.timestamps[0] + 3600 - monotonic()
            minutes = max(1, int(remaining / 60))
            raise RateLimitExceeded(
                f"⏰ Rate limit: {self._config.max_checks_per_hour} "
                f"checks/hour. Try again in ~{minutes} min."
            )

        if state.active_jobs >= self._config.max_concurrent_per_user:
            raise RateLimitExceeded(
                "⏳ You already have an active check. Wait for it to finish."
            )

    async def acquire(self, user_id: int) -> int:
        async with self._lock:
            self._check_rate_limit_locked(user_id)
            state = self._users[user_id]
            state.timestamps.append(monotonic())
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
