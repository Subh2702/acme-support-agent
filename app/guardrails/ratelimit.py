"""In-process sliding-window rate limiter (per IP and per session).

Single-instance deployments only; with several replicas this would move to
Redis or Postgres (documented in DECISIONS.md).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(
        self, limit: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Record a hit for `key`. Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(1, int(self.window_s - (now - hits[0])) + 1)
            hits.append(now)
            if len(self._hits) > 50_000:  # bound memory under abuse
                self._evict(now)
            return True, 0

    def _evict(self, now: float) -> None:
        stale = [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window_s]
        for key in stale:
            del self._hits[key]
