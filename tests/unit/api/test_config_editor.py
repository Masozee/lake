"""Editing configs/sources.yaml from the browser.

The file is git-tracked and normally changes only through review. Editing it from
a web form bypasses that, so these tests are the review: every way a valid-looking
YAML file could still take a scraper down, refused.

The write path matters as much as the validation. A refused edit must leave the
file byte-identical, and an accepted one must be backed up first — because the
undo is the only thing standing where `git revert` used to.
"""

from __future__ import annotations

import pytest

yaml = pytest.importorskip("yaml")

from lake.api.admin.config_editor import (  # noqa: E402
    InvalidConfig,
    list_backups,
    read_backup,
    validate,
    write_config,
)

#: A module that really does import, so a valid case is genuinely valid.
REAL_MODULE = "lake.sources.gov_news.scraper:GovNewsScraper"

GOOD = f"""
defaults:
  timeout_seconds: 60
sources:
  - source_id: example
    display_name: "An example source"
    kind: api
    schedule: daily
    enabled: true
    freshness_sla_hours: 30
    module: {REAL_MODULE}
"""


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A scratch sources.yaml, wired in through the settings the editor reads."""
    import lake.settings as settings_module

    configs = tmp_path / "configs"
    configs.mkdir()
    path = configs / "sources.yaml"
    path.write_text(GOOD, encoding="utf-8")

    monkeypatch.setenv("LAKE_SOURCES_CONFIG", str(path))
    settings_module.get_settings.cache_clear()
    yield path
    settings_module.get_settings.cache_clear()


def _one_source(**overrides) -> str:
    """A single-source file, with fields overridden. Serialised, not hand-written,
    so a test cannot accidentally assert on a typo."""
    source = {
        "source_id": "example",
        "display_name": "An example source",
        "kind": "api",
        "schedule": "daily",
        "module": REAL_MODULE,
    }
    source.update(overrides)
    return yaml.safe_dump({"sources": [source]})


# --- what a valid file looks like --------------------------------------------


def test_the_real_registry_validates(registry):
    """The file the project actually ships must pass its own editor."""
    from pathlib import Path

    shipped = Path(__file__).resolve().parents[3] / "configs/sources.yaml"
    assert validate(shipped.read_text(encoding="utf-8"))


def test_a_good_file_returns_its_sources(registry):
    assert sorted(validate(GOOD)) == ["example"]


def test_defaults_are_merged_into_each_source(registry):
    """`freshness_sla_hours` set in defaults must reach a source that omits it —
    the same merge the registry does, or the editor would validate a file the
    loader then reads differently."""
    merged = validate(
        yaml.safe_dump(
            {
                "defaults": {"freshness_sla_hours": 48},
                "sources": [
                    {
                        "source_id": "example",
                        "display_name": "X",
                        "kind": "api",
                        "schedule": "daily",
                        "module": REAL_MODULE,
                    }
                ],
            }
        )
    )
    assert merged["example"]["freshness_sla_hours"] == 48


# --- every way a file can be wrong -------------------------------------------


def test_malformed_yaml_is_refused(registry):
    with pytest.raises(InvalidConfig, match="not valid YAML"):
        validate("sources: [unclosed")


def test_a_file_with_no_sources_is_refused(registry):
    with pytest.raises(InvalidConfig, match="non-empty list"):
        validate("defaults: {}")


def test_a_schedule_with_no_timer_is_refused(registry):
    """The silent killer. A source on an unknown schedule is written, synced, and
    then never runs — which is exactly the failure `check-freshness` exists for."""
    with pytest.raises(InvalidConfig, match="has no timer"):
        validate(_one_source(schedule="hourly"))


def test_an_unimportable_module_is_refused(registry):
    """Caught here, or it fails at scrape time, hours later, in a log nobody reads."""
    with pytest.raises(InvalidConfig, match="cannot load module"):
        validate(_one_source(module="lake.sources.nope:Nope"))


def test_an_unknown_kind_is_refused(registry):
    with pytest.raises(InvalidConfig, match="is not one of"):
        validate(_one_source(kind="carrier-pigeon"))


def test_a_missing_display_name_is_refused(registry):
    with pytest.raises(InvalidConfig, match="display_name is required"):
        validate(_one_source(display_name=""))


@pytest.mark.parametrize("sla", [-5, 0, "soon"])
def test_a_nonsense_sla_is_refused(registry, sla):
    with pytest.raises(InvalidConfig, match="positive whole number"):
        validate(_one_source(freshness_sla_hours=sla))


def test_a_duplicate_source_id_is_refused(registry):
    """Two entries with one id: the second silently wins, and the first vanishes."""
    doc = yaml.safe_load(_one_source())
    doc["sources"].append(dict(doc["sources"][0]))
    with pytest.raises(InvalidConfig, match="duplicate source_id"):
        validate(yaml.safe_dump(doc))


def test_every_problem_is_reported_at_once(registry):
    """One error per submit is a form nobody finishes."""
    with pytest.raises(InvalidConfig) as exc:
        validate(_one_source(schedule="hourly", kind="pigeon", module="nope:Nope"))
    assert len(exc.value.errors) == 3


# --- secrets -----------------------------------------------------------------


@pytest.mark.parametrize("key", ["api_key", "token", "password", "secret"])
def test_a_literal_secret_is_refused(registry, key):
    """A literal key in a git-tracked file is a leak. The ${env:VAR} indirection
    exists for exactly this, so say so rather than quietly committing it."""
    with pytest.raises(InvalidConfig, match="looks like a literal secret"):
        validate(_one_source(**{key: "sk-live-abc123"}))


def test_an_env_reference_is_accepted(registry):
    """The indirection is the whole point — it must still work."""
    assert validate(_one_source(api_key="${env:MY_KEY}"))


# --- the write path ----------------------------------------------------------


def test_a_refused_edit_leaves_the_file_untouched(registry):
    """The single most important property here: a bad save changes nothing."""
    before = registry.read_text(encoding="utf-8")

    with pytest.raises(InvalidConfig):
        write_config("sources: [garbage")

    assert registry.read_text(encoding="utf-8") == before
    assert list_backups() == []  # and takes no backup either


def test_a_good_edit_is_written_and_backed_up(registry):
    before = registry.read_text(encoding="utf-8")
    after = before + "\n# an edit\n"

    backup = write_config(after)

    assert registry.read_text(encoding="utf-8") == after
    assert backup.read_text(encoding="utf-8") == before  # the undo


def test_comments_survive_a_round_trip(registry):
    """The editor writes text, never a re-serialised parse tree. In this file the
    comments are the only record of *why* a source is configured as it is, and a
    YAML round trip would eat every one of them."""
    content = "# keep me\n" + GOOD
    write_config(content)
    assert "# keep me" in registry.read_text(encoding="utf-8")


def test_backups_are_listed_newest_first(registry):
    write_config(GOOD + "\n# one\n")
    write_config(GOOD + "\n# two\n")

    backups = list_backups()
    assert len(backups) == 2
    assert backups[0]["name"] > backups[1]["name"]  # names sort chronologically


def test_a_backup_can_be_read_back(registry):
    before = registry.read_text(encoding="utf-8")
    backup = write_config(GOOD + "\n# edited\n")

    assert read_backup(backup.name) == before


def test_a_backup_name_cannot_escape_the_backup_directory(registry):
    """`../../etc/passwd` reads nothing."""
    write_config(GOOD + "\n# make a backup dir\n")

    with pytest.raises(FileNotFoundError):
        read_backup("../../../etc/passwd")
