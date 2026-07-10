"""In-run retry policy.

Two levels of retry exist in this system, and conflating them is a classic bug:

  * In-run (here): transient network faults, 5xx, 429. Exponential backoff with
    jitter, capped at ~5 attempts, inside a single process.
  * Cross-run (lake retry / lake-retry.timer): the whole run failed. A new run
    row is created with attempt=N+1. Bounded, durable, visible in the catalog.

4xx other than 408/429 are never retried. Those mean the source changed or our
code is wrong; retrying only hammers someone else's server.
"""

from __future__ import annotations

import httpx
from tenacity import (
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)
from tenacity import Retrying as _Retrying

from lake.core.logging import get_logger

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504, 507, 509})


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    if isinstance(exc, httpx.TransportError):
        # timeouts, connection resets, DNS failures, broken pools
        return True
    return getattr(exc, "transient", False) is True


def _log_retry(state: RetryCallState) -> None:
    exc = state.outcome.exception() if state.outcome else None
    log.warning(
        "fetch.retry",
        attempt=state.attempt_number,
        sleep_seconds=round(state.idle_for, 1),
        error_class=type(exc).__name__ if exc else None,
        error=str(exc)[:200] if exc else None,
    )


def retrying(
    attempts: int = 5,
    initial_seconds: float = 10.0,
    max_seconds: float = 600.0,
) -> _Retrying:
    """Build a tenacity Retrying controller.

    Usage:
        for attempt in retrying(attempts=5):
            with attempt:
                r = client.get(url)
                r.raise_for_status()
    """
    return _Retrying(
        stop=stop_after_attempt(attempts),
        wait=wait_exponential_jitter(initial=initial_seconds, max=max_seconds),
        retry=retry_if_exception(is_transient),
        before_sleep=_log_retry,
        reraise=True,
    )


def respect_retry_after(response: httpx.Response) -> float | None:
    """Honour a server's Retry-After. Being a good citizen keeps you unblocked."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None  # HTTP-date form; the backoff below is close enough
