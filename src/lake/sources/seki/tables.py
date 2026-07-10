"""Pure functions: one SEKI .xls -> long-format observations. No network, no disk.

Written against the real workbooks (fetched 2026-07-10), not against a guess.
Every SEKI table shares one geometry, whatever its subject:

      row k     |          | 2010 |      |      |   <- year, merged across periods
      row k+1   | KETERANGAN| Jan  | Feb  | Mar  |   <- period, one per column
      row k+2   | Uang Beredar Luas | 2073859.77 | ...
                  ^ label column      ^ values

Four things vary and are therefore detected, never assumed:

  * the header's row index (3 to 7 in the wild),
  * the label column,
  * the frequency — monthly, quarterly, or annual,
  * how the period is written.

That last one is the trap. Roughly half the tables spell periods as text (`Jan`,
`Q1`); the other half store them as real Excel dates, which arrive as bare floats
(`36892.0` is 2001-01-01). A parser that only reads cell *values* sees a number,
finds no period row, falls through to the annual branch, and silently emits one
wrong observation per column. So this module reads cell *types* as well, and a
date cell in a header row is a period that carries its own year.

The marker word is never used to find the header: it is `KETERANGAN` in most
tables but `URAIAN`, `LAPANGAN USAHA`, `KELOMPOK NEGARA`, or `KELOMPOK BANK &
LAPANGAN USAHA` in others. Anchoring on the periods works for all of them, and
keeps working when Bank Indonesia adds a table.

Needs the `transform` extra (xlrd). The scraper never imports this module, so
`lake scrape seki` runs without it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, NamedTuple

from lake.core.logging import get_logger

log = get_logger(__name__)

#: Indonesian and English month abbreviations, matched on the first three letters.
_MONTHS: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6,
    "jul": 7, "agt": 8, "agu": 8, "ags": 8, "aug": 8, "sep": 9, "okt": 10,
    "oct": 10, "nov": 11, "des": 12, "dec": 12,
}  # fmt: skip

_QUARTERS = {f"q{n}": n for n in (1, 2, 3, 4)}

#: A year in a SEKI header. Anything outside is a stray number, not a year.
_YEAR_MIN, _YEAR_MAX = 1950, 2100

#: How far down to look for the header before giving up.
_HEADER_SEARCH_ROWS = 16
#: A header row needs at least this many period cells to be one.
_MIN_PERIOD_CELLS = 2

_NON_ALNUM = re.compile(r"[^a-z0-9]")

Freq = Literal["monthly", "quarterly", "annual"]


class Cell(NamedTuple):
    """A worksheet cell, with the type information .xls actually carries.

    `is_date` is the whole reason this type exists: Excel stores a date as a
    float, and only the cell's format distinguishes 36892.0-the-date from
    36892.0-the-measurement.
    """

    value: Any
    is_date: bool = False


class Period(NamedTuple):
    freq: Freq
    number: int | None  # 1..12 monthly, 1..4 quarterly, None annual
    year: int | None  # set only when the cell is a real date


@dataclass(frozen=True, slots=True)
class Header:
    header_row: int
    label_col: int
    #: column index -> (year, period number or None)
    columns: dict[int, tuple[int, int | None]]
    freq: Freq


def _norm(value: Any) -> str:
    return _NON_ALNUM.sub("", str(value).strip().lower())


def _as_year(cell: Cell) -> int | None:
    """Years arrive as floats (`2010.0`) because .xls has one numeric type."""
    value = cell.value
    if cell.is_date or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        year = int(value)
        return year if _YEAR_MIN <= year <= _YEAR_MAX and year == value else None
    match = re.fullmatch(r"((?:19|20)\d{2})(?:\s*\D.*)?", str(value).strip())
    return int(match.group(1)) if match else None


def _as_period(cell: Cell, *, datemode: int = 0) -> Period | None:
    """A header cell -> the period it labels, or None.

    A date cell is unambiguous and brings its own year. A text cell names a month
    or a quarter and inherits the year from the row above.
    """
    if cell.is_date:
        stamp = _date_from_serial(cell.value, datemode)
        if stamp is None:
            return None
        # SEKI date headers are always period starts; the day is noise.
        return Period("monthly", stamp.month, stamp.year)

    key = _norm(cell.value)
    if not key:
        return None
    if key in _QUARTERS:
        return Period("quarterly", _QUARTERS[key], None)
    # "tw1" / "trw1" are Bank Indonesia's Indonesian quarter labels
    if match := re.fullmatch(r"t(?:r)?w([1-4])", key):
        return Period("quarterly", int(match.group(1)), None)
    if month := _MONTHS.get(key[:3]):
        return Period("monthly", month, None)
    return None


def _date_from_serial(value: Any, datemode: int) -> date | None:
    """An Excel serial -> a date, but only one a statistics bureau could mean.

    Some SEKI header cells are date-*formatted* while holding a small counter
    (`1.0`, `2.0`). Excel renders those as 1900-01-01, and taking them at face
    value stamps real measurements with an impossible period. Anything outside
    the plausible window is a formatting artifact, not a date.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        import xlrd

        stamp = xlrd.xldate_as_datetime(value, datemode).date()
    except Exception:
        return None
    return stamp if _YEAR_MIN <= stamp.year <= _YEAR_MAX else None


def _period_start(year: int, freq: Freq, number: int | None) -> date:
    """Stamp every observation with the first day of the period it covers, so a
    month, a quarter, and a year sort and join on one comparable column."""
    if freq == "monthly":
        return date(year, number or 1, 1)
    if freq == "quarterly":
        return date(year, (number or 1) * 3 - 2, 1)
    return date(year, 1, 1)


def _label_column(rows: list[list[Cell]], header_row: int, first_value_col: int) -> int | None:
    """The label column holds the indicator names.

    Found by looking *down* the column at the data rows rather than along the
    header: some sheets carry fiscal-year columns ("1984/85") to the left of the
    real periods, and those are data headers, not labels.
    """
    best: tuple[int, int] | None = None  # (text_rows, col)
    for col in range(min(first_value_col, 8)):
        text_rows = sum(
            1
            for row in rows[header_row + 1 : header_row + 25]
            if col < len(row)
            and isinstance(row[col].value, str)
            and row[col].value.strip()
            and not row[col].is_date
        )
        if text_rows >= 3 and (best is None or text_rows > best[0]):
            best = (text_rows, col)
    return best[1] if best else None


def find_header(rows: list[list[Cell]], *, datemode: int = 0) -> Header | None:
    """Locate the header block, or None when the sheet has no recognisable one.

    None is a real answer: several SEKI sheets are cover pages, notes, or blank.
    Inventing a header for them would fabricate observations.
    """
    for index, row in enumerate(rows[:_HEADER_SEARCH_ROWS]):
        periods = {
            col: period
            for col, cell in enumerate(row)
            if (period := _as_period(cell, datemode=datemode))
        }
        if len(periods) < _MIN_PERIOD_CELLS:
            continue
        header = _build_header(rows, index, periods)
        if header is not None:
            return header

    # No period row anywhere: the table may still be annual, years in the header.
    for index, row in enumerate(rows[:_HEADER_SEARCH_ROWS]):
        years = {col: year for col, cell in enumerate(row) if (year := _as_year(cell))}
        if len(years) < _MIN_PERIOD_CELLS:
            continue
        label_col = _label_column(rows, index, min(years))
        if label_col is None:
            continue
        return Header(
            header_row=index,
            label_col=label_col,
            columns={c: (y, None) for c, y in years.items()},
            freq="annual",
        )
    return None


def _build_header(
    rows: list[list[Cell]], header_row: int, periods: dict[int, Period]
) -> Header | None:
    freq: Freq = periods[min(periods)].freq
    columns: dict[int, tuple[int, int | None]] = {}

    # Date headers carry their own year, so no year row is needed at all. A row
    # that is mostly dates is a date row; the odd text cell in it is a stray.
    dated = {c: p for c, p in periods.items() if p.year is not None}
    if len(dated) >= _MIN_PERIOD_CELLS and len(dated) >= len(periods) // 2:
        columns = {c: (p.year, p.number) for c, p in dated.items() if p.year is not None}
        freq = dated[min(dated)].freq
    else:
        year_row = next(
            (
                r
                for r in range(header_row - 1, max(header_row - 4, -1), -1)
                if any(_as_year(cell) for cell in rows[r])
            ),
            None,
        )
        if year_row is None:
            return None
        columns = _map_year_blocks(rows[year_row], periods)

    if not columns:
        return None

    label_col = _label_column(rows, header_row, min(columns))
    if label_col is None:
        return None

    return Header(header_row=header_row, label_col=label_col, columns=columns, freq=freq)


def _map_year_blocks(
    year_row: list[Cell], periods: dict[int, Period]
) -> dict[int, tuple[int, int | None]]:
    """Assign a year to every period column, one year-block at a time.

    Forward-filling the year row is not enough. A wide sheet lays several years
    side by side, and Bank Indonesia writes the year label wherever it likes
    inside the block — above January in one block, above December in the next:

        c172 year=2023  Jan | c173 .. Feb | ... | c183 .. Dec
        c185 year=      Jan | c186 .. Feb | ... | c196 year=2024  Dec

    Carrying 2023 rightwards therefore stamps the 2024 block as 2023, and two
    columns collide on the same (year, month). The period sequence is the honest
    boundary: a month that is not greater than the one before it starts a new
    year. Each block then takes the year label found anywhere inside it, offset
    by that label's own position.
    """
    ordered = sorted(periods)
    blocks: list[list[int]] = []
    previous: int | None = None
    for col in ordered:
        number = periods[col].number
        wrapped = previous is not None and number is not None and number <= previous
        if wrapped or not blocks:
            blocks.append([])
        blocks[-1].append(col)
        previous = number

    columns: dict[int, tuple[int, int | None]] = {}
    anchor: int | None = None  # the year of the block we last resolved

    for block in blocks:
        year: int | None = None
        for col in block:
            if col < len(year_row) and (found := _as_year(year_row[col])):
                year = found
                break
        if year is None and anchor is not None:
            year = anchor + 1  # unlabelled block: the next year along
        if year is None:
            continue
        anchor = year
        for col in block:
            columns[col] = (year, periods[col].number)

    return columns


def extract_unit(rows: list[list[Cell]]) -> str | None:
    """SEKI states the unit in a parenthesised banner line: `(Miliar Rp)`."""
    for row in rows[:6]:
        for cell in row[:3]:
            text = str(cell.value).strip()
            if text.startswith("(") and text.endswith(")") and 2 < len(text) <= 60:
                return text[1:-1].strip()
    return None


class AmbiguousSheet(ValueError):
    """The sheet parsed, but two observations claim the same series and period.

    That means the layout was misread — SEKI's oldest sheets use an indented
    outline (`1.` / `1.1.` / `a.` / `= Makanan`) rather than the numbered grid
    every other sheet uses, and a single label column cannot identify a series
    in it. Emitting both values would silently double-count; picking one would
    silently discard real data. Refusing is the only honest option.
    """


def parse_sheet(
    rows: list[list[Cell]], *, table_id: str, sheet_name: str, datemode: int = 0
) -> list[dict[str, Any]]:
    """One sheet -> long-format observations.

    Raises AmbiguousSheet when the result is not a function of its key: the
    caller decides whether to skip the sheet or fail the run.
    """
    header = find_header(rows, datemode=datemode)
    if header is None:
        return []

    unit = extract_unit(rows)
    records: list[dict[str, Any]] = []

    for row in rows[header.header_row + 1 :]:
        if header.label_col >= len(row):
            continue
        label = row[header.label_col].value
        if not isinstance(label, str) or not label.strip():
            continue
        indicator = " ".join(label.split())
        # SEKI numbers its own rows in column 0, and that number is the only
        # stable identity a series has: an indicator name such as "Pinjaman yang
        # Diberikan" recurs four times in one sheet under different parents, so
        # (indicator, period) alone would collide and silently double-count.
        row_no = _row_number(row)

        for col, (year, number) in header.columns.items():
            if col >= len(row):
                continue
            cell = row[col]
            value = cell.value
            # A blank cell is a missing observation, never a zero. A month with
            # no survey is not a month of zero inflation.
            if value is None or value == "" or isinstance(value, bool):
                continue
            # Footnote markers, dashes, and stray dates are not measurements.
            if isinstance(value, str) or cell.is_date:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue

            records.append(
                {
                    "table_id": table_id,
                    "sheet": sheet_name,
                    "row_no": row_no,
                    "indicator": indicator,
                    "period": _period_start(year, header.freq, number),
                    "freq": header.freq,
                    "value": numeric,
                    "unit": unit,
                }
            )

    _assert_one_value_per_series(records, sheet_name)
    return records


def _assert_one_value_per_series(records: list[dict[str, Any]], sheet_name: str) -> None:
    seen: dict[tuple[Any, ...], float] = {}
    for record in records:
        key = (record["row_no"], record["indicator"], record["period"])
        previous = seen.get(key)
        if previous is not None and previous != record["value"]:
            raise AmbiguousSheet(
                f"{sheet_name}: {record['indicator']!r} at {record['period']} "
                f"has two values ({previous} and {record['value']})"
            )
        seen[key] = record["value"]


def _row_number(row: list[Cell]) -> int | None:
    """Bank Indonesia's own row number, from the leftmost numeric cell."""
    for cell in row[:2]:
        value = cell.value
        if isinstance(value, bool) or cell.is_date:
            continue
        if isinstance(value, int | float) and value == int(value) and 0 < value < 10_000:
            return int(value)
    return None


def _sheet_cells(sheet: Any) -> list[list[Cell]]:
    """Read a worksheet into typed cells. XL_CELL_DATE is 3."""
    return [
        [Cell(sheet.cell_value(r, c), sheet.cell_type(r, c) == 3) for c in range(sheet.ncols)]
        for r in range(sheet.nrows)
    ]


def parse(raw: bytes, *, table_id: str) -> list[dict[str, Any]]:
    """Every sheet of one SEKI workbook, flattened.

    Sheets are year-ranged (`Th 1985-1992`, `I.1`), so all of them together are
    the full history. A sheet with no recognisable header contributes nothing
    rather than nonsense.
    """
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("seki parsing needs: uv sync --extra transform") from exc

    book = xlrd.open_workbook(file_contents=raw)
    try:
        records: list[dict[str, Any]] = []
        for sheet in book.sheets():
            if sheet.nrows == 0 or sheet.ncols == 0:
                continue
            try:
                records.extend(
                    parse_sheet(
                        _sheet_cells(sheet),
                        table_id=table_id,
                        sheet_name=sheet.name,
                        datemode=book.datemode,
                    )
                )
            except AmbiguousSheet as exc:
                # A sheet we cannot read is data we do not have. Say so loudly and
                # keep the sheets we do understand, rather than guess at this one.
                log.warning(
                    "seki.sheet_skipped", table_id=table_id, sheet=sheet.name, reason=str(exc)
                )
        return records
    finally:
        book.release_resources()
