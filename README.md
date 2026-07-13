# lake

A small, durable data lake for one server and one NAS.

Scrapers and API collectors run on daily / weekly / monthly / yearly schedules,
land immutable raw bytes on a NAS, record every run in a Postgres catalog, and
build typed Parquet for querying. It fits on an Intel NUC and is meant to still
be debuggable at 3am, two years from now, by someone who did not write it.

```
[systemd timer] → [scraper] → staging (NUC SSD) → checksum → raw/ (NAS, immutable)
                      ↓                                          ↓
              [Postgres catalog]  ← runs, files, errors → [DuckDB] → processed/*.parquet
                      ↓                                          ↓
              [freshness alert]                    [read-only replica] → [API] → [web/]
```

Every source lands in one table, `lake.observations`. One row is one observation:
at some `period`, the series named `series` had `value`, in some `unit` — as true
of "Indonesia's GDP in 1998" as of "M2 in May 2026". So there is one table to
query, one shape for the AI to learn, and one grid for the browser. See
[docs/api.md](docs/api.md).

## The five things that make it survive contact with reality

1. **`Requires=mnt-nas.mount`** plus a `.lake_mounted` sentinel checked in code.
   Scrapers refuse to run without the NAS. The alternative is silently filling
   the NUC's root disk for three weeks and noticing when Postgres won't start.

2. **Atomic commit into `raw/`, files land `0o440`.** A temp file inside the
   destination directory, fsync, verify the digest, then `os.replace()`. A reader
   sees the whole file or nothing. A partial file that looks real will haunt you
   for years.

3. **`Persistent=true` on every timer.** A NUC's real failure mode is being
   switched off, not being wrong. Missed runs fire at next boot.

4. **`file_observations.was_new`.** Separates *the source stopped publishing*
   from *our scraper broke*. Different alerts, different fixes. Most catalogs
   conflate them and you can never tell which happened.

5. **Freshness SLA alerting.** A scraper that silently stopped being scheduled
   never fails, because it never runs. `OnFailure=` structurally cannot see it.
   `lake check-freshness` can.

## Quick start (local)

```bash
uv venv && uv pip install -e '.[dev]'
cp .env.example .env          # then point LAKE_NAS_ROOT at a scratch dir

# a fake NAS for local work — the sentinel is what the mount guard looks for
mkdir -p /tmp/lake && touch /tmp/lake/.lake_mounted

createdb lake_meta
uv run alembic upgrade head
uv run lake sync-sources

uv run lake doctor            # preflight: NAS, database, registry, alerting
uv run pytest -q
```

## Everyday commands

```bash
lake scrape worldbank_gdp                       # one source, today
lake scrape bps_inflation -d 2026-06-01 --force # re-run a specific logical date
lake scrape-schedule daily                      # what the systemd timer runs
lake backfill bps_inflation --start 2024-01-01 --end 2026-06-01

lake status                                     # recent runs, newest first
lake status -s gov_news -n 5
lake check-freshness                            # what has gone quiet
lake doctor                                     # is anything obviously broken

lake retry                                      # cross-run retry, attempt=N+1
lake transform gdp_annual                       # rebuild processed Parquet
lake sweep                                      # clear staging, report quarantine
lake retention --apply                          # enforce deletion policy
```

Debugging a specific run:

```bash
journalctl -u lake-scrape@bps_inflation -f
journalctl -u lake-scrape@bps_inflation --since '2 days ago' -o cat | jq 'select(.level=="error")'
psql lake_meta -c 'SELECT * FROM v_freshness WHERE is_stale'
```

## Layout

| Path | Purpose |
|---|---|
| `src/lake/core/` | Shared plumbing: atomic storage, retry, logging, validation. Fix a bug once. |
| `src/lake/sources/<id>/` | One package per source. Delete the folder, delete the source. |
| `src/lake/metadata/` | The only code that talks to Postgres. |
| `src/lake/transform/` | raw → Parquet, via DuckDB. Idempotent: rebuilds, never appends. |
| `src/lake/ops/` | Alerting, staging sweep, archive, retention. |
| `configs/` | `sources.yaml` and `retention.yaml`. Ops-editable; no code change to add a source. |
| `deploy/systemd/` | Timers, templated units, sandboxing. Version-controlled infrastructure. |
| `migrations/` | Alembic. Schema changes are reviewable and reversible. |
| `tests/fixtures/` | Real captured responses. Sources break silently; fixtures notice. |
| `docs/runbook.md` | What to do at 3am. Written before you need it. |

## Scraper anatomy

Five layers, strictly separated, because that is what makes them testable:

```
config.yaml   what to fetch. no logic.
scraper.py    bytes in from the network. no parsing, no disk.
storage.py    bytes to the NAS, atomically. shared, not per-source.
parser.py     bytes -> list[dict]. pure: no network, no disk.
schema.py     dict -> validated record, or rejected.
transform.py  validated records -> parquet.
```

A parser is a pure function, so it tests against a captured fixture in
microseconds and goes red the day upstream renames a field.

## Deduplication, in four layers

1. **Idempotency** on `(source_id, logical_date)`, enforced by a partial unique
   index in Postgres — not by application logic.
2. **Content checksum**: identical bytes are not rewritten, but the *observation*
   is still recorded. See point 4 above.
3. **Conditional GET**: `ETag` / `If-Modified-Since` from the last success. A 304
   is `skipped_unchanged`, not a failure.
4. **Atomic commit**: no partial file ever exists in `raw/` to be mistaken for a
   real one.

## Retry, in two levels

Conflating these is a classic bug.

* **In-run** (`core/retry.py`): transient network faults, 5xx, 429. Exponential
  backoff with jitter, ~5 attempts, inside one process. Never retries a 404.
* **Cross-run** (`lake retry`, every 30 min): the whole run failed. A new run row
  with `attempt=N+1`. Bounded, durable, visible in the catalog.

The scraper units are `Type=oneshot` with **no `Restart=`**. Retrying a scrape at
the process level is an infinite loop against someone else's server.

## Deployment

systemd and a `uv` venv. Not Docker. One machine, one team, a Python-only
workload that needs NAS access and a local Postgres socket — containers would add
a network namespace, a volume-permission puzzle, and a registry to run, to solve
isolation problems this system does not have. systemd already provides restart
policy, dependency ordering, resource limits, `ProtectSystem=strict`, journald,
and `OnFailure=`.

See [docs/deployment.md](docs/deployment.md) and [docs/runbook.md](docs/runbook.md).

## Serving API & AI

A read-only HTTP API and an AI agent that can explore the data but cannot change
it. The read-only guarantee is structural — the DuckDB serving connection is
opened `read_only=True` with `enable_external_access=False`, which cannot be
undone at runtime — backed by a parser-level SQL guard. A TanStack Start frontend
(tables, a SQL/chart explorer, an AI chat) sits on top.

```bash
uv sync --extra api
uv run lake serve build      # processed/*.parquet -> read-only replica
uv run lake serve run        # API on 127.0.0.1:8000
make web                     # frontend on :3000, proxies /api
```

See [docs/api.md](docs/api.md). Bind everything to localhost; reach it over
Tailscale or an authenticating proxy — the public API has no auth of its own.

## Admin panel

`/admin` monitors the pipeline, edits `configs/sources.yaml`, and manages who can
do those things. It is the only part of the system that writes, and the only part
that has any concept of a user.

```bash
uv run alembic upgrade head                    # users, sessions, audit_log
uv run lake admin create-user you@example.org  # prompts; there is no web sign-up
```

Argon2id passwords, database-backed sessions (so revoking one actually revokes
it), and a rate-limited login. A config edit is validated before it is written,
backed up before it overwrites, and audited with the full previous content — which
is what stands in for the git commit a browser edit does not produce. It will not
accept a literal secret, and it will not sync the catalog for you.

This is a second lock on the same door, not a reason to open the first one: bind
to localhost and stay behind Tailscale or a proxy regardless.

See [docs/admin.md](docs/admin.md).

## Roadmap

Phase 1 storage + one manual scraper · Phase 2 timers + catalog · Phase 3
validation + Parquet · Phase 4 dashboard + alerting · Phase 5 lakehouse, *only*
when someone asks a question the current setup cannot answer.

See [docs/roadmap.md](docs/roadmap.md).
