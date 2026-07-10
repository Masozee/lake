"""Structured logging.

JSON lines in production: one event per line, always carrying run_id / source_id /
logical_date. That triple is what lets you reconstruct a single run from the
interleaved output of concurrent scrapers.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

import structlog

# Any key whose name contains one of these gets redacted before it reaches a
# log sink. Cheap insurance against a stray `log.info("req", headers=headers)`.
_SECRET_SUBSTRINGS = frozenset(
    {"api_key", "apikey", "token", "password", "secret", "authorization", "cookie", "dsn"}
)


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if any(s in key.lower() for s in _SECRET_SUBSTRINGS):
            event_dict[key] = "***"
    return event_dict


def configure(
    log_dir: Path | None = None,
    level: str = "INFO",
    json: bool = True,
) -> None:
    """Wire stdlib logging + structlog. Idempotent."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.FileHandler(log_dir / "lake.jsonl", encoding="utf-8"))
        except OSError:
            # Read-only or missing log dir must never stop a scrape. journald
            # still captures stdout, which is the sink that actually matters.
            pass

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "lake") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
