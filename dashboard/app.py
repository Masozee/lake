"""Job-status dashboard. ~80 lines, and it answers the only questions that matter:
what is stale, what failed, and why.

SECURITY: Streamlit ships no authentication. The systemd unit binds this to
127.0.0.1. Reach it over Tailscale or behind an authenticating reverse proxy.
Never expose port 8501.

Run:  uv run --extra dashboard streamlit run dashboard/app.py
"""

from __future__ import annotations

import pandas as pd
import sqlalchemy as sa
import streamlit as st

from lake.settings import get_settings

st.set_page_config(page_title="lake", page_icon="🌊", layout="wide")

settings = get_settings()


@st.cache_resource
def engine() -> sa.Engine:
    return sa.create_engine(settings.db_dsn, pool_pre_ping=True)


@st.cache_data(ttl=60)
def query(sql: str) -> pd.DataFrame:
    return pd.read_sql(sa.text(sql), engine())


st.title("🌊 lake")
st.caption(f"{settings.env} · {settings.nas_root}")

# --- health ------------------------------------------------------------------

fresh = query(
    "SELECT * FROM v_freshness ORDER BY is_stale DESC, hours_since_success DESC NULLS FIRST"
)
runs_24h = query("SELECT count(*) AS n FROM runs WHERE started_at > now() - interval '24 hours'")
fails_24h = query(
    """
    SELECT count(*) AS n FROM runs
    WHERE status = 'failed' AND started_at > now() - interval '24 hours'
    """
)

c1, c2, c3, c4 = st.columns(4)
c1.metric("Sources", len(fresh))
stale_count = int(fresh["is_stale"].fillna(True).sum()) if not fresh.empty else 0
c2.metric(
    "Stale",
    stale_count,
    delta=None if stale_count == 0 else "needs attention",
    delta_color="inverse",
)
c3.metric("Runs (24h)", int(runs_24h["n"].iloc[0]))
c4.metric("Failures (24h)", int(fails_24h["n"].iloc[0]))

if stale_count:
    st.error(f"{stale_count} source(s) past their freshness SLA — see the table below.")

# --- freshness ---------------------------------------------------------------

st.subheader("Freshness")
st.caption(
    "A stale source has not succeeded within its SLA. This catches a scraper that "
    "silently stopped being scheduled — which never fails, because it never runs."
)
if fresh.empty:
    st.info("No sources registered. Run `lake sync-sources`.")
else:
    st.dataframe(
        fresh[
            [
                "source_id",
                "schedule",
                "last_status",
                "hours_since_success",
                "freshness_sla_hours",
                "is_stale",
            ]
        ],
        use_container_width=True,
        hide_index=True,
        column_config={
            "hours_since_success": st.column_config.NumberColumn(
                "hours since success", format="%.1f"
            ),
            "is_stale": st.column_config.CheckboxColumn("stale"),
        },
    )

# --- runs --------------------------------------------------------------------

st.subheader("Recent runs")
runs = query(
    """
    SELECT source_id, logical_date, status, attempt, trigger,
           file_count, bytes_written, duration_ms, started_at
    FROM runs ORDER BY started_at DESC LIMIT 100
    """
)
st.dataframe(runs, use_container_width=True, hide_index=True)

# --- errors ------------------------------------------------------------------

st.subheader("Errors (7 days)")
errors = query(
    """
    SELECT r.source_id, r.logical_date, r.attempt,
           e.error_class, left(e.error_message, 160) AS message, e.occurred_at
    FROM run_errors e JOIN runs r USING (run_id)
    WHERE e.occurred_at > now() - interval '7 days'
    ORDER BY e.occurred_at DESC LIMIT 100
    """
)
if errors.empty:
    st.success("No errors in the last 7 days.")
else:
    st.dataframe(errors, use_container_width=True, hide_index=True)

# --- quiet sources -----------------------------------------------------------

st.subheader("Quiet sources")
st.caption(
    "Succeeding, but every file we fetch is byte-identical to one we already hold. "
    "The source stopped publishing — this is not a scraper bug, and it is a different "
    "fix. Distinguishing the two is the entire reason file_observations.was_new exists."
)
quiet = query(
    """
    SELECT r.source_id,
           max(o.observed_at) AS last_observed,
           count(*) FILTER (WHERE o.was_new) AS new_files,
           count(*) AS observations
    FROM file_observations o JOIN runs r USING (run_id)
    WHERE o.observed_at > now() - interval '30 days'
    GROUP BY r.source_id
    HAVING count(*) FILTER (WHERE o.was_new) = 0
    """
)
if quiet.empty:
    st.success("Every source has published something new in the last 30 days.")
else:
    st.warning(f"{len(quiet)} source(s) have published nothing new in 30 days.")
    st.dataframe(quiet, use_container_width=True, hide_index=True)
