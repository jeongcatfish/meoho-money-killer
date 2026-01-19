import asyncio
import time


class SignalGuard:
    def __init__(self, ttl_sec: int) -> None:
        self._ttl_sec = ttl_sec
        self._signals: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def register(self, signal_id: str) -> bool:
        now = time.time()
        async with self._lock:
            self._prune(now)
            if signal_id in self._signals:
                return False
            self._signals[signal_id] = now
            return True

    def _prune(self, now: float) -> None:
        expired = [key for key, ts in self._signals.items() if now - ts > self._ttl_sec]
        for key in expired:
            self._signals.pop(key, None)
