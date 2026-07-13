"""Runtime configuration, read from the environment.

In production these come from /etc/lake/lake.env via systemd's EnvironmentFile=,
so secrets never enter the process table or shell history.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LAKE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "production"] = "development"

    # storage
    nas_root: Path = Path("/mnt/nas/lake")
    staging_root: Path = Path("/var/lib/lake/staging")

    # metadata catalog
    db_dsn: str = "postgresql+psycopg://lake@/lake_meta?host=/var/run/postgresql"

    # logging
    log_dir: Path = Path("/var/log/lake")
    log_level: str = "INFO"
    log_json: bool = True

    # alerting
    alert_enabled: bool = True
    alert_ntfy_url: str | None = None

    # AI exploration (optional — the rest of the API works without it)
    anthropic_api_key: str | None = None
    #: comma-separated list of allowed CORS origins for the frontend
    api_cors_origins: str = "http://localhost:3000,http://localhost:5173"

    #: Where this API answers from, as the outside world reaches it. This is what
    #: goes into the copy-paste snippets on a dataset page, so it has to be the URL
    #: a reader can actually curl — not the one the frontend uses to reach us over
    #: the loopback. Behind a reverse proxy the two are different, and a snippet
    #: that says `127.0.0.1` is a snippet that works on exactly one machine.
    api_public_url: str = "http://localhost:8000"

    # Rate limiting
    api_rate_limit_enabled: bool = True
    #: comma-separated IPs whose X-Forwarded-For we trust (your reverse proxy).
    #: Empty by default: with no proxy, the socket peer is the real client.
    api_trusted_proxies: str = ""
    #: per-minute ceilings, per client IP. Override to taste.
    api_rate_catalog_per_min: int = 120
    api_rate_query_per_min: int = 30
    api_rate_ai_per_min: int = 6
    #: The admin login. Tight on purpose: it is the one endpoint an attacker can
    #: call in a loop for free, and a password worth guessing is worth guessing
    #: slowly. Ten a minute is generous for a human and useless for a dictionary.
    api_rate_login_per_min: int = 10

    # source registry
    sources_config: Path = Field(default=Path("configs/sources.yaml"))

    @property
    def raw_root(self) -> Path:
        return self.nas_root / "raw"

    @property
    def processed_root(self) -> Path:
        return self.nas_root / "processed"

    @property
    def quarantine_root(self) -> Path:
        return self.nas_root / "quarantine"

    @property
    def archive_root(self) -> Path:
        return self.nas_root / "archive"

    @property
    def meta_root(self) -> Path:
        return self.nas_root / "_meta"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
