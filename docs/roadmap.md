# Roadmap

Each phase ends with a property you can test, not a checklist you can tick. Do not
start the next phase until the current one's exit criterion actually holds.

## Phase 1 — one scraper, by hand (week 1-2)

Get the storage layer right before anything else exists. Everything later depends
on `raw/` being trustworthy, and nothing later can fix it if it is not.

* NAS mounted via a `.mount` unit; `.lake_mounted` sentinel in place.
* `core/storage.py`: atomic commit, checksum, manifest, quarantine.
* Exactly one scraper. Run it by hand.
* SQLite for metadata is fine here.

**Exit:** `lake scrape X` writes a checksummed file to `raw/`, and killing it
mid-write leaves nothing behind. Verify by `kill -9` during a large download.

*Deliberately skipped:* orchestrator, dashboard, Postgres, Parquet.

## Phase 2 — schedules and the catalog (week 3-5)

* Postgres, alembic, the full schema. The partial unique index on `runs` is what
  makes idempotency real rather than aspirational.
* systemd timers, templated units, `Persistent=true`.
* `structlog` JSON, `OnFailure=` → ntfy, `lake-retry.timer`.
* Three to five sources spanning all four schedule types.
* `pg_dump` nightly, restic offsite.

**Exit:** reboot the NUC at 05:55. The 06:00 run fires at boot. Break a source's
URL; your phone buzzes within a minute.

## Phase 3 — validation and processed data (week 6-9)

The phase that stops the lake from quietly filling with garbage.

* Pydantic schemas per source; `quarantine/`; the `validations` table.
* Statistical gates: row count within 3σ, null rate, primary key uniqueness.
* Parser/transform split. DuckDB → `processed/*.parquet`.
* `lake backfill`. Real captured fixtures in `tests/`.

**Exit:** point a source at a URL that returns a 404 HTML page named `.xlsx`.
It is quarantined and you are paged. It does not reach `processed/`.

Consider Prefect here — but only if you now have genuine inter-task dependencies,
not merely several tasks.

## Phase 4 — visibility (week 10-12)

* Streamlit dashboard, bound to `127.0.0.1`.
* `v_freshness` checked hourly, with alert suppression.
* `lake archive`, `lake retention`.
* **`docs/runbook.md`, written before it is needed.**
* **Restore-test the catalog from backup. Write the date in the runbook.**

**Exit:** someone who is not you opens one page and correctly says which sources
are healthy. And you have restored the database from a backup at least once.

*Optional:* Grafana + Loki + Prometheus (~700 MB RAM) when you want log search and
duration trends. Not before.

## Phase 5 — lakehouse, if warranted (month 4+)

Only when a real need appears. In order of cost:

1. **DuckDB views over `processed/`** — a `.sql` file of `CREATE VIEW`s. This
   covers about 90% of what people mean when they say "we need a warehouse". Do
   this first, and stop here if it works.
2. **dbt-duckdb** — when transforms grow a dependency graph and you want tests,
   docs, and lineage for them.
3. **Apache Iceberg** (`pyiceberg` + DuckDB) — when you genuinely need schema
   evolution, time travel, or ACID over `processed/`. Real cost, real benefit.
4. **A Postgres serving layer, or MinIO as an S3 gateway** — only when external
   tools need a SQL or S3 endpoint.

Do not do any of this because it is on a roadmap. Do it the day someone asks a
question the current setup cannot answer.

## The order is not arbitrary

Storage before scheduling, because a scheduler that reliably runs a scraper which
writes corrupt files is worse than no scheduler. Scheduling before validation,
because you cannot compute a 3σ row-count band without run history. Validation
before the dashboard, because a dashboard that shows green while the data is
wrong is an active liability.
