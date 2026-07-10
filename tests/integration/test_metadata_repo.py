"""MetadataRepo against a real Postgres.

These skip when no database is reachable, so `pytest` still passes on a laptop.
They must run on the NUC and in CI — this is where the idempotency guard and the
retry query live, and neither can be exercised by a fake.

    createdb lake_meta_test
    LAKE_TEST_DB_DSN=postgresql+psycopg://$USER@localhost/lake_meta_test pytest tests/integration
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime

import pytest

sa = pytest.importorskip("sqlalchemy")

from lake.core.models import FetchedFile, RunContext  # noqa: E402
from lake.metadata.models import Base  # noqa: E402

DSN = os.environ.get("LAKE_TEST_DB_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set LAKE_TEST_DB_DSN to run catalog tests"),
]


@pytest.fixture(scope="module")
def engine():
    eng = sa.create_engine(DSN)
    try:
        with eng.connect() as c:
            c.execute(sa.text("SELECT 1"))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"database unreachable: {exc}")
    yield eng
    eng.dispose()


@pytest.fixture
def repo(engine, monkeypatch):
    """A repo bound to a freshly created scratch database."""
    from pathlib import Path

    from sqlalchemy.orm import sessionmaker

    from lake.metadata import session as session_module
    from lake.metadata.repo import MetadataRepo

    with engine.begin() as c:
        c.execute(sa.text("DROP VIEW IF EXISTS v_freshness"))
    Base.metadata.drop_all(engine)
    with engine.begin() as c:
        c.execute(sa.text("DROP TYPE IF EXISTS run_status"))
        c.execute(sa.text("DROP TYPE IF EXISTS schedule_kind"))
    Base.metadata.create_all(engine)

    # v_freshness belongs to the migration, not the ORM. Pull the exact SQL out of
    # the migration file so these tests exercise what production actually runs.
    migration = Path(__file__).resolve().parents[2] / "migrations/versions/0001_initial_catalog.py"
    namespace: dict = {}
    exec(compile(migration.read_text(), str(migration), "exec"), namespace)
    with engine.begin() as c:
        c.execute(sa.text(namespace["V_FRESHNESS"]))

    # session_scope() resolves get_sessionmaker() at call time, so this reaches
    # every repo method without touching the repo module.
    scratch = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: scratch)

    return MetadataRepo()


def _source(repo, source_id="s1", sla=24):
    repo.upsert_source(
        {
            "source_id": source_id,
            "display_name": source_id,
            "kind": "api",
            "schedule": "daily",
            "enabled": True,
            "freshness_sla_hours": sla,
        }
    )


def _ctx(source_id="s1", day=date(2026, 7, 9), attempt=1) -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        source_id=source_id,
        logical_date=day,
        started_at=datetime.now(UTC),
        attempt=attempt,
    )


# -- the idempotency guard ----------------------------------------------------


def test_two_successes_for_one_logical_date_are_impossible(repo, engine):
    """The partial unique index, not application logic, is what guarantees this."""
    _source(repo)
    a, b = _ctx(), _ctx()

    repo.start_run(a)
    repo.finish_run(a, status="success", file_count=1)

    repo.start_run(b)
    with pytest.raises(sa.exc.IntegrityError):
        repo.finish_run(b, status="success", file_count=1)


def test_run_succeeded_reflects_the_index(repo):
    _source(repo)
    ctx = _ctx()
    assert not repo.run_succeeded("s1", ctx.logical_date)

    repo.start_run(ctx)
    repo.finish_run(ctx, status="success")
    assert repo.run_succeeded("s1", ctx.logical_date)


def test_failed_runs_do_not_block_a_later_success(repo):
    _source(repo)
    bad = _ctx(attempt=1)
    repo.start_run(bad)
    repo.finish_run(bad, status="failed")

    good = _ctx(attempt=2)
    repo.start_run(good)
    repo.finish_run(good, status="success")  # must not raise

    assert repo.run_succeeded("s1", good.logical_date)


# -- content dedupe -----------------------------------------------------------


def test_same_checksum_twice_yields_one_file_row(repo):
    _source(repo)
    ctx = _ctx()
    repo.start_run(ctx)
    art = FetchedFile(filename="a.json", content=b'{"a":1}', url="u")

    first = repo.record_file(ctx, art, "raw/a.json")
    assert repo.find_file_by_checksum("s1", art.sha256) == first

    # a concurrent run racing on the same bytes adopts the existing row
    ctx2 = _ctx(day=date(2026, 7, 10))
    repo.start_run(ctx2)
    second = repo.record_file(ctx2, art, "raw/b.json")
    assert second == first


def test_observation_records_was_new_false_for_a_duplicate(repo):
    """The column that separates 'source went quiet' from 'scraper broke'."""
    _source(repo)
    ctx = _ctx()
    repo.start_run(ctx)
    art = FetchedFile(filename="a.json", content=b'{"a":1}', url="u", etag='"v1"')

    file_id = repo.record_file(ctx, art, "raw/a.json")
    repo.record_observation(ctx, file_id, art, was_new=True)
    repo.finish_run(ctx, status="success")

    ctx2 = _ctx(day=date(2026, 7, 10))
    repo.start_run(ctx2)
    repo.record_observation(ctx2, file_id, art, was_new=False)
    repo.finish_run(ctx2, status="success")

    assert repo.last_success_headers("s1") == {"etag": '"v1"'}


# -- the retry query ----------------------------------------------------------


def _backdate(engine, run_id, *, minutes):
    with engine.begin() as c:
        c.execute(
            sa.text(
                "UPDATE runs SET finished_at = now() - make_interval(mins => :m),"
                "               started_at  = now() - make_interval(mins => :m)"
                " WHERE run_id = :r"
            ),
            {"m": minutes, "r": run_id},
        )


def test_a_recent_failure_is_not_retried_yet(repo, engine):
    _source(repo)
    ctx = _ctx()
    repo.start_run(ctx)
    repo.finish_run(ctx, status="failed")

    assert repo.failed_runs_to_retry(older_than_minutes=15) == []


def test_an_old_failure_is_retried(repo, engine):
    _source(repo)
    ctx = _ctx()
    repo.start_run(ctx)
    repo.finish_run(ctx, status="failed")
    _backdate(engine, ctx.run_id, minutes=60)

    rows = repo.failed_runs_to_retry(older_than_minutes=15)
    assert [(r["source_id"], r["attempt"]) for r in rows] == [("s1", 1)]


def test_a_date_that_since_succeeded_is_never_retried(repo, engine):
    """A manual re-run must not be clobbered by the retry timer."""
    _source(repo)
    bad = _ctx(attempt=1)
    repo.start_run(bad)
    repo.finish_run(bad, status="failed")
    _backdate(engine, bad.run_id, minutes=60)

    good = _ctx(attempt=2)
    repo.start_run(good)
    repo.finish_run(good, status="success")

    assert repo.failed_runs_to_retry(older_than_minutes=15) == []


def test_skipped_unchanged_also_closes_a_logical_date(repo, engine):
    _source(repo)
    bad = _ctx(attempt=1)
    repo.start_run(bad)
    repo.finish_run(bad, status="failed")
    _backdate(engine, bad.run_id, minutes=60)

    skip = _ctx(attempt=2)
    repo.start_run(skip)
    repo.finish_run(skip, status="skipped_unchanged")

    assert repo.failed_runs_to_retry(older_than_minutes=15) == []


def test_the_attempt_cap_is_honoured(repo, engine):
    _source(repo)
    for attempt in (1, 2, 3):
        ctx = _ctx(attempt=attempt)
        repo.start_run(ctx)
        repo.finish_run(ctx, status="failed")
        _backdate(engine, ctx.run_id, minutes=60)

    assert repo.failed_runs_to_retry(max_attempts=3) == []
    assert len(repo.failed_runs_to_retry(max_attempts=4)) == 1


def test_only_the_latest_failed_attempt_is_returned(repo, engine):
    """Attempts advance 1 -> 2 -> 3; they must not fan out."""
    _source(repo)
    for attempt in (1, 2):
        ctx = _ctx(attempt=attempt)
        repo.start_run(ctx)
        repo.finish_run(ctx, status="failed")
        _backdate(engine, ctx.run_id, minutes=60)

    rows = repo.failed_runs_to_retry(max_attempts=5)
    assert [r["attempt"] for r in rows] == [2]


def test_an_in_flight_run_blocks_a_retry(repo, engine):
    """Never run the same scrape twice at once."""
    _source(repo)
    failed = _ctx(attempt=1)
    repo.start_run(failed)
    repo.finish_run(failed, status="failed")
    _backdate(engine, failed.run_id, minutes=60)

    live = _ctx(attempt=2)
    repo.start_run(live)  # still 'running'

    assert repo.failed_runs_to_retry(max_attempts=5) == []


def test_a_run_stuck_in_running_does_not_freeze_the_date_forever(repo, engine):
    """A single `kill -9` must not block a logical_date for eternity.

    After stale_after_hours the 'running' row is a corpse, not a worker.
    """
    _source(repo)
    failed = _ctx(attempt=1)
    repo.start_run(failed)
    repo.finish_run(failed, status="failed")
    _backdate(engine, failed.run_id, minutes=600)

    zombie = _ctx(attempt=2)
    repo.start_run(zombie)  # 'running', never finished
    _backdate(engine, zombie.run_id, minutes=600)  # ten hours ago

    rows = repo.failed_runs_to_retry(max_attempts=5, stale_after_hours=6)
    assert [r["attempt"] for r in rows] == [1], "the zombie froze the logical_date"


# -- freshness ----------------------------------------------------------------


def test_a_source_that_never_ran_is_stale(repo):
    _source(repo, sla=24)
    stale = repo.stale_sources()
    assert [s["source_id"] for s in stale] == ["s1"]
    assert stale[0]["hours_since_success"] is None


def test_a_recent_success_is_fresh(repo):
    _source(repo, sla=24)
    ctx = _ctx()
    repo.start_run(ctx)
    repo.finish_run(ctx, status="success")

    assert repo.stale_sources() == []


def test_an_old_success_goes_stale(repo, engine):
    _source(repo, sla=24)
    ctx = _ctx()
    repo.start_run(ctx)
    repo.finish_run(ctx, status="success")

    with engine.begin() as c:
        c.execute(
            sa.text("UPDATE runs SET finished_at = now() - interval '30 hours' WHERE run_id = :r"),
            {"r": ctx.run_id},
        )

    stale = repo.stale_sources()
    assert [s["source_id"] for s in stale] == ["s1"]
    assert stale[0]["hours_since_success"] > 24


def test_a_disabled_source_is_never_stale(repo):
    """v_freshness filters on enabled. A source you turned off must not page you."""
    repo.upsert_source(
        {
            "source_id": "off",
            "display_name": "off",
            "kind": "api",
            "schedule": "daily",
            "enabled": False,
            "freshness_sla_hours": 1,
        }
    )
    assert repo.stale_sources() == []


def test_a_source_without_an_sla_is_never_stale(repo):
    repo.upsert_source(
        {
            "source_id": "nosla",
            "display_name": "nosla",
            "kind": "api",
            "schedule": "adhoc",
            "enabled": True,
        }
    )
    assert repo.stale_sources() == []
