"""Small in-process sliding-window rate limiter.

Good enough for a single-instance deployment. For multiple replicas, back this with Redis
(documented in docs/SECURITY.md).
"""

from __future__ import annotations

import ipaddress
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
    """The address rate limits are keyed on.

    Each trusted proxy appends the address it received the request from to X-Forwarded-For, so the
    entry `trusted_proxy_hops` places from the right was written by our own proxy. Everything to its
    left came from the client and can be forged; using the left-most entry (what uvicorn does with
    --forwarded-allow-ips="*") would let anyone pick a fresh rate-limit bucket per request.
    """
    hops = get_settings().trusted_proxy_hops
    peer = request.client.host if request.client else "unknown"
    if hops == 0:
        return peer
    forwarded = [
        part.strip()
        for value in request.headers.getlist("x-forwarded-for")
        for part in value.split(",")
        if part.strip()
    ]
    if not forwarded:
        return peer  # direct connection (local dev, tests)
    # Fewer entries than proxies means a proxy was bypassed or the setting is too high; the
    # left-most entry is then the closest thing to the client a proxy actually recorded.
    entry = _strip_port(forwarded[-hops] if len(forwarded) >= hops else forwarded[0])
    try:
        return str(ipaddress.ip_address(entry))
    except ValueError:
        return entry[:64]  # malformed but proxy-written: still a stable key


def _strip_port(entry: str) -> str:
    """Drop a port some proxies append ("198.51.100.7:51234", "[2001:db8::1]:443").

    That port is the client's ephemeral source port and changes on every connection; keeping it would
    give each new connection its own rate-limit bucket. Bare IPv6 addresses (several colons) are kept.
    """
    if entry.startswith("["):
        end = entry.find("]")
        return entry[1:end] if end > 0 else entry
    host, sep, port = entry.rpartition(":")
    if sep and port.isdigit() and host.count(":") == 0:
        return host
    return entry


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
