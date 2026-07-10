"""The registry: YAML in, wired scraper out. Adding a source touches nothing else."""

from __future__ import annotations

import pytest

from lake.core.exceptions import ConfigError
from lake.registry import _expand_env, _load_raw, get_source_config, sources_for_schedule

YAML = """
version: 1
defaults:
  timeout_seconds: 60
  retry: {attempts: 5, backoff_seconds: 10}
sources:
  - source_id: alpha
    schedule: daily
    enabled: true
    module: lake.sources.worldbank_gdp.scraper:WorldBankGDPScraper
    api_key: "${env:TEST_LAKE_KEY}"
  - source_id: beta
    schedule: daily
    enabled: false
    module: x:Y
  - source_id: gamma
    schedule: monthly
    enabled: true
    module: x:Y
    timeout_seconds: 300
    retry: {attempts: 2}
"""


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "sources.yaml"
    path.write_text(YAML)
    _load_raw.cache_clear()
    monkeypatch.setenv("TEST_LAKE_KEY", "s3cr3t")
    yield path
    _load_raw.cache_clear()


def test_defaults_merge_into_each_source(registry):
    alpha = get_source_config("alpha", registry)
    assert alpha["timeout_seconds"] == 60
    assert alpha["retry"]["attempts"] == 5


def test_source_overrides_win_and_merge_deeply(registry):
    gamma = get_source_config("gamma", registry)
    assert gamma["timeout_seconds"] == 300  # overridden
    assert gamma["retry"]["attempts"] == 2  # overridden
    assert gamma["retry"]["backoff_seconds"] == 10  # inherited from defaults


def test_env_references_resolve(registry):
    assert get_source_config("alpha", registry)["api_key"] == "s3cr3t"


def test_missing_env_reference_is_a_config_error(registry, monkeypatch):
    monkeypatch.delenv("TEST_LAKE_KEY")
    with pytest.raises(ConfigError, match="TEST_LAKE_KEY"):
        get_source_config("alpha", registry)


def test_secrets_are_not_expanded_at_load_time(registry):
    """The cached config must never hold a resolved secret — it could be logged."""
    raw = _load_raw(str(registry))
    assert raw["alpha"]["api_key"] == "${env:TEST_LAKE_KEY}"


def test_schedule_filter_skips_disabled_sources(registry):
    assert sources_for_schedule("daily", registry) == ["alpha"]  # beta is disabled
    assert sources_for_schedule("monthly", registry) == ["gamma"]
    assert sources_for_schedule("yearly", registry) == []


def test_unknown_source_lists_the_known_ones(registry):
    with pytest.raises(ConfigError, match="alpha, beta, gamma"):
        get_source_config("nope", registry)


def test_expand_env_recurses_into_nested_structures(monkeypatch):
    monkeypatch.setenv("X", "1")
    out = _expand_env({"a": ["${env:X}", {"b": "${env:X}"}]})
    assert out == {"a": ["1", {"b": "1"}]}
