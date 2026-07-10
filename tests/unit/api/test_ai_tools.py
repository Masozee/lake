"""The AI tool surface. No API key needed — dispatch() runs the tools directly.

The point being proved: there is no verb the model can use to change data, and
every rejection comes back as a readable error rather than a crash.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_list_tables_tool(replica):
    from lake.api.ai.tools import dispatch

    assert dispatch("list_tables", {}) == {"tables": ["gdp_annual"]}


def test_describe_tool(replica):
    from lake.api.ai.tools import dispatch

    result = dispatch("describe_table", {"table": "gdp_annual"})
    assert result["row_count"] == 5
    assert any(c["name"] == "gdp_usd" for c in result["columns"])


def test_run_sql_tool_aggregates(replica):
    from lake.api.ai.tools import dispatch

    result = dispatch(
        "run_sql",
        {
            "sql": (
                "SELECT country_iso3, sum(gdp_usd) g FROM lake.gdp_annual "
                "GROUP BY 1 ORDER BY g DESC NULLS LAST"
            )
        },
    )
    assert result["rows"][0][0] == "USA"


def test_run_sql_respects_the_ai_row_cap(replica):
    from lake.api.ai import tools

    result = tools.dispatch("run_sql", {"sql": "SELECT * FROM lake.gdp_annual", "limit": 999999})
    # capped at AI_MAX_LIMIT regardless of what the model asks for
    assert result["row_count"] <= tools.AI_MAX_LIMIT


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM lake.gdp_annual",
        "DROP TABLE lake.gdp_annual",
        "UPDATE lake.gdp_annual SET gdp_usd = 0",
        "SELECT * FROM read_csv('/etc/passwd')",
        "PRAGMA database_list",
        "COPY (SELECT 1) TO '/tmp/x.csv'",
        "ATTACH '/tmp/e.db' AS e",
        "INSTALL httpfs",
        "SELECT 1; DROP TABLE lake.gdp_annual",
    ],
)
def test_the_model_cannot_escape_read_only(replica, sql):
    from lake.api.ai.tools import dispatch

    result = dispatch("run_sql", {"sql": sql})
    assert "error" in result
    assert "rows" not in result


def test_there_is_no_write_tool(replica):
    """The clearest guarantee: no mutating verb exists in the toolset at all."""
    from lake.api.ai.tools import TOOL_DEFINITIONS

    names = {t["name"] for t in TOOL_DEFINITIONS}
    assert names == {"list_tables", "describe_table", "profile_table", "run_sql"}
    for forbidden in ("insert", "update", "delete", "write", "create", "drop", "edit"):
        assert not any(forbidden in n for n in names)


def test_unknown_tool_is_an_error_not_a_crash(replica):
    from lake.api.ai.tools import dispatch

    assert "error" in dispatch("delete_everything", {})


def test_missing_argument_is_a_clean_error(replica):
    from lake.api.ai.tools import dispatch

    result = dispatch("run_sql", {})
    assert result["error"] == "missing required argument: sql"


def test_agent_without_api_key_degrades(replica, monkeypatch):
    """No key -> a single error event, not an exception. The rest of the API works."""
    monkeypatch.delenv("LAKE_ANTHROPIC_API_KEY", raising=False)
    from lake.api.ai.agent import explore

    events = list(explore("how many countries are there?"))
    assert events[-1]["type"] == "error"
    assert "api key" in events[-1]["error"].lower()
