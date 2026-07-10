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
    (nas / "processed" / "dataset=gdp_annual").mkdir(parents=True)
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

    # fabricate processed parquet with a full-permission builder connection
    build = duckdb.connect()
    build.execute(
        """
        COPY (
          SELECT * FROM (VALUES
            ('IDN','Indonesia',2023,1.37e12),
            ('IDN','Indonesia',2024,1.42e12),
            ('USA','United States',2023,2.77e13),
            ('USA','United States',2024,2.92e13),
            ('DEU','Germany',2024,NULL)
          ) t(country_iso3,country_name,year,gdp_usd)
        ) TO ? (FORMAT PARQUET, PARTITION_BY (year), OVERWRITE_OR_IGNORE)
        """,
        [str(nas / "processed" / "dataset=gdp_annual")],
    )
    build.close()

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
