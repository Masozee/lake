"""The serving replica: a DuckDB file the API reads and nothing else writes.

Two connections exist in this system, and they are never the same object.

    builder()  read-write, no network callers, run by `lake serve build`.
               Reads processed/*.parquet off the NAS and materialises tables.

    serving()  read_only=True, enable_external_access=False.
               Every HTTP request and every AI tool call uses this. It physically
               cannot write, cannot read a file off disk, cannot ATTACH another
               database, cannot INSTALL an extension, and cannot re-enable any of
               that at runtime.

The last point is the load-bearing one. Verified against DuckDB 1.5:

    >>> c = duckdb.connect(db, read_only=True, config={'enable_external_access': False})
    >>> c.execute("SET enable_external_access=true")
    InvalidInputException: Cannot enable external access while database is running

So the lockdown is not a policy the AI could talk its way past. It is a property
of the process. `lake.api.sql_guard` exists to produce good error messages and to
survive someone later loosening this file — not because this file needs help.

Why a replica at all, rather than querying the Parquet directly? Because
`enable_external_access=False` blocks `read_parquet()` too. Choosing to keep the
filesystem lock means the data must already be inside the database. That trade is
the right way round: a serving layer that cannot touch the filesystem cannot be
tricked into reading `/etc/passwd` or the raw/ tree.
"""

from __future__ import annotations

import base64
import datetime
import decimal
import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

from lake.core.logging import get_logger
from lake.settings import get_settings

log = get_logger(__name__)

#: Namespace for every table exposed to readers. Nothing outside it is queryable.
SCHEMA = "lake"

#: Per-query ceilings. A reader cannot raise them: SET is rejected by the guard,
#: and by the engine.
DEFAULT_ROW_LIMIT = 10_000
MAX_ROW_LIMIT = 100_000
DEFAULT_TIMEOUT_SECONDS = 20.0
SERVING_MEMORY_LIMIT = "1GB"
SERVING_THREADS = 2


class QueryTimeout(TimeoutError):
    """The query ran past its deadline and was interrupted."""


def replica_path() -> Path:
    """The serving database. Lives on the NUC's local SSD, never on the NAS.

    DuckDB over NFS has the same locking problems as SQLite over NFS. The replica
    is derived data — if it is lost, rebuild it from processed/.

    The filename is `serving.duckdb`, not `lake.duckdb`, and that matters: DuckDB
    derives the catalog name from the filename, so a database called `lake` holding
    a schema called `lake` makes every `lake.table` reference ambiguous and every
    query fails with a BinderException.
    """
    settings = get_settings()
    return settings.staging_root.parent / "serving" / "serving.duckdb"


# -- builder: the only writer -------------------------------------------------


@contextmanager
def builder(path: Path | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-write connection with filesystem access. Never exposed over HTTP."""
    target = path or replica_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(target))
    try:
        con.execute(f"SET memory_limit = '{SERVING_MEMORY_LIMIT}'")
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        yield con
    finally:
        con.close()


def build_replica(path: Path | None = None) -> dict[str, int]:
    """Materialise processed/*.parquet into the serving replica.

    Builds into a temporary file and swaps it in, so a reader never observes a
    half-built database, and a failed build leaves the previous replica serving.
    """
    settings = get_settings()
    final = path or replica_path()
    staging = final.with_suffix(".building")
    staging.unlink(missing_ok=True)

    processed = settings.processed_root
    if not processed.is_dir():
        raise FileNotFoundError(f"no processed layer at {processed} — run `lake transform` first")

    counts: dict[str, int] = {}
    with builder(staging) as con:
        for dataset_dir in sorted(processed.glob("dataset=*")):
            dataset_id = dataset_dir.name.split("=", 1)[1]
            if not _is_safe_identifier(dataset_id):
                log.warning("replica.skipped_unsafe_name", dataset_id=dataset_id)
                continue

            pattern = str(dataset_dir / "**" / "*.parquet")
            # The builder HAS filesystem access; this is the one place it is used.
            #
            # `EXCLUDE (dataset)`: hive partitioning turns the `dataset=` directory
            # name into a column, which then says "observations" on all 987,860 rows
            # of the table already called observations. The real dataset each row
            # belongs to is `dataset_id`, written by the transform.
            con.execute(
                f'CREATE OR REPLACE TABLE {SCHEMA}."{dataset_id}" AS '
                f"SELECT * EXCLUDE (dataset) FROM read_parquet(?, hive_partitioning = true)",
                [pattern],
            )
            rows = scalar(con.execute(f'SELECT count(*) FROM {SCHEMA}."{dataset_id}"'))
            counts[dataset_id] = rows
            log.info("replica.table_built", dataset_id=dataset_id, rows=rows)

        if not counts:
            raise FileNotFoundError(f"no dataset=* directories under {processed}")

        con.execute("CHECKPOINT")

    final.unlink(missing_ok=True)
    shutil.move(str(staging), str(final))
    log.info("replica.built", path=str(final), tables=len(counts), rows=sum(counts.values()))
    return counts


def _is_safe_identifier(name: str) -> bool:
    """Table names come from directory names on the NAS. Do not trust them."""
    return name.replace("_", "").isalnum() and not name[0].isdigit()


def scalar(cursor: duckdb.DuckDBPyConnection) -> Any:
    """First column of the first row. A count(*) always returns one row."""
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("expected a scalar result, got none")
    return row[0]


# -- serving: read-only, no filesystem ----------------------------------------

_serving_lock = threading.Lock()
_serving: duckdb.DuckDBPyConnection | None = None


def serving() -> duckdb.DuckDBPyConnection:
    """The single read-only connection. Cursors are taken from it per request.

    DuckDB connections are thread-safe for cursor creation; a cursor is not shared
    across threads. One process-wide connection, one cursor per query.
    """
    global _serving
    with _serving_lock:
        if _serving is None:
            path = replica_path()
            if not path.is_file():
                raise FileNotFoundError(
                    f"serving replica missing at {path} — run `lake serve build`"
                )
            _serving = duckdb.connect(
                str(path),
                read_only=True,
                config={
                    # Blocks read_csv/read_parquet/ATTACH/INSTALL/COPY TO, and
                    # cannot be turned back on while the database is open.
                    "enable_external_access": False,
                    "memory_limit": SERVING_MEMORY_LIMIT,
                    "threads": SERVING_THREADS,
                    "autoinstall_known_extensions": False,
                    "autoload_known_extensions": False,
                },
            )
            # A local (post-connect) setting, not a global connect-time one.
            # DuckDB's stdout progress bar is noise in a server and corruption in
            # a piped response. Callers cannot undo this: sql_guard rejects SET.
            _serving.execute("SET enable_progress_bar = false")
            log.info("serving.opened", path=str(path))
    return _serving


def close_serving() -> None:
    global _serving
    with _serving_lock:
        if _serving is not None:
            _serving.close()
            _serving = None


@contextmanager
def _deadline(cursor: duckdb.DuckDBPyConnection, seconds: float) -> Iterator[None]:
    """Interrupt a runaway query.

    DuckDB has no per-query timeout setting, but `interrupt()` works from another
    thread and raises InterruptException in the executing one. Verified.
    """
    fired = threading.Event()

    def fire() -> None:
        if not fired.wait(seconds):
            cursor.interrupt()

    watchdog = threading.Thread(target=fire, daemon=True)
    watchdog.start()
    try:
        yield
    finally:
        fired.set()
        watchdog.join(timeout=1.0)


@contextmanager
def read_cursor(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Iterator[duckdb.DuckDBPyConnection]:
    """A time-bounded cursor on the read-only replica."""
    cursor = serving().cursor()
    try:
        with _deadline(cursor, timeout):
            yield cursor
    except duckdb.InterruptException as exc:
        raise QueryTimeout(f"query exceeded {timeout:.0f}s and was cancelled") from exc
    finally:
        cursor.close()


def jsonable(value: Any) -> Any:
    """Coerce a DuckDB value into something `json.dumps` accepts.

    DuckDB hands back date, datetime, Decimal, UUID, bytes, and timedelta. A
    default=str fallback would silently stringify numbers; being explicit keeps
    floats as floats, which is what a charting frontend needs.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return str(value)


def run_query(
    sql: str,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute a pre-validated SELECT and return rows plus a truncation flag.

    The caller MUST have passed `sql` through `sql_guard.validate()` first. This
    function does not re-validate; it enforces the row and time ceilings.
    """
    limit = max(1, min(limit, MAX_ROW_LIMIT))
    started = time.perf_counter()

    with read_cursor(timeout) as cursor:
        result = cursor.execute(sql)
        columns = [d[0] for d in (result.description or [])]
        # Fetch one extra row: if it exists, the result was truncated.
        rows = result.fetchmany(limit + 1)

    truncated = len(rows) > limit
    rows = rows[:limit]

    return {
        "columns": columns,
        "rows": [[jsonable(v) for v in row] for row in rows],
        "row_count": len(rows),
        "truncated": truncated,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def stream_batches(
    sql: str,
    params: list[Any] | None = None,
    *,
    batch_rows: int = 8192,
    max_rows: int = MAX_ROW_LIMIT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Iterator[tuple[list[str], list[list[Any]]]]:
    """Yield (columns, rows) chunks without materialising the whole result.

    This is what makes 'stream the data' true rather than aspirational: a client
    asking for a million rows never causes a million rows to exist in this
    process's memory at once.

    `params` are bound, never interpolated — an export carries the same filters as
    the page it was downloaded from, and those come from a caller.

    The deadline covers the entire stream, not just the initial execute — a query
    that produces its first batch quickly and then stalls is still cancelled.
    """
    cursor = serving().cursor()
    try:
        with _deadline(cursor, timeout):
            reader = cursor.execute(sql, params or []).to_arrow_reader(batch_rows)
            columns: list[str] | None = None
            emitted = 0

            for batch in reader:
                if columns is None:
                    columns = list(batch.schema.names)

                rows = [
                    [jsonable(v) for v in row]
                    for row in zip(*[col.to_pylist() for col in batch.columns], strict=True)
                ]
                if emitted + len(rows) > max_rows:
                    rows = rows[: max_rows - emitted]

                if rows:
                    emitted += len(rows)
                    yield columns, rows
                if emitted >= max_rows:
                    return

            if columns is None:  # a query that returned no batches at all
                yield [d[0] for d in (cursor.description or [])], []
    except duckdb.InterruptException as exc:
        raise QueryTimeout(f"query exceeded {timeout:.0f}s and was cancelled") from exc
    finally:
        cursor.close()
