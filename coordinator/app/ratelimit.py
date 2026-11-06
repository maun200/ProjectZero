from collections import deque
from typing import Callable
import time
import threading


class RateLimiter:
    """Sliding-window limiter: at most `limit` events per `window` seconds and key.

    Per-process; not shared between coordinator processes. `limit=0` disables it.
    """

    def __init__(self, limit: int, clock: Callable[[], float] = time.monotonic, window: float = 60.0):
        self.per_minute = limit  # historical name, kept for callers/tests
        self.window = window
        self.clock = clock
        self.slots: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def check(self, key: str) -> float:
        """Record one event; return 0 if allowed, else seconds until the next slot frees."""
        if self.per_minute == 0:
            return 0.0
        now = self.clock()
        with self.lock:
            slot = self.slots.setdefault(key, deque())
            while slot and slot[0] <= now - self.window:
                slot.popleft()
            if len(slot) >= self.per_minute:
                return self.window - (now - slot[0])
            slot.append(now)
            if len(self.slots) > 10000:  # bound memory under many distinct keys (e.g. source IPs)
                for stale in [k for k, v in self.slots.items() if not v or v[-1] <= now - self.window]:
                    del self.slots[stale]
            return 0.0
