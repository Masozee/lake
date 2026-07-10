# bps_inflation

**Schedule:** monthly, 3rd at 07:00 UTC ±1h · **Owner:** research · **SLA:** 800h (~33d)

Monthly consumer price inflation by region, published as a single Excel workbook.

## Upstream

```
https://example.bps.go.id/inflation.xlsx
```

Fetched with a conditional GET: we send `If-None-Match` with the ETag from our
last successful run. A `304 Not Modified` marks the run `skipped_unchanged` — not
a failure. Upstream simply published nothing new.

## Why the 3rd, not the 1st

Statistical agencies publish late. A timer that fires on the 1st records a
failure every single month, trains everyone to ignore the alert, and then misses
the real outage. The `logical_date` is still the **first of the month the data
describes**, so `2026-07-01` means "July's figures", whenever they arrived.

## Status

⚠️ **The parser is a template.** `parser.py` guesses the column layout. Before you
build the processed layer:

1. `lake scrape bps_inflation` — real bytes land in `raw/`
2. Copy one file to `tests/fixtures/bps_inflation_sample.xlsx`
3. Open it, fix `REGION_COL` and `HEADER_ROW`, write a test against the fixture
4. Only then wire it into `transform.py`

Nothing imports the parser until you do, so an unverified parser cannot corrupt
`raw/`. The schema (`schema.py`) is already correct regardless of layout.

## Quirks

* **Blank cells are missing observations, not zeros.** A region with no survey
  that month is not a region with 0% inflation. `parse()` skips them; do not
  "fix" this with `fillna(0)`.
* **Summary rows look exactly like data rows.** `Total`, `Jumlah`, `Nasional`,
  `Indonesia`. `InflationRecord` rejects them by name.
* **The year is not in a column.** It lives in the sheet title or a merged banner
  cell. `infer_year()` regexes it out.
* **An out-of-range figure is a parsing bug, not an economy.** The schema bounds
  `inflation_pct` to `[-50, 100]`. Outside that you have read a raw index as a
  percentage, or the columns are offset by one.

## Files

```
raw/source=bps_inflation/year=2026/month=07/day=01/run=.../
    bps_inflation_20260701_000.xlsx
    bps_inflation_20260701_000.xlsx.meta.json
    _MANIFEST.json
```

The 200-with-an-HTML-error-page failure is caught by the structural gate before
anything is written: `.xlsx` must begin with `PK\x03\x04`.
