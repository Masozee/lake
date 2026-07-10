"""The SEKI transform's pure helpers.

`transform_seki` itself needs Postgres and a NAS mount, so what is tested here is
the part that silently corrupts a dataset when it is wrong: the join key between
a downloaded file and the catalogue that names it.
"""

from __future__ import annotations

import json

import pytest

from lake.transform.runner import _seki_catalogue, _seki_table_id


class TestTableId:
    """`{source}_{stamp}_{table_id}.xls` — and table ids contain underscores."""

    def test_a_simple_table_id_round_trips(self):
        assert _seki_table_id("seki_20260701_TABEL9_7") == "TABEL9_7"

    def test_an_underscored_table_id_is_not_truncated(self):
        """Splitting from the right takes `1` off `TABEL1_1_1` and breaks the
        join to the catalogue for every table on the page."""
        assert _seki_table_id("seki_20260701_TABEL1_1_1") == "TABEL1_1_1"
        assert _seki_table_id("seki_20260701_TABEL1_1") == "TABEL1_1"

    def test_every_real_table_id_survives_the_round_trip(self):
        for table_id in ("TABEL1_1", "TABEL1_1_1", "TABEL5_11", "TABEL9_9", "TABEL8_1"):
            assert _seki_table_id(f"seki_20260701_{table_id}") == table_id

    def test_an_unexpected_name_is_returned_whole_rather_than_mangled(self):
        assert _seki_table_id("weird") == "weird"


class TestCatalogue:
    def test_reads_the_catalogue_the_scraper_stored(self, tmp_path):
        entries = [
            {"table_id": "TABEL1_1", "number": "I.1.", "title": "Uang Beredar", "section": "I."},
            {
                "table_id": "TABEL1_1_1",
                "number": "I.1.A.",
                "title": "Reklasifikasi",
                "section": "I.",
            },
        ]
        (tmp_path / "seki_20260701_catalogue.json").write_text(
            json.dumps(entries), encoding="utf-8"
        )

        catalogue = _seki_catalogue(tmp_path)
        assert set(catalogue) == {"TABEL1_1", "TABEL1_1_1"}
        assert catalogue["TABEL1_1"]["title"] == "Uang Beredar"

    def test_a_run_without_a_catalogue_yields_no_titles_rather_than_failing(self, tmp_path):
        """An old run predating the catalogue must still transform; the rows
        simply carry no section, which is honest."""
        assert _seki_catalogue(tmp_path) == {}

    def test_the_newest_catalogue_wins(self, tmp_path):
        (tmp_path / "seki_20260601_catalogue.json").write_text(
            json.dumps([{"table_id": "T", "title": "old"}]), encoding="utf-8"
        )
        (tmp_path / "seki_20260701_catalogue.json").write_text(
            json.dumps([{"table_id": "T", "title": "new"}]), encoding="utf-8"
        )
        assert _seki_catalogue(tmp_path)["T"]["title"] == "new"


pytest.importorskip("duckdb", reason="the dedupe query needs the transform extra")


def test_overlapping_sheets_deduplicate_to_the_revised_figure():
    """SEKI ships a current sheet plus year-range history sheets, and the same
    observation appears on both. Where they disagree the later revision wins —
    the one on the sheet whose data runs furthest forward.
    """
    import datetime as dt

    import duckdb
    import pyarrow as pa

    rows = [
        # the history sheet stops in 2020 and carries a stale figure
        ("T", "Th 2010-2020", 1, "M2", dt.date(2020, 1, 1), "monthly", 100.0, "Rp"),
        # the current sheet runs to 2026 and carries the revision
        ("T", "I.1", 1, "M2", dt.date(2020, 1, 1), "monthly", 111.0, "Rp"),
        ("T", "I.1", 1, "M2", dt.date(2026, 1, 1), "monthly", 999.0, "Rp"),
    ]
    names = ("table_id", "sheet", "row_no", "indicator", "period", "freq", "value", "unit")
    table = pa.table({name: [row[i] for row in rows] for i, name in enumerate(names)})

    con = duckdb.connect()
    con.register("raw_records", table)
    result = con.execute(
        """
        WITH scored AS (
            SELECT *, max(period) OVER (PARTITION BY table_id, sheet) AS sheet_last_period
            FROM raw_records
        ),
        ranked AS (
            SELECT *, row_number() OVER (
                PARTITION BY table_id, row_no, indicator, period
                ORDER BY sheet_last_period DESC, sheet DESC
            ) AS pick
            FROM scored
        )
        SELECT period, value FROM ranked WHERE pick = 1 ORDER BY period
        """
    ).fetchall()

    assert result == [(dt.date(2020, 1, 1), 111.0), (dt.date(2026, 1, 1), 999.0)]
