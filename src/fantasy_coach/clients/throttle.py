"""Conservative request throttle for the Yahoo client (framework §7).

Yahoo publishes no hard rate limit; the operational rule (framework §2.3/§7) is
to *design as if we have ~1 req/sec sustained* and, crucially, to keep
**live-draft polling at ~1 request every 2–3 seconds** so we never get throttled
mid-draft (which would be catastrophic — §7).

:class:`Throttle` enforces a **minimum interval** between successive requests: a
caller that fires back-to-back is made to wait out the remainder of the window.
For draft polling, construct one with ``min_interval=2.5`` and share it across
the hot loop; everything else can share a warmer default.

Both the clock and the sleep function are **injectable**, so tests exercise the
timing logic with a fake clock and never actually sleep (the M1 offline-testing
requirement, carried into M2). :class:`Throttle` also provides
:meth:`backoff_sleep` for the exponential backoff the client applies on Yahoo
``429``/``999`` throttle responses.
"""

from __future__ import annotations

import time
from collections.abc import Callable

# ~1 req/sec sustained budget for warm/bulk reads (settings, players, teams).
DEFAULT_MIN_INTERVAL = 1.0

# Live-draft polling floor: 1 request every 2.5s keeps us at ~0.4 req/s (§7).
DRAFT_POLL_INTERVAL = 2.5


class Throttle:
    """Enforce a minimum spacing between successive calls.

    Args:
        min_interval: Minimum seconds between the *start* of one permitted call
            and the next. ``0`` disables throttling (useful in tests).
        time_func: Monotonic clock returning seconds. Injectable for tests.
        sleep_func: Blocking sleep taking seconds. Injectable for tests.

    The instance is intended to be shared by everything that should share a rate
    budget. It is **not** thread-safe on its own; the Yahoo client serializes
    calls through it under the same lock it uses for a request.
    """

    def __init__(
        self,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        *,
        time_func: Callable[[], float] = time.monotonic,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._time = time_func
        self._sleep = sleep_func
        self._last_call: float | None = None
        self._wait_count = 0
        self._total_waited = 0.0

    def wait(self) -> float:
        """Block until it is legal to make the next request; return seconds slept.

        The first call never waits. Subsequent calls sleep only for the portion
        of ``min_interval`` that has not already elapsed since the previous call,
        so a naturally-slow caller (e.g. a 3s draft loop) is never delayed.
        """
        now = self._time()
        slept = 0.0
        if self._last_call is not None and self.min_interval > 0:
            elapsed = now - self._last_call
            remaining = self.min_interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
                slept = remaining
                self._wait_count += 1
                self._total_waited += remaining
        # Record when this call is actually permitted to proceed.
        self._last_call = self._time()
        return slept

    def backoff_sleep(self, attempt: int, *, base: float = 2.0, cap: float = 60.0) -> float:
        """Sleep with exponential backoff for a throttled retry; return seconds.

        Attempt 1 sleeps ``base`` seconds, attempt 2 ``2*base``, then ``4*base``,
        capped at ``cap`` (framework §7: ``2s→4s→8s…``). Also resets the
        min-interval clock so the retry itself isn't double-charged a wait.

        Args:
            attempt: 1-based retry number.
            base: Seconds for the first backoff.
            cap: Maximum backoff sleep.
        """
        delay = min(cap, base * (2 ** max(0, attempt - 1)))
        self._sleep(delay)
        self._last_call = self._time()
        return delay

    def reset(self) -> None:
        """Forget the last-call time (the next :meth:`wait` won't block)."""
        self._last_call = None

    @property
    def stats(self) -> dict[str, float | int]:
        """Diagnostics: how many times we waited and for how long in total."""
        return {
            "min_interval": self.min_interval,
            "wait_count": self._wait_count,
            "total_waited": round(self._total_waited, 4),
        }


class NullThrottle(Throttle):
    """A no-op throttle (never sleeps). Handy for tests that don't assert timing."""

    def __init__(self) -> None:
        super().__init__(min_interval=0.0, sleep_func=lambda _s: None)

    def wait(self) -> float:  # noqa: D102 - inherited docstring
        return 0.0

    def backoff_sleep(self, attempt: int, *, base: float = 2.0, cap: float = 60.0) -> float:  # noqa: D102
        return 0.0


__all__ = [
    "Throttle",
    "NullThrottle",
    "DEFAULT_MIN_INTERVAL",
    "DRAFT_POLL_INTERVAL",
]
