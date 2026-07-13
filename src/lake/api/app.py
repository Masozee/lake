"""FastAPI application factory.

    uv run uvicorn lake.api.app:app --host 127.0.0.1 --port 8000

Bind to localhost and reach it over Tailscale or behind an authenticating proxy,
exactly like the dashboard. The API is read-only, but the data is still yours.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from lake.api import engine
from lake.api.middleware import RateLimitMiddleware
from lake.api.routes import admin, ai, data, ui_json
from lake.core.logging import configure, get_logger
from lake.settings import get_settings

log = get_logger("lake.api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    configure(log_dir=settings.log_dir, level=settings.log_level, json=settings.log_json)
    # Open the read-only replica once at startup, so the first request is fast and
    # a missing replica fails loudly here rather than on a random request.
    try:
        engine.serving()
        log.info(
            "api.ready",
            tables=len(
                engine.serving()
                .execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'lake'"
                )
                .fetchall()
            ),
        )
    except FileNotFoundError:
        log.warning("api.no_replica", hint="run `lake serve build`")
    yield
    engine.close_serving()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="lake API",
        version="0.1.0",
        description="Read-only HTTP + AI access to the data lake.",
        lifespan=lifespan,
    )

    # Rate limiting runs BEFORE CORS in the request path. Middleware added later
    # wraps earlier ones, so adding the limiter after CORS means CORS is outermost
    # and a 429 still carries the right CORS headers for the browser to read it.
    if settings.api_rate_limit_enabled:
        trusted = frozenset(p.strip() for p in settings.api_trusted_proxies.split(",") if p.strip())
        app.add_middleware(RateLimitMiddleware, trusted_proxies=trusted)

    origins = [o.strip() for o in settings.api_cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # This app is now the API and nothing else. The website is a separate TanStack
    # Start app in web/, which reaches these routes over a proxy — so there are no
    # HTML pages, no templates, and no static assets served from here.
    app.include_router(data.router, prefix="/api", tags=["data"])
    app.include_router(ai.router, prefix="/api/ai", tags=["ai"])
    # View-shaped JSON for the frontend. Separate from /api/* because it answers
    # "what can a reader open", not "what tables exist".
    app.include_router(ui_json.router, prefix="/api/ui", tags=["ui"])
    # The admin panel. The only router here that writes, and the only one behind a
    # login. Its rate-limit tier is the strictest — see middleware._tier_for.
    app.include_router(admin.router, prefix="/api/admin", tags=["admin"])

    @app.get("/api/health")
    def health() -> dict:
        try:
            tables = engine.scalar(
                engine.serving().execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'lake'"
                )
            )
            return {"status": "ok", "tables": tables}
        except FileNotFoundError:
            return {"status": "no_replica", "hint": "run `lake serve build`"}

    return app


app = create_app()
