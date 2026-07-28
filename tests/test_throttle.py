"""Tests for the request throttle (framework §7).

Timing is asserted with a :class:`~tests.conftest.FakeClock` whose ``sleep``
advances a virtual clock, so these run instantly and never touch wall-clock time.
"""

from __future__ import annotations

from fantasy_coach.clients.throttle import (
    DEFAULT_MIN_INTERVAL,
    DRAFT_POLL_INTERVAL,
    NullThrottle,
    Throttle,
)
from tests.conftest import FakeClock


def make_throttle(min_interval: float, clock: FakeClock) -> Throttle:
    return Throttle(min_interval, time_func=clock.time, sleep_func=clock.sleep)


def test_first_call_never_waits():
    clock = FakeClock()
    throttle = make_throttle(2.5, clock)
    assert throttle.wait() == 0.0
    assert clock.sleeps == []


def test_back_to_back_calls_wait_the_remainder():
    clock = FakeClock()
    throttle = make_throttle(2.5, clock)
    throttle.wait()  # t=1000, no sleep
    slept = throttle.wait()  # immediately again -> full interval
    assert slept == 2.5
    assert clock.sleeps == [2.5]


def test_partial_elapsed_only_waits_the_gap():
    clock = FakeClock()
    throttle = make_throttle(2.5, clock)
    throttle.wait()
    clock.advance(1.0)  # 1s passed naturally
    slept = throttle.wait()
    assert slept == 1.5  # only the remaining 1.5s


def test_naturally_slow_caller_is_never_delayed():
    clock = FakeClock()
    throttle = make_throttle(DRAFT_POLL_INTERVAL, clock)
    throttle.wait()
    clock.advance(3.0)  # a real draft loop already sleeps ~3s between polls
    assert throttle.wait() == 0.0
    assert clock.sleeps == []


def test_sustained_rate_matches_the_configured_interval():
    """Ten back-to-back calls at the draft floor span 22.5s — ~0.4 req/s (§7)."""
    clock = FakeClock()
    throttle = make_throttle(DRAFT_POLL_INTERVAL, clock)
    start = clock.time()
    for _ in range(10):
        throttle.wait()
    assert clock.time() - start == 22.5
    assert throttle.stats["wait_count"] == 9


def test_zero_interval_disables_throttling():
    clock = FakeClock()
    throttle = make_throttle(0.0, clock)
    throttle.wait()
    assert throttle.wait() == 0.0
    assert clock.sleeps == []


def test_negative_interval_is_clamped_to_zero():
    throttle = Throttle(-5.0)
    assert throttle.min_interval == 0.0


def test_reset_clears_last_call():
    clock = FakeClock()
    throttle = make_throttle(2.5, clock)
    throttle.wait()
    throttle.reset()
    # After reset the next call behaves like the first — no wait.
    assert throttle.wait() == 0.0


def test_backoff_is_exponential():
    clock = FakeClock()
    throttle = make_throttle(1.0, clock)
    assert throttle.backoff_sleep(1, base=2.0) == 2.0
    assert throttle.backoff_sleep(2, base=2.0) == 4.0
    assert throttle.backoff_sleep(3, base=2.0) == 8.0
    assert clock.sleeps == [2.0, 4.0, 8.0]


def test_backoff_is_capped():
    clock = FakeClock()
    throttle = make_throttle(1.0, clock)
    assert throttle.backoff_sleep(10, base=2.0, cap=30.0) == 30.0


def test_backoff_resets_interval_clock():
    clock = FakeClock()
    throttle = make_throttle(2.5, clock)
    throttle.wait()
    throttle.backoff_sleep(1)  # sleeps, and resets last_call to "now"
    # A wait immediately after backoff should not double-charge the interval.
    assert throttle.wait() == 2.5  # full interval again (last_call was reset to now)


def test_stats_track_waits():
    clock = FakeClock()
    throttle = make_throttle(2.5, clock)
    throttle.wait()
    throttle.wait()
    throttle.wait()
    stats = throttle.stats
    assert stats["wait_count"] == 2  # first call didn't wait
    assert stats["total_waited"] == 5.0
    assert stats["min_interval"] == 2.5


def test_null_throttle_never_sleeps():
    throttle = NullThrottle()
    assert throttle.wait() == 0.0
    assert throttle.backoff_sleep(5) == 0.0


def test_default_constants():
    assert DEFAULT_MIN_INTERVAL == 1.0
    assert DRAFT_POLL_INTERVAL == 2.5
