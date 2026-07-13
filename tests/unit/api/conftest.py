"""A real serving replica on a tmp path, and a TestClient over the real app.

These tests use the actual DuckDB engine — no mocks. The read-only guarantee is a
property of the engine, so a mock would test nothing worth testing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("fastapi")


@pytest.fixture
def replica(tmp_path, monkeypatch) -> Path:
    """Build a serving replica from a fabricated processed layer, wired via env."""
    import lake.settings as settings_module

    nas = tmp_path / "nas" / "lake"
    (nas / "processed").mkdir(parents=True)
    (nas / ".lake_mounted").touch()
    staging = tmp_path / "staging"
    staging.mkdir()

    # Drive real Settings through the environment, then bust the lru_cache so every
    # module that calls get_settings() sees our tmp paths.
    monkeypatch.setenv("LAKE_ENV", "development")
    monkeypatch.setenv("LAKE_NAS_ROOT", str(nas))
    monkeypatch.setenv("LAKE_STAGING_ROOT", str(staging))
    monkeypatch.setenv("LAKE_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("LAKE_ANTHROPIC_API_KEY", raising=False)
    settings_module.get_settings.cache_clear()

    # Every source lands in one long table, exactly as the transform writes it:
    # dataset=observations/source_id=<source>/year=<year>/. Both sources are here
    # because the whole point of the merge is that they share one shape.
    #
    # Small, but it reproduces the things that actually bite:
    #   * a series name reused across groups ("Lainnya" in I.1. and I.2.) — the name
    #     does not identify the series
    #   * a series name containing a colon ("Aset:")
    #   * a `row_no` that decides which series is a group's headline
    #   * two publishers keying their groups completely differently — Bank Indonesia
    #     numbers its tables (`I.1.`), the World Bank codes its indicators
    #     (`NY.GDP.MKTP.CD`) — and both filling the SAME column, never NULL
    build = duckdb.connect()
    observations = nas / "processed" / "dataset=observations"
    observations.mkdir(parents=True)

    # BOTH sources write EVERY column with EVERY type spelled out, exactly as the
    # real transform does — its `_publish` refuses a relation that does not match
    # the shared schema.
    #
    # The casts are not tidiness. DuckDB does not unify differing Parquet schemas
    # across hive partitions, and an untyped all-NULL column is inferred as INTEGER
    # — so SEKI's empty `series_code` would be an INTEGER column, and reading it
    # beside the World Bank's 'IDN' fails with a cast error. Naming the type is what
    # makes one shared table safe for two sources that fill different columns.
    def project(rows: str) -> str:
        return f"""
        COPY (
          SELECT
            CAST(dataset_id   AS VARCHAR) AS dataset_id,
            CAST(group_id     AS VARCHAR) AS group_id,
            CAST(group_title  AS VARCHAR) AS group_title,
            CAST(section      AS VARCHAR) AS section,
            CAST(series       AS VARCHAR) AS series,
            CAST(series_code  AS VARCHAR) AS series_code,
            CAST(row_no       AS BIGINT)  AS row_no,
            CAST(period       AS DATE)    AS period,
            CAST(year         AS BIGINT)  AS year,
            CAST(freq         AS VARCHAR) AS freq,
            CAST(value        AS DOUBLE)  AS value,
            CAST(unit         AS VARCHAR) AS unit
          FROM (VALUES {rows}) t(dataset_id, group_id, group_title,
                                 section, series, series_code, row_no,
                                 period, year, freq, value, unit)
        ) TO ? (FORMAT PARQUET, PARTITION_BY (year), OVERWRITE_OR_IGNORE)
        """

    # The World Bank publishes one indicator, and THAT is its group — keyed the way
    # the World Bank keys it. It has no sections and gives no row order, but it has a
    # group, because every source does. The country IS the series.
    build.execute(
        project("""
            ('gdp_annual','NY.GDP.MKTP.CD','GDP (current US$)', NULL, 'Indonesia',     'IDN', NULL,
             DATE '2023-01-01', 2023, 'annual', 1.37e12, 'USD'),
            ('gdp_annual','NY.GDP.MKTP.CD','GDP (current US$)', NULL, 'Indonesia',     'IDN', NULL,
             DATE '2024-01-01', 2024, 'annual', 1.42e12, 'USD'),
            ('gdp_annual','NY.GDP.MKTP.CD','GDP (current US$)', NULL, 'United States', 'USA', NULL,
             DATE '2023-01-01', 2023, 'annual', 2.77e13, 'USD'),
            ('gdp_annual','NY.GDP.MKTP.CD','GDP (current US$)', NULL, 'United States', 'USA', NULL,
             DATE '2024-01-01', 2024, 'annual', 2.92e13, 'USD'),
            -- a NULL value: a missing observation is not a zero
            ('gdp_annual','NY.GDP.MKTP.CD','GDP (current US$)', NULL, 'Germany',       'DEU', NULL,
             DATE '2024-01-01', 2024, 'annual', NULL,    'USD')
        """),
        [str(observations / "source_id=worldbank_gdp")],
    )

    # SEKI's groups are Bank Indonesia's own table numbers — `I.1.`, not `TABEL1_1`,
    # which is only what they call the spreadsheet on their web server.
    build.execute(
        project("""
            ('seki_indicators','I.1.','Uang Beredar','I. UANG DAN BANK',
             'M2',      NULL, 1, DATE '2024-01-01', 2024, 'monthly', 100.0, 'Miliar Rp'),
            ('seki_indicators','I.1.','Uang Beredar','I. UANG DAN BANK',
             'M2',      NULL, 1, DATE '2024-02-01', 2024, 'monthly', 110.0, 'Miliar Rp'),
            -- "Lainnya" again in I.2. below: the name does not identify the series.
            ('seki_indicators','I.1.','Uang Beredar','I. UANG DAN BANK',
             'Lainnya', NULL, 2, DATE '2024-01-01', 2024, 'monthly', 10.0,  'Miliar Rp'),
            -- A name ending in a colon — seven of SEKI's real ones do.
            ('seki_indicators','I.1.','Uang Beredar','I. UANG DAN BANK',
             'Aset:',   NULL, 3, DATE '2024-01-01', 2024, 'monthly', 5.0,   'Miliar Rp'),
            ('seki_indicators','I.2.','Suku Bunga','I. UANG DAN BANK',
             'Lainnya', NULL, 1, DATE '2024-01-01', 2024, 'monthly', 7.5,   'Persen')
        """),
        [str(observations / "source_id=seki")],
    )
    build.close()

    # The fixture must produce the schema the transform declares, or every test
    # below is asserting against a table production will never build.
    check = duckdb.connect()
    got = {
        r[0]
        for r in check.execute(
            "DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning = true)",
            [str(observations / "**" / "*.parquet")],
        ).fetchall()
    }
    check.close()
    from lake.transform.runner import SCHEMA as EXPECTED

    missing = set(EXPECTED) - got
    assert not missing, f"fixture parquet is missing columns: {sorted(missing)}"

    from lake.api import engine

    engine.close_serving()  # drop any connection from a previous test
    engine.build_replica()
    yield nas
    engine.close_serving()
    settings_module.get_settings.cache_clear()


@pytest.fixture
def client(replica):
    from fastapi.testclient import TestClient

    from lake.api.app import create_app

    with TestClient(create_app()) as c:
        yield c
