"""Voice lifecycle concurrency primitives, independent of the Discord API."""
from __future__ import annotations
import asyncio
import time
from contextlib import asynccontextmanager


class KeyedLocks:
    """Keeps locks per key to serialize changes for one user/channel.

    Locks are intentionally retained until process restart; even waiting tasks
    will still share the same lock (no unsafe eager deletion).
    """
    def __init__(self):
        self._locks = {}

    @asynccontextmanager
    async def hold(self, key):
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield


class Cooldowns:
    def __init__(self, seconds: float):
        self.seconds = max(0.0, float(seconds))
        self._last = {}

    def remaining(self, key) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self._last.get(key, -float('inf'))))

    def mark(self, key):
        self._last[key] = time.monotonic()


creation_locks = KeyedLocks()
channel_locks = KeyedLocks()
creation_cooldowns = Cooldowns(8)
