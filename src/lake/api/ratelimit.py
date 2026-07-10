"""In-process rate limiting. One NUC, one process — no Redis needed.

A token bucket per (client, bucket-name). Buckets refill continuously, so a
client that has been quiet gets burst capacity up to the bucket size, then is
throttled to the steady rate. Cheap, thread-safe, and it forgets idle clients so
memory does not grow without bound.

Three tiers, because the endpoints cost wildly different amounts:

    catalog   loose  — cheap metadata reads
    query     medium — a DuckDB scan, bounded by row/time caps already
    ai        strict — each call spawns model requests; the expensive one

Getting the client identity right matters. Behind a reverse proxy the socket peer
is the proxy, so every client would share one bucket. We read X-Forwarded-For,
but ONLY when the request actually came from a trusted proxy — otherwise a client
could spoof the header and get a fresh bucket per fake IP, defeating the limit.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from starlette.requests import Request


@dataclass(frozen=True, slots=True)
class Limit:
    """`capacity` tokens, refilled at `capacity / per_seconds` per second."""

    capacity: int
    per_seconds: float

    @property
    def refill_per_second(self) -> float:
        return self.capacity / self.per_seconds


# Tune to taste. These are per-client, per-tier.
LIMITS: dict[str, Limit] = {
    "catalog": Limit(capacity=120, per_seconds=60),  # 120/min
    "query": Limit(capacity=30, per_seconds=60),  # 30/min
    "ai": Limit(capacity=6, per_seconds=60),  # 6/min — each is costly
}


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Token-bucket limiter. `allow()` returns (ok, retry_after_seconds)."""

    def __init__(self, limits: dict[str, Limit] | None = None, *, idle_evict_seconds: float = 900):
        self._limits = limits or LIMITS
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = threading.Lock()
        self._idle_evict = idle_evict_seconds
        self._last_sweep = 0.0

    def allow(self, client: str, tier: str, *, now: float | None = None) -> tuple[bool, float]:
        limit = self._limits.get(tier)
        if limit is None:  # unknown tier is never limited
            return True, 0.0

        now = now if now is not None else time.monotonic()
        key = (client, tier)

        with self._lock:
            self._maybe_sweep(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(limit.capacity), updated=now)
                self._buckets[key] = bucket
            else:
                elapsed = now - bucket.updated
                bucket.tokens = min(
                    limit.capacity, bucket.tokens + elapsed * limit.refill_per_second
                )
                bucket.updated = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0

            # Seconds until one token is available.
            deficit = 1.0 - bucket.tokens
            return False, deficit / limit.refill_per_second

    def _maybe_sweep(self, now: float) -> None:
        """Drop buckets idle longer than the eviction window. Caller holds the lock."""
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        cutoff = now - self._idle_evict
        for key in [k for k, b in self._buckets.items() if b.updated < cutoff]:
            del self._buckets[key]


def client_identity(request: Request, *, trusted_proxies: frozenset[str]) -> str:
    """The IP to key the limiter on.

    Trust X-Forwarded-For only when the immediate peer is a known proxy; take the
    left-most (original client) entry. Otherwise use the socket peer, which cannot
    be spoofed.
    """
    peer = request.client.host if request.client else "unknown"

    if peer in trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # left-most is the original client; the rest are proxy hops
            return forwarded.split(",")[0].strip() or peer

    return peer
