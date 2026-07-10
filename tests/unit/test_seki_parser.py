"""SEKI parsers, against real captured bytes from bi.go.id.

Every test here encodes a mistake the parser made against the real workbooks
before it stopped making it. Bank Indonesia's .xls files are legacy BIFF with an
inconsistent header layout, so a parser that only ever saw a synthetic fixture
would look correct and quietly produce nonsense.

Refresh the fixtures with the real files, never a hand-edited approximation:
    curl -s 'https://www.bi.go.id/SEKI/tabel/TABEL9_7.xls' \\
        > tests/fixtures/seki_TABEL9_7_monthly.xls
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from lake.sources.seki.parser import ALLOWED_HOSTS, extract_tables
from lake.sources.seki.tables import (
    AmbiguousSheet,
    Cell,
    _map_year_blocks,
    find_header,
    parse,
    parse_sheet,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

pytest.importorskip("xlrd", reason="seki parsing needs the transform extra")


# -- the index page -----------------------------------------------------------


@pytest.fixture
def index_bytes() -> bytes:
    return (FIXTURES / "seki_index.html").read_bytes()


BASE_URL = "https://www.bi.go.id/id/statistik/ekonomi-keuangan/seki/Default.aspx"


def test_extract_tables_reads_number_title_and_section(index_bytes: bytes):
    tables = extract_tables(index_bytes, BASE_URL)
    assert len(tables) == 5

    first = tables[0]
    assert first.table_id == "TABEL1_1"
    assert first.number == "I.1."
    assert first.title.startswith("Uang Beredar dan Faktor-Faktor")
    assert first.section == "I. UANG DAN BANK"
    assert first.url.endswith("/SEKI/tabel/TABEL1_1.xls")


def test_every_row_inherits_the_section_heading_above_it(index_bytes: bytes):
    # The anchors carry an icon, not text, and the heading is a separate <th>.
    # Two independent passes over the markup would lose that relationship.
    tables = extract_tables(index_bytes, BASE_URL)
    assert {t.section for t in tables} == {"I. UANG DAN BANK"}


def test_table_ids_are_unique_and_ordered(index_bytes: bytes):
    tables = extract_tables(index_bytes, BASE_URL)
    ids = [t.table_id for t in tables]
    assert ids == ["TABEL1_1", "TABEL1_1_1", "TABEL1_2", "TABEL1_3", "TABEL1_4"]
    assert len(set(ids)) == len(ids)


def test_limit_stops_early(index_bytes: bytes):
    assert len(extract_tables(index_bytes, BASE_URL, limit=2)) == 2


def test_links_off_bank_indonesia_are_dropped():
    """The index is the one input we do not control, and it decides what we
    fetch next. A rewritten page must not point the scraper at another host."""
    assert "www.bi.go.id" in ALLOWED_HOSTS
    evil = (
        b'<th colspan="4"><b>I. X</b></th>'
        b'<td width="30">I.1.</td><td style="text-align:left;">Title</td>'
        b'<td><a href="https://evil.example/steal.xls"></a></td></tr>'
    )
    assert extract_tables(evil, BASE_URL) == []


def test_rows_without_an_excel_link_are_skipped():
    pdf_only = (
        b'<th colspan="4"><b>I. X</b></th>'
        b'<td width="30">I.1.</td><td style="text-align:left;">Title</td>'
        b'<td><a href="https://www.bi.go.id/x.pdf"></a></td></tr>'
    )
    assert extract_tables(pdf_only, BASE_URL) == []


# -- the workbooks ------------------------------------------------------------


@pytest.fixture
def monthly_bytes() -> bytes:
    return (FIXTURES / "seki_TABEL9_7_monthly.xls").read_bytes()


@pytest.fixture
def annual_bytes() -> bytes:
    return (FIXTURES / "seki_TABEL4_1_annual.xls").read_bytes()


def test_parses_a_real_monthly_table(monthly_bytes: bytes):
    records = parse(monthly_bytes, table_id="TABEL9_7")
    assert records

    # cross-checked against the raw cell: sheet '9.7', row 2 ("3 Bulan" under
    # LIBOR), Jan 2010. The name alone is ambiguous — "3 Bulan" also appears at
    # row 6 under a different parent rate, with a different value. That is
    # exactly why row_no is part of a series' identity.
    hit = [
        r
        for r in records
        if r["row_no"] == 2 and r["period"] == date(2010, 1, 1) and r["sheet"] == "9.7"
    ]
    assert len(hit) == 1
    assert hit[0]["indicator"] == "3 Bulan"
    assert hit[0]["value"] == pytest.approx(0.25)
    assert hit[0]["freq"] == "monthly"
    assert hit[0]["unit"] == "Persen Per Tahun"
    assert hit[0]["table_id"] == "TABEL9_7"

    by_name = [
        r for r in records if r["indicator"] == "3 Bulan" and r["period"] == date(2010, 1, 1)
    ]
    assert len({r["row_no"] for r in by_name}) == len(by_name) > 1


def test_parses_a_real_annual_table(annual_bytes: bytes):
    records = parse(annual_bytes, table_id="TABEL4_1")
    assert records
    assert {r["freq"] for r in records} == {"annual"}

    hit = [
        r
        for r in records
        if r["indicator"] == "Pendapatan Negara dan Hibah" and r["period"] == date(2008, 1, 1)
    ]
    assert hit and hit[0]["value"] == pytest.approx(894990.6)
    # an annual observation is stamped with the first day of its year
    assert all(r["period"].month == 1 and r["period"].day == 1 for r in records)


def test_every_period_is_one_a_statistics_bureau_could_mean(
    monthly_bytes: bytes, annual_bytes: bytes
):
    """Some header cells are date-*formatted* but hold a counter (1.0), which
    Excel renders as 1900-01-01. Taking that at face value stamped real
    measurements with an impossible period."""
    for raw, table_id in ((monthly_bytes, "TABEL9_7"), (annual_bytes, "TABEL4_1")):
        for record in parse(raw, table_id=table_id):
            assert 1950 <= record["period"].year <= 2100, record


def test_a_series_has_one_value_per_period(monthly_bytes: bytes, annual_bytes: bytes):
    """`(indicator, period)` is not an identity: an indicator name such as
    "Pinjaman yang Diberikan" recurs within one sheet under different parents.
    Bank Indonesia's own row number disambiguates them."""
    for raw, table_id in ((monthly_bytes, "TABEL9_7"), (annual_bytes, "TABEL4_1")):
        seen: dict[tuple, float] = {}
        for r in parse(raw, table_id=table_id):
            key = (r["sheet"], r["row_no"], r["indicator"], r["period"])
            if key in seen:
                assert seen[key] == r["value"], f"{key} has two values"
            seen[key] = r["value"]


def test_ambiguous_sheets_are_refused_not_guessed():
    """SEKI's oldest sheets use an indented outline rather than the numbered
    grid. One label column cannot identify a series there, so parse_sheet must
    refuse rather than double-count."""
    # No row numbers (the old outline sheets have none), and the same label
    # repeats with different values — as "= Makanan / Food" does under several
    # parents. The key (row_no=None, "Makanan", Jan) then claims two values.
    rows = _cells(
        ["", "", 2020.0, ""],
        ["", "KETERANGAN", "Jan", "Feb"],
        ["", "Makanan", 1.0, 2.0],
        ["", "Perumahan", 3.0, 4.0],
        ["", "Makanan", 9.0, 8.0],
    )
    with pytest.raises(AmbiguousSheet, match="Makanan"):
        parse_sheet(rows, table_id="T", sheet_name="old")


def test_parse_skips_an_ambiguous_sheet_and_keeps_the_rest(monthly_bytes: bytes):
    """A sheet we cannot read is data we do not have — but it must not discard
    the sheets we can read."""
    records = parse(monthly_bytes, table_id="TABEL9_7")
    assert records  # TABEL9_7's single sheet is unambiguous and survives


# -- header geometry ----------------------------------------------------------


def _cells(*rows: list) -> list[list[Cell]]:
    return [[c if isinstance(c, Cell) else Cell(c) for c in row] for row in rows]


def test_header_is_found_by_periods_not_by_a_marker_word():
    """The marker is KETERANGAN in most tables but URAIAN, LAPANGAN USAHA, or
    KELOMPOK NEGARA in others. Anchoring on the periods works for all."""
    rows = _cells(
        ["", "", "", 2020.0, "", ""],
        ["", "", "KELOMPOK NEGARA", "Q1", "Q2", "Q3"],
        [1.0, "", "Amerika Serikat", 1.6, 2.7, 3.1],
        [2.0, "", "Jepang", 0.5, 0.6, 0.7],
        [3.0, "", "Tiongkok", 6.1, 6.2, 6.3],
    )
    header = find_header(rows)
    assert header is not None
    assert header.freq == "quarterly"
    assert header.label_col == 2


def test_quarterly_periods_map_to_the_first_month_of_the_quarter():
    rows = _cells(
        ["", "", "", 2020.0, "", ""],
        ["", "", "URAIAN", "Q1", "Q2", "Q4"],
        [1.0, "", "Ekspor", 10.0, 20.0, 40.0],
        [2.0, "", "Impor", 1.0, 2.0, 4.0],
        [3.0, "", "Neraca", 9.0, 18.0, 36.0],
    )
    records = parse_sheet(rows, table_id="T", sheet_name="s")
    periods = sorted({r["period"] for r in records})
    assert periods == [date(2020, 1, 1), date(2020, 4, 1), date(2020, 10, 1)]


def test_a_blank_cell_is_a_missing_observation_never_a_zero():
    rows = _cells(
        ["", "", "", 2020.0, ""],
        ["", "", "KETERANGAN", "Jan", "Feb"],
        [1.0, "", "Inflasi", 1.5, ""],
        [2.0, "", "Deflasi", "", 2.5],
        [3.0, "", "Netral", 0.0, 0.0],
    )
    records = parse_sheet(rows, table_id="T", sheet_name="s")
    values = {(r["indicator"], r["period"].month): r["value"] for r in records}
    assert values == {
        ("Inflasi", 1): 1.5,
        ("Deflasi", 2): 2.5,
        ("Netral", 1): 0.0,  # a real zero survives
        ("Netral", 2): 0.0,
    }


def test_footnote_markers_are_not_measurements():
    rows = _cells(
        ["", "", "", 2020.0, ""],
        ["", "", "KETERANGAN", "Jan", "Feb"],
        [1.0, "", "Inflasi", "-", 2.5],
        [2.0, "", "Deflasi", 1.0, "n.a."],
        [3.0, "", "Netral", 3.0, 4.0],
    )
    records = parse_sheet(rows, table_id="T", sheet_name="s")
    assert all(isinstance(r["value"], float) for r in records)
    assert len(records) == 4


# -- the year-block boundary, which is where this parser was most wrong -------


def _period(number: int) -> Cell:
    return Cell(["", "Jan", "Feb", "Mar", "Apr"][number])


def test_year_blocks_split_on_the_month_sequence_restarting():
    """A wide sheet lays years side by side and Bank Indonesia writes the year
    label wherever it likes inside the block — above January in one, above
    December in the next. Forward-filling the year therefore stamps the next
    block with the previous year, and two columns collide on the same month."""
    from lake.sources.seki.tables import Period

    # year row: 2023 sits above the FIRST column; 2024 above the LAST.
    year_row = [Cell(""), Cell(2023.0), Cell(""), Cell(""), Cell(""), Cell(2024.0)]
    periods = {
        1: Period("monthly", 1, None),  # Jan 2023
        2: Period("monthly", 2, None),  # Feb 2023
        3: Period("monthly", 1, None),  # Jan 2024  <- sequence restarts
        4: Period("monthly", 2, None),
        5: Period("monthly", 3, None),
    }
    columns = _map_year_blocks(year_row, periods)
    assert columns[1] == (2023, 1)
    assert columns[2] == (2023, 2)
    assert columns[3] == (2024, 1)
    assert columns[5] == (2024, 3)
    # and no two columns claim the same (year, month)
    assert len(set(columns.values())) == len(columns)


def test_an_unlabelled_year_block_continues_the_sequence():
    from lake.sources.seki.tables import Period

    year_row = [Cell(""), Cell(2001.0), Cell(""), Cell(""), Cell("")]
    periods = {
        1: Period("monthly", 11, None),
        2: Period("monthly", 12, None),
        3: Period("monthly", 1, None),  # new block, no label
        4: Period("monthly", 2, None),
    }
    columns = _map_year_blocks(year_row, periods)
    assert columns[2] == (2001, 12)
    assert columns[3] == (2002, 1)


def test_date_formatted_headers_carry_their_own_year():
    """Roughly half the tables store the period as a real Excel date, which
    arrives as a bare float. Reading only cell values finds no period row, falls
    through to the annual branch, and emits one wrong observation per column."""
    rows = [
        [Cell(""), Cell("KETERANGAN"), Cell(36892.0, True), Cell(36923.0, True)],
        [Cell(1.0), Cell("Uang Primer"), Cell(47.05), Cell(50.19)],
        [Cell(2.0), Cell("Uang Kartal"), Cell(39.22), Cell(42.06)],
        [Cell(3.0), Cell("Giro"), Cell(3.13), Cell(3.47)],
    ]
    header = find_header(rows)
    assert header is not None
    assert header.freq == "monthly"
    assert header.columns[2] == (2001, 1)
    assert header.columns[3] == (2001, 2)


def test_a_date_formatted_counter_is_not_a_date():
    """`1.0` in a date-formatted cell is Excel's 1900-01-01, not a period."""
    from lake.sources.seki.tables import _date_from_serial

    assert _date_from_serial(1.0, 0) is None
    assert _date_from_serial(36892.0, 0) == date(2001, 1, 1)


def test_a_sheet_with_no_recognisable_header_yields_nothing():
    """Cover pages and notes must contribute no observations rather than nonsense."""
    rows = _cells(
        ["Catatan / Notes", "", ""],
        ["Sumber: Bank Indonesia", "", ""],
    )
    assert find_header(rows) is None
    assert parse_sheet(rows, table_id="T", sheet_name="notes") == []
