"""Лимиты: глобальный NCBI token-bucket и per-user окна."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable


class TokenBucket:
    """Один экземпляр на процесс — общий лимит NCBI."""

    def __init__(
        self,
        rate_per_sec: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_sec <= 0:
            msg = "rate_per_sec должен быть > 0"
            raise ValueError(msg)
        self._rate = float(rate_per_sec)
        self._tokens = float(rate_per_sec)
        self._updated = monotonic()
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Дождаться одного токена."""
        async with self._lock:
            while True:
                now = self._monotonic()
                elapsed = now - self._updated
                self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
                await self._sleep(wait)


class PerUserWindow:
    """Не больше `limit` событий за `window_sec` на пользователя."""

    def __init__(
        self,
        limit: int,
        window_sec: float = 60.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            msg = "limit должен быть >= 1"
            raise ValueError(msg)
        self._limit = limit
        self._window_sec = window_sec
        self._monotonic = monotonic
        self._hits: dict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        """True, если событие в пределах лимита; факт учитывается сразу."""
        now = self._monotonic()
        queue = self._hits[user_id]
        cutoff = now - self._window_sec
        while queue and queue[0] <= cutoff:
            queue.popleft()
        if len(queue) >= self._limit:
            return False
        queue.append(now)
        return True
