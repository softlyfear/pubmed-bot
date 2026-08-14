"""Token-bucket NCBI: не больше N запросов в секунду."""

import pytest

from pubmed_bot.services.rate_limit import PerUserWindow, TokenBucket


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_first_token_does_not_wait() -> None:
    clock = _Clock()
    bucket = TokenBucket(2.0, monotonic=clock.monotonic, sleep=clock.sleep)
    await bucket.acquire()
    assert clock.sleeps == []


async def test_exceeding_rate_waits() -> None:
    clock = _Clock()
    bucket = TokenBucket(1.0, monotonic=clock.monotonic, sleep=clock.sleep)
    await bucket.acquire()
    await bucket.acquire()
    assert clock.sleeps
    assert clock.sleeps[0] == pytest.approx(1.0)


def test_per_user_window_allows_then_blocks() -> None:
    clock = _Clock()
    window = PerUserWindow(2, window_sec=60.0, monotonic=clock.monotonic)
    assert window.allow(1) is True
    assert window.allow(1) is True
    assert window.allow(1) is False
    assert window.allow(2) is True
