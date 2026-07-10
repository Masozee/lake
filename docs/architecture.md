# Architecture

## Is this a data lake, a warehouse, or a lakehouse?

**A data lake now; a lakehouse later, only if asked.**

A warehouse is the wrong shape: the sources emit PDF, HTML, images, and Excel
alongside CSV and JSON. Forcing a schema at ingest time means throwing away
everything that does not fit, and you cannot get it back.

So: immutable files on the NAS, a metadata catalog that knows what they are, and
a typed Parquet layer built *from* them. That is a lake with a catalog — which is
already 90% of what people mean by "lakehouse". The remaining 10% (schema
evolution, time travel, ACID) is Iceberg, and you should add it the day someone
asks a question the current setup cannot answer. Not before.

## The two machines

```
┌─────────────────────────────┐        ┌──────────────────────────┐
│ Intel NUC — compute         │        │ NAS — durable bytes      │
│                             │        │                          │
│  scrapers                   │        │  raw/       immutable    │
│  orchestrator (systemd)     │  NFS   │  processed/ parquet      │
│  Postgres  ← local SSD      │◄──────►│  archive/   tar.zst      │
│  staging   ← local SSD      │        │  quarantine/             │
│  dashboard                  │        │  _meta/backups/          │
└─────────────────────────────┘        └──────────────────────────┘
        stateless-ish                        RAID + snapshots
     reimage and restore                    the data survives
```

**The NUC** runs everything and stores almost nothing that matters. Code, venv,
secrets, and the metadata database live on its local SSD. If it dies: reimage,
remount, restore the catalog from backup. No data is lost.

Postgres and staging go on the **local SSD, never the NAS**. SQLite over NFS lies
about locking, and Postgres over NFS is worse. Staging on local disk also means a
crashed download cannot leave debris on the NAS.

**The NAS** stores bytes and does nothing clever. No database, no compute. Give it
redundant disks and — this matters more than any other line in this document —
**immutable snapshots that the NUC's `lake` user cannot delete.** If the NUC is
ever compromised, the snapshots are what save you.

## The flow

```
 [scraper]
     │ 1. fetch to staging on the NUC's local SSD
     ▼
 [staging]  ──2. structural gate: magic bytes, size, decompresses──┐
     │                                                             │ fail
     │ 3. checksum, fsync, atomic os.replace()                     ▼
     ▼                                                       [quarantine/]
 [raw/ on the NAS]  ← immutable, 0o440, hive-partitioned
     │
     ├──4. record run, file, checksum, observation──► [Postgres catalog]
     │                                                        │
     │ 5. DuckDB: validate, clean, rebuild partition          │
     ▼                                                        ▼
 [processed/*.parquet]                              [freshness alert]
     │                                                   [dashboard]
     │ 6. archive cold months to tar.zst
     ▼
 [archive/]  ──7. restic──►  offsite
```

Two rules make the whole thing debuggable two years later:

1. **Raw is immutable.** Everything downstream is rebuildable from it. That means
   a bad parse is never a data-loss event — you fix the parser and re-run.
2. **Nothing enters `raw/` unless it is complete.** Write to a temp file *inside*
   the destination directory, fsync, verify the digest, then `os.replace()`. A
   reader sees the whole file or nothing.

## What the catalog is for

Files on a disk cannot answer questions. The catalog can:

| Question | Answer |
|---|---|
| Did we get October inflation? | `runs` where `(source_id, logical_date)` |
| What is broken right now? | `v_freshness where is_stale` |
| Where did this Parquet come from? | `datasets.built_from_run → runs → file_observations → files.nas_path` |
| Which code produced this file? | `runs.git_sha` — check it out, replay against the same bytes |
| Did the source stop, or did we break? | `file_observations.was_new` vs `runs.status` |
| Are we silently dropping rows? | `validations.rows_rejected` over time |

That fifth row is the one most catalogs get wrong. A scraper that succeeds every
day while upstream quietly stopped publishing looks *identical* to a healthy
system — unless you record, per run, whether the bytes you fetched were new.

## Why the metadata DB is the crown jewel

Raw files can often be re-downloaded. The run history, the checksums, the lineage,
and the record of what you *once held and then deleted* cannot. Back up Postgres
nightly, push it offsite, and **restore-test it quarterly**.

## Deliberate non-goals

* **No Airflow.** 2 GB of scheduler, webserver, worker, and its own database, to
  run twelve jobs on a schedule. systemd already does this, with dependency
  ordering and journald for free.
* **No Docker.** One machine, one team, Python only, needing NAS access and a
  local Postgres socket. Containers would add a network namespace, a volume
  permission puzzle, and a registry — to solve isolation problems this system
  does not have. `ProtectSystem=strict` gives the isolation that matters.
* **No Spark.** DuckDB is out-of-core and handles tens of gigabytes on a NUC.
* **No Kafka.** These sources publish daily, monthly, and yearly. There is no
  stream.

Add any of them the day the constraint that rules it out stops being true.
