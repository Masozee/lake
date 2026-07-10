"""FastAPI application factory.

    uv run uvicorn lake.api.app:app --host 127.0.0.1 --port 8000

Bind to localhost and reach it over Tailscale or behind an authenticating proxy,
exactly like the dashboard. The API is read-only, but the data is still yours.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from lake.api import engine
from lake.api.middleware import RateLimitMiddleware
from lake.api.routes import ai, data, ui
from lake.core.logging import configure, get_logger
from lake.settings import get_settings

_STATIC = Path(__file__).resolve().parent / "static"

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

    app.include_router(data.router, prefix="/api", tags=["data"])
    app.include_router(ai.router, prefix="/api/ai", tags=["ai"])

    # The htmx UI (pages + fragments) and its vendored assets. Same origin as the
    # API, so no CORS and no separate server — one process serves everything.
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    app.include_router(ui.router, tags=["ui"])

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
