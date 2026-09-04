from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self):
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, bucket: str, key: str, *, limit: int, window_seconds: int) -> int:
        now = time.monotonic()
        cutoff = now - window_seconds
        identity = (bucket, key[:256])
        with self._lock:
            events = self._events[identity]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return max(1, int(window_seconds - (now - events[0])))
            events.append(now)
            if len(self._events) > 10_000:
                self._prune_locked(cutoff)
        return 0

    def _prune_locked(self, cutoff: float) -> None:
        for identity in list(self._events):
            events = self._events[identity]
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                self._events.pop(identity, None)
