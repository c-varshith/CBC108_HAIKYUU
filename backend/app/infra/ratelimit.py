"""In-memory sliding-window limiter. Correct for one process; for several API workers put the
same logic behind Redis (the interface is just hit/reset)."""
from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_hits: int, window_seconds: int):
        self.max, self.window = max_hits, window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> int | None:
        """Record an attempt. Returns seconds to wait if over the limit, else None."""
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            return max(1, int(self.window - (now - q[0])) + 1)
        q.append(now)
        if len(self._hits) > 50_000:  # bound memory under key-spraying
            self._gc(now)
        return None

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def _gc(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window]:
            self._hits.pop(k, None)
