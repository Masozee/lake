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


def transform_gdp(
    *,
    meta: MetadataRepo | None = None,
    storage: Storage | None = None,
) -> dict[str, Any]:
    """Worked example: worldbank_gdp raw JSON -> processed Parquet, partitioned by year."""
    settings = get_settings()
    meta = meta or MetadataRepo()
    storage = storage or default_storage()
    storage.assert_mounted()

    run_dirs = complete_run_dirs(settings.raw_root, "worldbank_gdp", storage)
    if not run_dirs:
        raise RuntimeError("no complete run directories for worldbank_gdp")

    newest = run_dirs[-1]  # newest complete run wins; raw is immutable, so this is stable
    pattern = str(newest / "*.json")

    dataset_id = "gdp_annual"
    final_dir = settings.processed_root / f"dataset={dataset_id}"
    tmp_dir = final_dir.with_name(final_dir.name + ".tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    con = _connect()
    con.execute(
        """
        CREATE OR REPLACE TEMP VIEW src AS
        SELECT
            unnest(json_extract(json, '$[1]')) AS rec
        FROM read_json(?, format='auto', maximum_object_size=104857600)
        """,
        [pattern],
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE clean AS
        SELECT
            upper(json_extract_string(rec, '$.countryiso3code'))       AS country_iso3,
            json_extract_string(rec, '$.country.value')                AS country_name,
            CAST(json_extract_string(rec, '$.date') AS INTEGER)        AS year,
            TRY_CAST(json_extract_string(rec, '$.value') AS DOUBLE)    AS gdp_usd,
            json_extract_string(rec, '$.indicator.id')                 AS indicator
        FROM src
        WHERE json_extract_string(rec, '$.countryiso3code') <> ''
          AND json_extract_string(rec, '$.date') IS NOT NULL
        """
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
        detail={**verdict.detail, "dataset_id": dataset_id},
    )
    if not verdict:
        con.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"quality gate failed for {dataset_id}: {verdict.detail}")

    con.execute(
        """
        COPY clean TO ? (FORMAT PARQUET, COMPRESSION ZSTD,
                         PARTITION_BY (year), OVERWRITE_OR_IGNORE)
        """,
        [str(tmp_dir)],
    )
    con.close()

    # Atomic-ish swap. Not a single syscall, but the window is one rename wide
    # and readers keyed on dataset= never see a half-written partition set.
    old_dir = final_dir.with_name(final_dir.name + ".old")
    shutil.rmtree(old_dir, ignore_errors=True)
    if final_dir.exists():
        final_dir.rename(old_dir)
    tmp_dir.rename(final_dir)
    shutil.rmtree(old_dir, ignore_errors=True)

    meta.record_dataset(
        dataset_id=dataset_id,
        source_id="worldbank_gdp",
        nas_path=str(final_dir.relative_to(settings.nas_root)),
        row_count=rows,
        partition_keys=["year"],
    )
    log.info("transform.complete", dataset_id=dataset_id, rows=rows, path=str(final_dir))
    return {"dataset_id": dataset_id, "rows": rows, "path": str(final_dir)}


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
        table_id = _seki_table_id(path.stem)
        try:
            parsed = parse_table(path.read_bytes(), table_id=table_id)
        except Exception as exc:  # one unreadable workbook is not a failed month
            log.warning("transform.table_failed", table_id=table_id, error=str(exc)[:200])
            skipped.append(table_id)
            continue
        if not parsed:
            skipped.append(table_id)
            continue
        info = catalogue.get(table_id, {})
        for record in parsed:
            record["section"] = info.get("section")
            record["table_title"] = info.get("title")
            record["table_number"] = info.get("number")
        records.extend(parsed)

    if not records:
        raise RuntimeError("seki: parsed zero observations from the newest run")
    log.info("transform.parsed", observations=len(records), skipped_tables=len(skipped))

    dataset_id = "seki_indicators"
    final_dir = settings.processed_root / f"dataset={dataset_id}"
    tmp_dir = final_dir.with_name(final_dir.name + ".tmp")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    con = _connect()
    con.register("raw_records", _seki_arrow(records))
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE clean AS
        WITH scored AS (
            -- how far forward each sheet's data runs; a window function may not
            -- appear inside another window's ORDER BY, so it is materialised here
            SELECT *, max(period) OVER (PARTITION BY table_id, sheet) AS sheet_last_period
            FROM raw_records
        ),
        ranked AS (
            SELECT
                *,
                row_number() OVER (
                    PARTITION BY table_id, row_no, indicator, period
                    ORDER BY sheet_last_period DESC, sheet DESC
                ) AS pick
            FROM scored
        )
        SELECT
            table_id, table_number, table_title, section,
            indicator, row_no,
            period, year(period) AS year, freq, value, unit
        FROM ranked
        WHERE pick = 1
        """
    )

    rows: int = con.execute("SELECT count(*) FROM clean").fetchone()[0]

    history = meta.dataset_row_history(dataset_id)
    verdict = check_row_count_sane(rows, history)
    meta.record_validation(
        None,
        None,
        verdict.check_name,
        verdict.passed,
        rows_total=rows,
        detail={**verdict.detail, "dataset_id": dataset_id, "skipped_tables": len(skipped)},
    )
    if not verdict:
        con.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"quality gate failed for {dataset_id}: {verdict.detail}")

    con.execute(
        """
        COPY clean TO ? (FORMAT PARQUET, COMPRESSION ZSTD,
                         PARTITION_BY (year), OVERWRITE_OR_IGNORE)
        """,
        [str(tmp_dir)],
    )
    con.close()

    old_dir = final_dir.with_name(final_dir.name + ".old")
    shutil.rmtree(old_dir, ignore_errors=True)
    if final_dir.exists():
        final_dir.rename(old_dir)
    tmp_dir.rename(final_dir)
    shutil.rmtree(old_dir, ignore_errors=True)

    meta.record_dataset(
        dataset_id=dataset_id,
        source_id="seki",
        nas_path=str(final_dir.relative_to(settings.nas_root)),
        row_count=rows,
        partition_keys=["year"],
    )
    log.info("transform.complete", dataset_id=dataset_id, rows=rows, skipped=len(skipped))
    return {"dataset_id": dataset_id, "rows": rows, "path": str(final_dir), "skipped": skipped}


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
        "table_id",
        "table_number",
        "table_title",
        "section",
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
