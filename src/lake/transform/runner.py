"""raw/ -> processed/*.parquet, via DuckDB.

Two properties matter more than speed:

* Idempotent. We rebuild a whole partition and swap it in; we never append.
  Re-running a transform twice must leave the same bytes, or backfills become
  a guessing game.

* Only reads trustworthy input. A run directory counts as input iff it holds a
  _MANIFEST.json with status='complete'. A scraper killed mid-write leaves a
  directory full of real-looking files and no manifest — and is ignored.

DuckDB is out-of-core: a NUC handles tens of GB here. Do not reach for Spark.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from lake.core.logging import get_logger
from lake.core.storage import MANIFEST_NAME, Storage, default_storage
from lake.metadata.repo import MetadataRepo
from lake.settings import get_settings
from lake.transform.quality import check_row_count_sane

log = get_logger(__name__)


def complete_run_dirs(raw_root: Path, source_id: str, storage: Storage) -> list[Path]:
    """Every run directory for a source that carries a 'complete' manifest."""
    base = raw_root / f"source={source_id}"
    if not base.is_dir():
        return []
    return sorted(
        manifest.parent
        for manifest in base.rglob(MANIFEST_NAME)
        if storage.is_complete(manifest.parent)
    )


def _connect():
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "transform needs the 'transform' extra: uv sync --extra transform"
        ) from exc

    con = duckdb.connect()
    settings = get_settings()
    # A NUC shares RAM with Postgres and the scrapers. Cap DuckDB explicitly
    # rather than discovering the limit via the OOM killer.
    con.execute("SET memory_limit='2GB'")
    con.execute("SET threads=4")
    con.execute(f"SET temp_directory='{settings.staging_root}/duckdb'")
    return con


# --- the one schema every source lands in ------------------------------------
#
# Every published observation is the same fact: at some `period`, some `series`
# had some `value`, in some `unit`. That is true of "Indonesia's GDP in 1998" and
# of "M2 in May 2026", so both are stored the same way and there is exactly one
# table to query, one shape for the AI to learn, and one grid for the browser.
#
# Getting here means seeing that SEKI's long/tidy shape is the *general* one and
# the World Bank's wide shape is the special case. The wide table names its
# measure in a column (`gdp_usd DOUBLE`), which reads beautifully for one measure
# and cannot hold a second. The long table names it in data (`series`, `unit`),
# which is uglier for one measure and holds a thousand.
#
# The columns, and why each earns its place:
#
#   source_id    who published it: 'worldbank_gdp', 'seki'. Also the outer
#                partition key, so each source owns its own bytes on disk.
#   dataset_id   which published thing this row belongs to. Two sources can both
#                have a series called "Japan" — 27 SEKI indicators collide with
#                World Bank country names — so nothing is identified by name alone.
#   group_id     a group *within* a dataset, and the publisher's own key for it:
#                'I.1.' is Bank Indonesia's numbering, 'NY.GDP.MKTP.CD' is the
#                World Bank's indicator code. Never NULL — see below.
#   group_title  what that group is called: 'Uang Beredar dan Faktor-Faktor…',
#                'GDP (current US$)'. A name, NOT a key: four SEKI titles are
#                shared by two or three different tables.
#   section      the part of the publication the group sits under, where the
#                publisher divides it into any: 'I. UANG DAN BANK'. NULL otherwise.
#   series       whatever the row is a time series OF. For SEKI that is the
#                indicator ("Uang Beredar Luas(M2)"); for GDP it is the country
#                ("Indonesia"). This is the merge's one real decision, and it is
#                the honest one: a country IS what the GDP series is of.
#   series_code  the publisher's own stable id for that series, when it has one:
#                'IDN' for Indonesia. NULL where the publisher gives none.
#   row_no       the publisher's own row order within a group, so SEKI reads the
#                way Bank Indonesia prints it. NULL where a source gives no order.
#   period       a DATE, always. A year becomes its first day, so 1998 and
#                May 2026 sort and filter with the same operator.
#   freq         how often the series is published: annual, monthly, quarterly.
#                The thing `period` alone cannot tell you.
#   value        the number. Nullable — the World Bank reports 2,681 missing
#                years, and a missing observation is not a zero.
#   unit         what the number is in. Mandatory, because a `value` column that
#                mixes 'Miliar Rp' and 'USD' and 'Persen' is a footgun without it.
#
# ## Why `group_id` is never NULL
#
# It used to be. The column was called `table_id` and held `TABEL1_1` — the name of
# a spreadsheet file on Bank Indonesia's web server — and the World Bank, which
# publishes one flat table, wrote NULL into it. Two things were wrong with that:
#
# 1. The key was the publisher's *file naming*, not their *identification*. Bank
#    Indonesia numbers its own tables `I.1.` through `XI.n.`, and those numbers are
#    unique across all 108 and never null. Keying on the filename meant the lake's
#    identity for a table would move if BI reorganised their web server.
#
# 2. NULL made the middle rung optional, and every consumer had to carry a branch
#    for "a source with no groups" — the resolver, the drill-down, the browse
#    predicate, the card builder. A shape that is only sometimes there is a shape
#    every caller gets to be wrong about.
#
# So each source now names its own groups, from what the publisher already gives:
#
#     seki           group_id = 'I.1.'            (Bank Indonesia's table numbering)
#     worldbank_gdp  group_id = 'NY.GDP.MKTP.CD'  (the World Bank's indicator code)
#
# `gdp_annual` publishes exactly one indicator today, so it has exactly one group.
# That is not a special case, it is a count — and the day it publishes a second one,
# nothing needs to change.
DATASET_ID = "observations"

#: Partitioned by source first, then year.
#:
#: `source_id` leads because each source is transformed independently — `lake
#: transform gdp_annual` must not touch SEKI's bytes — and the two overlap in
#: time (GDP runs 1960-2025, SEKI 1968-2026). Partitioning by year alone would
#: put both sources' rows in the same `year=1998/` directory, where one source's
#: `OVERWRITE_OR_IGNORE` rebuild would silently delete the other's. Leading with
#: the source gives each its own subtree to own, and the transform stays what the
#: module docstring promises: rebuild a partition, swap it in, never append.
#:
#: Year still partitions within that, because it is what almost every query
#: filters on and it keeps a partition to a few MB.
PARTITION_KEYS = ["source_id", "year"]

#: Every column, in order, that a source's `clean` relation must produce.
#:
#: `source_id` and `dataset_id` are not the same fact and both earn their place:
#: the first is who scraped it, the second is what they published. Today they map
#: one-to-one; the moment one scraper publishes two datasets they do not, and a
#: schema that conflated them would have to be migrated.
SCHEMA = (
    "source_id",
    "dataset_id",
    "group_id",
    "group_title",
    "section",
    "series",
    "series_code",
    "row_no",
    "period",
    "year",
    "freq",
    "value",
    "unit",
)


def _publish(
    con: Any,
    *,
    source_id: str,
    dataset_id: str,
    meta: MetadataRepo,
    validation_detail: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Gate, write, and swap in one source's slice of the merged table.

    Every source's transform ends here, so the quality gate cannot be forgotten and
    the swap cannot be done half a dozen subtly different ways. The caller has
    already built a TEMP TABLE `clean` in the shared SCHEMA; this owns everything
    after that.

    The write targets `dataset=observations/source_id=<source>/`, so a rebuild of
    one source replaces exactly its own bytes and cannot touch another's — which
    is what keeps a merged table as re-runnable as the two separate ones were.
    """
    settings = get_settings()

    # The relation must be exactly the shared schema, in order. Checked rather
    # than trusted: a column added to one transform and not the others would
    # otherwise produce a table whose shape depends on which source ran last.
    columns = tuple(r[0] for r in con.execute("DESCRIBE clean").fetchall())
    if columns != SCHEMA:
        raise RuntimeError(
            f"{source_id}: `clean` does not match the shared schema.\n"
            f"  expected: {SCHEMA}\n"
            f"  got:      {columns}"
        )

    rows: int = con.execute("SELECT count(*) FROM clean").fetchone()[0]

    # The statistical gate. Publishing zero rows is a perfectly successful HTTP
    # 200, and no check upstream of here can tell you the table went empty.
    history = meta.dataset_row_history(dataset_id)
    verdict = check_row_count_sane(rows, history)
    meta.record_validation(
        None,
        None,
        verdict.check_name,
        verdict.passed,
        rows_total=rows,
        # The validation row is a JSONB column in Postgres that gets queried; keep
        # it to scalars. A list of skipped table ids belongs in the return value,
        # which the CLI prints, not in a column someone will one day GROUP BY.
        detail={**verdict.detail, "dataset_id": dataset_id, **(validation_detail or {})},
    )

    # This source's own subtree inside the merged dataset.
    dataset_dir = settings.processed_root / f"dataset={DATASET_ID}"
    final_dir = dataset_dir / f"source_id={source_id}"
    tmp_dir = dataset_dir / f".{source_id}.tmp"

    if not verdict:
        con.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"quality gate failed for {dataset_id}: {verdict.detail}")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Written WITHOUT the source_id column in the path — the temp dir already is
    # the source's directory, and hive partitioning would nest a second one.
    con.execute(
        """
        COPY (SELECT * EXCLUDE (source_id) FROM clean) TO ?
             (FORMAT PARQUET, COMPRESSION ZSTD,
              PARTITION_BY (year), OVERWRITE_OR_IGNORE)
        """,
        [str(tmp_dir)],
    )
    con.close()

    # Atomic-ish swap. Not a single syscall, but the window is one rename wide and
    # readers keyed on source_id= never see a half-written partition set.
    old_dir = dataset_dir / f".{source_id}.old"
    shutil.rmtree(old_dir, ignore_errors=True)
    if final_dir.exists():
        final_dir.rename(old_dir)
    tmp_dir.rename(final_dir)
    shutil.rmtree(old_dir, ignore_errors=True)

    meta.record_dataset(
        dataset_id=dataset_id,
        source_id=source_id,
        nas_path=str(final_dir.relative_to(settings.nas_root)),
        row_count=rows,
        partition_keys=PARTITION_KEYS,
    )
    log.info("transform.complete", dataset_id=dataset_id, rows=rows, path=str(final_dir))
    return {"dataset_id": dataset_id, "rows": rows, "path": str(final_dir), **(extra or {})}


def transform_gdp(
    *,
    meta: MetadataRepo | None = None,
    storage: Storage | None = None,
) -> dict[str, Any]:
    """worldbank_gdp raw JSON -> the merged observations table.

    The World Bank publishes a *wide* table: one row per country-year, with the
    measure in its own named column. The merged table is long, so the country
    becomes the series — which is honest, because a country is exactly what a GDP
    series is a series *of*.
    """
    settings = get_settings()
    meta = meta or MetadataRepo()
    storage = storage or default_storage()
    storage.assert_mounted()

    run_dirs = complete_run_dirs(settings.raw_root, "worldbank_gdp", storage)
    if not run_dirs:
        raise RuntimeError("no complete run directories for worldbank_gdp")

    newest = run_dirs[-1]  # newest complete run wins; raw is immutable, so this is stable

    # Only the payload files. A run directory also holds `_MANIFEST.json` and a
    # `.meta.json` sidecar per artifact, and a bare `*.json` glob sweeps those in
    # too — whereupon DuckDB unifies three unrelated schemas and the payload's
    # `json` column vanishes. Name what we mean instead.
    payloads = [
        p
        for p in sorted(newest.glob("*.json"))
        if p.name != MANIFEST_NAME and not p.name.endswith(".meta.json")
    ]
    if not payloads:
        raise RuntimeError(f"worldbank_gdp: no payload JSON in {newest}")

    con = _connect()
    # A TEMP TABLE, not a TEMP VIEW: DuckDB cannot prepare a CREATE VIEW, so a
    # bound parameter inside one raises "Unexpected prepared parameter". The view
    # was read exactly once anyway, so materialising it costs nothing and keeps the
    # path a bound parameter rather than a string spliced into SQL.
    #
    # The World Bank answers with a two-element array: [pagination, records]. DuckDB
    # reads that as two ROWS — an object and an array — so the records are the row
    # that is an array, and `json_extract(json, '$[1]')` (which assumed one row
    # holding the whole document) silently reads nothing.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE src AS
        SELECT unnest(CAST(json AS JSON[])) AS rec
        FROM read_json(?, maximum_object_size=104857600)
        WHERE json_type(json) = 'ARRAY'
        """,
        [[str(p) for p in payloads]],
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE clean AS
        SELECT
            'worldbank_gdp'                                            AS source_id,
            'gdp_annual'                                               AS dataset_id,
            -- The group is the indicator, and the World Bank names it in every row:
            -- 'NY.GDP.MKTP.CD' / 'GDP (current US$)'. Read from the payload rather
            -- than hard-coded, so a run that starts returning a second indicator
            -- lands as a second group instead of being silently mislabelled as this
            -- one.
            json_extract_string(rec, '$.indicator.id')                 AS group_id,
            json_extract_string(rec, '$.indicator.value')              AS group_title,
            -- The World Bank divides its indicators into no sections, and does not
            -- order the countries within one.
            CAST(NULL AS VARCHAR)                                      AS section,
            -- The country IS the series. 'Indonesia' is what this row is a time
            -- series of, exactly as 'Uang Beredar Luas(M2)' is for SEKI.
            json_extract_string(rec, '$.country.value')                AS series,
            upper(json_extract_string(rec, '$.countryiso3code'))       AS series_code,
            CAST(NULL AS BIGINT)                                       AS row_no,
            -- An annual figure is dated to the first day of its year, so 1998 and
            -- May 2026 sort and filter with the same operator.
            make_date(CAST(json_extract_string(rec, '$.date') AS INTEGER), 1, 1) AS period,
            CAST(json_extract_string(rec, '$.date') AS INTEGER)        AS year,
            'annual'                                                   AS freq,
            -- Nullable on purpose: the World Bank reports 2,681 missing years, and
            -- a missing observation is not a zero.
            TRY_CAST(json_extract_string(rec, '$.value') AS DOUBLE)    AS value,
            'USD'                                                      AS unit
        FROM src
        WHERE json_extract_string(rec, '$.countryiso3code') <> ''
          AND json_extract_string(rec, '$.date') IS NOT NULL
          -- A row that names no indicator has no group, and a NULL group_id would
          -- be a row that belongs to the dataset but to nothing inside it — the
          -- exact hole this schema exists to close. Drop it rather than store it.
          AND json_extract_string(rec, '$.indicator.id') IS NOT NULL
        """
    )

    return _publish(con, source_id="worldbank_gdp", dataset_id="gdp_annual", meta=meta)


def transform_seki(
    *,
    meta: MetadataRepo | None = None,
    storage: Storage | None = None,
) -> dict[str, Any]:
    """SEKI's ~108 legacy .xls tables -> one long-format Parquet dataset.

    DuckDB cannot read BIFF .xls, so the workbooks are decoded in Python and
    handed to DuckDB as a single relation. That is affordable because the whole
    release is ~100k-1M rows, not because it is elegant.

    Sheets overlap on purpose: each table carries a current sheet plus year-range
    history sheets, and the same observation appears on both. Where they agree we
    keep one copy; where they disagree the later-revised figure wins, which is the
    one on the sheet whose data runs furthest forward.
    """
    from lake.sources.seki.tables import parse as parse_table

    settings = get_settings()
    meta = meta or MetadataRepo()
    storage = storage or default_storage()
    storage.assert_mounted()

    run_dirs = complete_run_dirs(settings.raw_root, "seki", storage)
    if not run_dirs:
        raise RuntimeError("no complete run directories for seki")
    newest = run_dirs[-1]

    catalogue = _seki_catalogue(newest)
    records: list[dict[str, Any]] = []
    skipped: list[str] = []

    for path in sorted(newest.glob("*.xls")):
        # `TABEL1_1` — the workbook's filename on Bank Indonesia's web server. It is
        # how the raw files are named and how the catalogue is keyed, so it is the
        # join key here; it is NOT what the table is called, and it does not survive
        # into the schema. Bank Indonesia's own numbering does.
        file_key = _seki_table_id(path.stem)
        try:
            parsed = parse_table(path.read_bytes(), table_id=file_key)
        except Exception as exc:  # one unreadable workbook is not a failed month
            log.warning("transform.table_failed", table_id=file_key, error=str(exc)[:200])
            skipped.append(file_key)
            continue
        if not parsed:
            skipped.append(file_key)
            continue

        info = catalogue.get(file_key, {})
        group_id = info.get("number")
        if not group_id:
            # No number means no group, and a row in a dataset but in nothing inside
            # it is the hole this schema exists to close. Skipping the table is loud
            # — the run reports it — where a NULL group would be silent.
            log.warning("transform.table_uncatalogued", table_id=file_key)
            skipped.append(file_key)
            continue

        for record in parsed:
            record["group_id"] = group_id
            record["group_title"] = info.get("title")
            record["section"] = info.get("section")
        records.extend(parsed)

    if not records:
        raise RuntimeError("seki: parsed zero observations from the newest run")
    log.info("transform.parsed", observations=len(records), skipped_tables=len(skipped))

    con = _connect()
    con.register("raw_records", _seki_arrow(records))
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE clean AS
        WITH scored AS (
            -- how far forward each sheet's data runs; a window function may not
            -- appear inside another window's ORDER BY, so it is materialised here
            SELECT *, max(period) OVER (PARTITION BY group_id, sheet) AS sheet_last_period
            FROM raw_records
        ),
        ranked AS (
            SELECT
                *,
                row_number() OVER (
                    PARTITION BY group_id, row_no, indicator, period
                    ORDER BY sheet_last_period DESC, sheet DESC
                ) AS pick
            FROM scored
        )
        SELECT
            'seki'                AS source_id,
            'seki_indicators'     AS dataset_id,
            -- `I.1.` — Bank Indonesia's own numbering, unique across all 108 tables.
            -- Not `TABEL1_1`, which is what they happen to call the spreadsheet.
            group_id, group_title, section,
            -- SEKI's `indicator` is the series: it is what the row is a series OF.
            -- Bank Indonesia gives no stable code for one, so series_code is NULL.
            indicator             AS series,
            CAST(NULL AS VARCHAR) AS series_code,
            row_no,
            period, year(period)  AS year,
            freq, value, unit
        FROM ranked
        WHERE pick = 1
        """
    )

    return _publish(
        con,
        source_id="seki",
        dataset_id="seki_indicators",
        meta=meta,
        validation_detail={"skipped_tables": len(skipped)},
        extra={"skipped": skipped},
    )


def _seki_table_id(stem: str) -> str:
    """`seki_20260701_TABEL1_1_1` -> `TABEL1_1_1`.

    The scraper names files `{source}_{stamp}_{table_id}.xls`, and table ids
    themselves contain underscores. Splitting from the right takes `1` off
    `TABEL1_1_1` and silently breaks the join to the catalogue, so the split has
    to come from the left and keep everything after the stamp.
    """
    parts = stem.split("_", 2)
    return parts[2] if len(parts) == 3 else stem


def _seki_catalogue(run_dir: Path) -> dict[str, dict[str, Any]]:
    """Table titles and sections, from the catalogue the scraper stored."""
    files = sorted(run_dir.glob("*_catalogue.json"))
    if not files:
        return {}
    entries = json.loads(files[-1].read_text(encoding="utf-8"))
    return {entry["table_id"]: entry for entry in entries}


def _seki_arrow(records: list[dict[str, Any]]):
    """Records -> an Arrow table DuckDB can query without a round trip to disk."""
    import pyarrow as pa

    columns = (
        "group_id",
        "group_title",
        "section",
        # The sheet a row was read off. Not part of the schema — it is how the
        # overlapping history sheets are de-duplicated, and nothing downstream of
        # that needs to know which one won.
        "sheet",
        "row_no",
        "indicator",
        "period",
        "freq",
        "value",
        "unit",
    )
    return pa.table({name: [record.get(name) for record in records] for name in columns})


TRANSFORMS = {
    "gdp_annual": transform_gdp,
    "seki_indicators": transform_seki,
}
