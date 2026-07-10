"""Rate-limiting middleware. Maps each request to a tier and enforces its bucket."""

from __future__ import annotations

import math

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from lake.api.ratelimit import Limit, RateLimiter, client_identity
from lake.core.logging import get_logger
from lake.settings import get_settings

log = get_logger(__name__)


def _tier_for(path: str) -> str:
    """Which bucket a request draws from. AI is the strict, expensive one."""
    if path.startswith("/api/ai"):
        return "ai"
    # exports scan a whole table; count them as query-tier even under /api/tables
    if path.startswith("/api/query") or path.endswith((".csv", ".xlsx")):
        return "query"
    return "catalog"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, trusted_proxies: frozenset[str]):
        super().__init__(app)
        settings = get_settings()
        self._limiter = RateLimiter(
            {
                "catalog": Limit(settings.api_rate_catalog_per_min, 60),
                "query": Limit(settings.api_rate_query_per_min, 60),
                "ai": Limit(settings.api_rate_ai_per_min, 60),
            }
        )
        self._trusted = trusted_proxies

    async def dispatch(self, request: Request, call_next) -> Response:
        # Health checks and non-API paths (the frontend assets) are never limited.
        path = request.url.path
        if path == "/api/health" or not path.startswith("/api/"):
            return await call_next(request)

        tier = _tier_for(path)
        client = client_identity(request, trusted_proxies=self._trusted)
        allowed, retry_after = self._limiter.allow(client, tier)

        if not allowed:
            seconds = max(1, math.ceil(retry_after))
            log.info("ratelimit.blocked", client=client, tier=tier, path=path)
            return JSONResponse(
                status_code=429,
                content={"detail": f"rate limit exceeded for {tier} requests; retry in {seconds}s"},
                headers={"Retry-After": str(seconds)},
            )

        return await call_next(request)
