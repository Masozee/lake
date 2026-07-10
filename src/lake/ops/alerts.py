"""Alerting via ntfy. Three lines of HTTP, no server to keep alive on the NUC.

The ntfy topic name IS the authentication — use a long random one, treat it as a
secret, and keep it in /etc/lake/lake.env.

Two paths into here:
  1. systemd OnFailure=lake-alert@%i.service    -> a scraper crashed
  2. lake-freshness.timer -> lake check-freshness -> a scraper went silent

The second matters more. A scraper that stopped being scheduled never fails, so
OnFailure= structurally cannot see it. Only freshness can.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx

from lake.core.logging import get_logger
from lake.settings import get_settings

log = get_logger(__name__)

#: Freshness runs hourly. Without suppression a single dead source pages you 24
#: times a day, and you learn to ignore the notification — which is worse than
#: having none. Re-alert on the same condition at most once every 12 hours.
SUPPRESS_SECONDS = 12 * 3600


def _state_path() -> Path:
    return get_settings().staging_root.parent / "alert_state.json"


def _should_send(key: str, *, window: int = SUPPRESS_SECONDS) -> bool:
    """De-duplicate identical alerts within a window. Fails open: if the state
    file is unreadable we send, because a missed alert is worse than a repeat."""
    path = _state_path()
    now = time.time()
    try:
        state = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        return True

    if now - state.get(key, 0) < window:
        log.info("alert.suppressed", key=key, reason="within suppression window")
        return False

    state[key] = now
    # Prune anything older than a day so the file cannot grow without bound.
    state = {k: v for k, v in state.items() if now - v < 86400}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError:
        pass
    return True


def notify(
    title: str,
    body: str,
    *,
    priority: str = "high",
    tags: str = "warning",
) -> bool:
    """Best-effort. An alerting failure must never take down the job it reports on."""
    settings = get_settings()
    if not settings.alert_enabled or not settings.alert_ntfy_url:
        log.info("alert.suppressed", title=title, reason="alerting disabled or no URL")
        return False

    try:
        response = httpx.post(
            settings.alert_ntfy_url,
            content=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10.0,
        )
        response.raise_for_status()
        log.info("alert.sent", title=title)
        return True
    except httpx.HTTPError as exc:
        log.error("alert.failed", title=title, error=str(exc)[:200])
        return False


def alert_run_failed(source_id: str, unit: str | None = None) -> bool:
    from lake.metadata.repo import MetadataRepo

    runs = MetadataRepo().recent_runs(source_id=source_id, limit=1)
    detail = ""
    if runs:
        r = runs[0]
        detail = f"\nlogical_date: {r['logical_date']}\nattempt: {r['attempt']}"

    body = (
        f"Scraper failed: {source_id}{detail}\n\n"
        f"journalctl -u {unit or f'lake-scrape@{source_id}.service'} -n 50"
    )
    return notify(f"lake: {source_id} failed", body, priority="high", tags="rotating_light")


def alert_stale_sources(stale: list[dict]) -> bool:
    """One grouped message. Never twelve notifications because the NAS died.

    Suppressed for 12h *per distinct set of stale sources* — so a newly broken
    source pages immediately even while an old one is still stale, but the same
    unchanged failure does not page 24 times a day.
    """
    if not stale:
        return False

    ids = sorted(s["source_id"] for s in stale)
    key = "stale:" + hashlib.sha256(",".join(ids).encode()).hexdigest()[:16]
    if not _should_send(key):
        return False

    lines = []
    for s in stale:
        hours = s.get("hours_since_success")
        age = f"{hours:.0f}h" if hours is not None else "never"
        lines.append(f"  {s['source_id']}: last success {age} (SLA {s['freshness_sla_hours']}h)")

    body = f"{len(stale)} source(s) past their freshness SLA:\n\n" + "\n".join(lines)
    return notify(f"lake: {len(stale)} stale source(s)", body, priority="high", tags="hourglass")
