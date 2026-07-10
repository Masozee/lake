"""CSV and Excel export, against the real read-only engine.

The researcher's real ask is a spreadsheet. These prove it streams, carries a BOM
(so Excel opens accented text right), and cannot be used to escape read-only.
"""

from __future__ import annotations

import csv
import io

import pytest

pytestmark = pytest.mark.integration


def test_csv_has_a_bom_and_header(replica):
    from lake.api import export

    data = b"".join(export.stream_csv("SELECT country_iso3, year FROM lake.gdp_annual LIMIT 3"))
    assert data.startswith(b"\xef\xbb\xbf")  # BOM for Excel

    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["country_iso3", "year"]
    assert len(rows) == 4  # header + 3


def test_csv_preserves_nulls_as_empty(replica):
    from lake.api import export

    data = b"".join(export.stream_csv("SELECT country_iso3, gdp_usd FROM lake.gdp_annual"))
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    deu = next(r for r in rows if r[0] == "DEU")
    assert deu[1] == ""  # NULL gdp -> empty cell, not "None"


def test_xlsx_is_a_valid_workbook(replica):
    from openpyxl import load_workbook

    from lake.api import export

    content = export.build_xlsx("SELECT country_iso3, year, gdp_usd FROM lake.gdp_annual LIMIT 5")
    assert content[:2] == b"PK"  # xlsx is a zip

    wb = load_workbook(io.BytesIO(content))
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header == ["country_iso3", "year", "gdp_usd"]
    assert ws.max_row == 6  # header + 5


def test_export_cannot_run_a_write(replica):
    """Even bypassing the route validator, the engine refuses a write."""
    from lake.api import export

    with pytest.raises(Exception):  # noqa: B017 — InvalidInputException from DuckDB
        list(export.stream_csv("DELETE FROM lake.gdp_annual"))


def test_safe_filename_strips_dangerous_characters():
    from lake.api.export import safe_filename

    assert safe_filename("gdp_annual", "csv") == "gdp_annual.csv"
    assert safe_filename("../../etc/passwd", "csv") == "etc_passwd.csv"
    assert safe_filename("", "xlsx") == "export.xlsx"


# -- the routes ---------------------------------------------------------------


def test_export_table_csv_route(client):
    r = client.get("/api/tables/gdp_annual/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="gdp_annual.csv"' in r.headers["content-disposition"]
    assert r.content.startswith(b"\xef\xbb\xbf")


def test_export_table_xlsx_route(client):
    r = client.get("/api/tables/gdp_annual/export.xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert r.content[:2] == b"PK"


def test_export_query_csv_route(client):
    r = client.get(
        "/api/query/export.csv",
        params={"sql": "SELECT country_iso3 FROM lake.gdp_annual LIMIT 2", "filename": "custom"},
    )
    assert r.status_code == 200
    assert 'filename="custom.csv"' in r.headers["content-disposition"]


def test_export_of_a_write_is_rejected_at_the_route(client):
    r = client.get("/api/query/export.csv", params={"sql": "DELETE FROM lake.gdp_annual"})
    assert r.status_code == 422


def test_export_of_a_file_read_is_rejected(client):
    r = client.get("/api/query/export.csv", params={"sql": "SELECT * FROM read_csv('/etc/passwd')"})
    assert r.status_code == 422


def test_export_unknown_table_is_404(client):
    assert client.get("/api/tables/nope/export.csv").status_code == 404
