"""What the admin panel shows: the health of the pipeline, read from Postgres.

Everything here is a read. It answers the three questions that matter when
something is wrong, which are the same three the Streamlit dashboard answers:

* **what is stale** — a source past its SLA. Catches the scraper that silently
  stopped being scheduled, which never fails because it never runs.
* **what failed** — and with which error, on which attempt.
* **what went quiet** — succeeding, but every file it fetches is byte-identical to
  one already held. The source stopped publishing. That is not a scraper bug and
  it is a different fix; `file_observations.was_new` is the column that tells the
  two apart, and this is where it earns its place.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from lake.metadata.session import session_scope


def _rows(sql: str, **params: Any) -> list[dict[str, Any]]:
    with session_scope() as s:
        return [dict(r) for r in s.execute(text(sql), params).mappings()]


def _one(sql: str, **params: Any) -> Any:
    with session_scope() as s:
        return s.scalar(text(sql), params)


def health() -> dict[str, Any]:
    """The four numbers across the top of the panel."""
    fresh = freshness()
    stale = [f for f in fresh if f["is_stale"]]
    return {
        "sources": len(fresh),
        "stale": len(stale),
        "runs_24h": _one(
            "SELECT count(*) FROM runs WHERE started_at > now() - interval '24 hours'"
        ),
        "failures_24h": _one(
            """
            SELECT count(*) FROM runs
            WHERE status = 'failed' AND started_at > now() - interval '24 hours'
            """
        ),
        "stale_ids": [f["source_id"] for f in stale],
    }


def freshness() -> list[dict[str, Any]]:
    """Every enabled source and whether it is inside its SLA.

    `v_freshness` is a view in the catalog, not a query assembled here — the
    alerting CLI reads the same view, so the page and the pager can never disagree
    about what "stale" means.
    """
    # The cast is not cosmetic. `hours_since_success` is a `numeric` in the view,
    # which psycopg hands back as a Decimal and FastAPI serializes as a *string* —
    # so the browser gets "22.64", not 22.64, and any arithmetic on it silently
    # does the wrong thing (or, as it did here, throws). Cast once, at the source,
    # rather than coercing it back at every place that reads it.
    return _rows(
        """
        SELECT source_id, display_name, schedule, enabled, freshness_sla_hours,
               last_success_at, last_status,
               CAST(hours_since_success AS double precision) AS hours_since_success,
               is_stale
        FROM v_freshness
        ORDER BY is_stale DESC, hours_since_success DESC NULLS FIRST
        """
    )


def recent_runs(limit: int = 100, source_id: str | None = None) -> list[dict[str, Any]]:
    # The ::text cast is load-bearing. Without it Postgres cannot infer a type for
    # a bare NULL parameter — `$1 IS NULL OR col = $1` raises AmbiguousParameter —
    # so the unfiltered case, which is the common one, would fail outright.
    return _rows(
        """
        SELECT run_id, source_id, logical_date, status, attempt, trigger,
               file_count, bytes_written, duration_ms, started_at, finished_at
        FROM runs
        WHERE (CAST(:source_id AS text) IS NULL OR source_id = :source_id)
        ORDER BY started_at DESC
        LIMIT :limit
        """,
        limit=limit,
        source_id=source_id,
    )


def recent_errors(days: int = 7, limit: int = 100) -> list[dict[str, Any]]:
    return _rows(
        """
        SELECT r.source_id, r.logical_date, r.attempt,
               e.error_class, left(e.error_message, 400) AS error_message,
               e.occurred_at
        FROM run_errors e
        JOIN runs r USING (run_id)
        WHERE e.occurred_at > now() - make_interval(days => :days)
        ORDER BY e.occurred_at DESC
        LIMIT :limit
        """,
        days=days,
        limit=limit,
    )


def quiet_sources(days: int = 30) -> list[dict[str, Any]]:
    """Succeeding, but publishing nothing new.

    Every file fetched in the window was byte-identical to one already held. The
    scraper is fine; the *source* stopped. Conflating this with a failure is how
    you spend a day debugging code that was never broken.
    """
    return _rows(
        """
        SELECT r.source_id,
               max(o.observed_at)                        AS last_observed,
               count(*) FILTER (WHERE o.was_new)         AS new_files,
               count(*)                                  AS observations
        FROM file_observations o
        JOIN runs r USING (run_id)
        WHERE o.observed_at > now() - make_interval(days => :days)
        GROUP BY r.source_id
        HAVING count(*) FILTER (WHERE o.was_new) = 0
        ORDER BY max(o.observed_at) DESC
        """,
        days=days,
    )


def storage() -> list[dict[str, Any]]:
    """What each source has actually landed, by layer. The 'is the NAS filling up'
    question, answered per source rather than per mount."""
    return _rows(
        """
        SELECT source_id,
               count(*)                                          AS files,
               coalesce(sum(size_bytes), 0)                      AS bytes,
               count(*) FILTER (WHERE deleted_at IS NOT NULL)    AS deleted,
               count(*) FILTER (WHERE archived_at IS NOT NULL)   AS archived,
               max(first_seen_at)                                AS newest
        FROM files
        GROUP BY source_id
        ORDER BY bytes DESC
        """
    )


def datasets() -> list[dict[str, Any]]:
    """The processed Parquet outputs, with lineage back to the run that built each."""
    return _rows(
        """
        SELECT dataset_id, source_id, nas_path, format, row_count,
               partition_keys, built_from_run, built_at
        FROM datasets
        ORDER BY built_at DESC
        """
    )


def audit(limit: int = 100) -> list[dict[str, Any]]:
    """Who changed what, newest first. The panel's own paper trail."""
    return _rows(
        """
        SELECT entry_id, actor_email, action, target, detail, occurred_at
        FROM audit_log
        ORDER BY occurred_at DESC
        LIMIT :limit
        """,
        limit=limit,
    )
