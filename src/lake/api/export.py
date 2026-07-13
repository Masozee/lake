"""Streaming CSV and Excel export.

A researcher's real request is usually "give me the spreadsheet," not "let me
write SQL." These functions turn a validated read-only query into a downloadable
file — and they stream, so a large table never sits whole in the server's memory.

CSV is a true generator: one row out per row read. Excel cannot be — the xlsx
format writes a zip at the end — but openpyxl's write_only mode keeps only a small
window of rows in memory, so it scales far past a naive `to_excel()`.

Both go through `engine.stream_batches`, which enforces the same row and time
ceilings as every other read. `COPY ... TO` would be simpler but is blocked at the
engine (external access off) — which is correct: we never let the database write
a file. We build the bytes in Python instead.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from typing import Any

from lake.api import engine

#: Export allows more rows than an interactive query — the point is to take the
#: data away. Still bounded, so one request cannot stream forever.
EXPORT_MAX_ROWS = 1_000_000
EXPORT_TIMEOUT = 120.0


def _cell(value: object) -> object:
    """Excel and CSV both choke on some Python types; normalise them."""
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)


def stream_csv(
    sql: str, params: list[Any] | None = None, *, max_rows: int = EXPORT_MAX_ROWS
) -> Iterator[bytes]:
    """Yield a CSV file as UTF-8 bytes, one flush per batch.

    Prefixed with a UTF-8 BOM so Excel on Windows opens accented text correctly —
    a real papercut for researchers with non-ASCII place names.
    """
    yield b"\xef\xbb\xbf"  # BOM

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    header_written = False

    for columns, rows in engine.stream_batches(
        sql, params, batch_rows=8192, max_rows=max_rows, timeout=EXPORT_TIMEOUT
    ):
        if not header_written:
            writer.writerow(columns)
            header_written = True
        for row in rows:
            writer.writerow([_cell(v) for v in row])

        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)

    if not header_written:  # a query that returned no rows still gets its header
        # stream_batches yields ([...], []) for an empty result, so this is rare,
        # but guard it so the file is never zero bytes.
        yield b""


def build_xlsx(
    sql: str, params: list[Any] | None = None, *, max_rows: int = EXPORT_MAX_ROWS
) -> bytes:
    """Build an .xlsx in memory with openpyxl's streaming writer.

    Returns the whole file — xlsx is a zip finalised at save() and cannot be
    streamed row-by-row over HTTP. write_only mode keeps memory bounded to a
    sliding window, not the whole sheet, so this still handles large exports.
    """
    from openpyxl import Workbook

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title="data")
    header_written = False

    for columns, rows in engine.stream_batches(
        sql, params, batch_rows=8192, max_rows=max_rows, timeout=EXPORT_TIMEOUT
    ):
        if not header_written:
            sheet.append(list(columns))
            header_written = True
        for row in rows:
            sheet.append([_cell(v) for v in row])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def safe_filename(stem: str, extension: str) -> str:
    """A download filename that won't break Content-Disposition or a filesystem."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem).strip("_")
    return f"{cleaned or 'export'}.{extension}"
