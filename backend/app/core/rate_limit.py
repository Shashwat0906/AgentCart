"""Small in-process sliding-window rate limiter.

Good enough for a single-instance deployment. For multiple replicas, back this with Redis
(documented in docs/SECURITY.md).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import Request

from app.core.config import get_settings
from app.core.errors import RateLimited


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: float = 60.0) -> float | None:
        """Record a hit. Returns None if allowed, otherwise seconds until retry."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= window_seconds:
                q.popleft()
            if len(q) >= limit:
                return max(0.0, window_seconds - (now - q[0]))
            q.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(bucket: str, per_minute: Callable[[], int]) -> Callable[[Request], None]:
    """FastAPI dependency factory. Keys by client IP (and user if the route sets it)."""

    def dependency(request: Request) -> None:
        if not get_settings().rate_limit_enabled:
            return
        identity = getattr(request.state, "user_id", None) or client_ip(request)
        retry = limiter.hit(f"{bucket}:{identity}", per_minute())
        if retry is not None:
            raise RateLimited(
                "Too many requests. Please slow down.",
                details={"retry_after_seconds": round(retry, 1)},
            )

    return dependency
