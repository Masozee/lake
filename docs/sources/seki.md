# seki

**Schedule:** monthly, 3rd at 07:00 UTC ±1h · **Owner:** research · **SLA:** 1200h (~50 days)

Bank Indonesia's *Statistik Ekonomi dan Keuangan Indonesia* — Indonesia's monthly
economic and financial statistics: money and banking, balance of payments, GDP,
prices, and external debt.

## Upstream

```
https://www.bi.go.id/id/statistik/ekonomi-keuangan/seki/Default.aspx
```

An HTML index that links ~108 tables, each as both a PDF and an `.xls`. We take
the Excel; the PDF is the same numbers rendered for print. The files themselves
live at `https://www.bi.go.id/SEKI/tabel/TABEL<n>_<m>.xls`.

## What lands in raw/

Per run:

| File | What it is |
|---|---|
| `seki_<date>_index.html` | the index page exactly as served |
| `seki_<date>_catalogue.json` | table id, number, title, section, URL |
| `seki_<date>_TABEL1_1.xls` … | ~108 workbooks, ~24 MB total |

The catalogue matters. A raw `.xls` does not know its own name — the table title
("Uang Beredar dan Faktor-Faktor yang Mempengaruhinya") and its section
("I. UANG DAN BANK") exist only on the index page, so they are captured with it.

## The format, and why it is awkward

Bank Indonesia publishes **legacy BIFF `.xls`**, not `.xlsx`. `openpyxl` cannot
read it; the parser uses `xlrd`, which since 2.0 reads exactly this format and
nothing else. DuckDB cannot read it either, so `transform_seki` decodes the
workbooks in Python and hands DuckDB one relation.

Every table shares a geometry, but four things vary and are therefore detected
rather than assumed:

* **the header row** (3 to 7, depending on how many banner lines the table has),
* **the label column**,
* **the frequency** — monthly, quarterly, or annual,
* **how the period is written**.

That last one is the trap. About half the tables spell the period as text
(`Jan`, `Q1`); the other half store a real Excel date, which arrives as a bare
float — `36892.0` is 2001-01-01. A parser reading only cell *values* sees a
number, finds no period row, falls through to the annual branch, and silently
emits one wrong observation per column. `tables.py` reads cell *types* too.

Three more traps, each of which the parser got wrong once:

1. **A date-formatted counter is not a date.** Some header cells hold `1.0` with
   a date format, which Excel renders as 1900-01-01. Serials outside
   `[1950, 2100]` are rejected.
2. **Years do not forward-fill.** A wide sheet lays years side by side, and the
   year label sits above January in one block and above December in the next.
   Carrying it rightwards stamps the following year's block with the previous
   year. The month sequence restarting is the honest block boundary.
3. **An indicator name is not an identity.** "Pinjaman yang Diberikan" appears
   four times in one sheet under different parents. Bank Indonesia numbers its
   own rows in column 0, and that number disambiguates them.

## What we cannot read

`TABEL8_1`'s five pre-2002 sheets use an indented outline (`1.` → `1.1.` → `a.`
→ `= Makanan / Food`) rather than the numbered grid, and a single label column
cannot identify a series in it. `parse_sheet` raises `AmbiguousSheet` and the
sheet is skipped with a warning — about 4.7% of that table's rows. Emitting both
values would double-count; picking one would discard real data.

## Reliability

The endpoints are intermittently flaky: a table that answers `302` with an empty
body on one request serves 130 kB of Excel on the next. Per-file failures are
therefore collected rather than raised, as `gov_news` does for its PDFs.

But a dead link and a site-wide outage look identical one file at a time. So the
run fails if fewer than `min_success_ratio` (default 0.90) of the discovered
tables land. A partial month must never masquerade as a complete one.

Bytes are checked against the OLE2 signature before they reach `raw/`, so an HTML
error page served as `TABEL1_1.xls` is quarantined rather than parsed six months
later.

## Processed

`transform_seki` → `dataset=seki_indicators`, partitioned by `year`:

| Column | Type | Notes |
|---|---|---|
| `table_id` | VARCHAR | `TABEL1_1` |
| `table_number` | VARCHAR | `I.1.` |
| `table_title` | VARCHAR | from the catalogue |
| `section` | VARCHAR | one of the nine SEKI sections |
| `indicator` | VARCHAR | the row label, as published |
| `row_no` | BIGINT | Bank Indonesia's own row number |
| `period` | DATE | first day of the month/quarter/year |
| `year` | BIGINT | partition key |
| `freq` | VARCHAR | `monthly` \| `quarterly` \| `annual` |
| `value` | DOUBLE | a blank cell is missing, never zero |
| `unit` | VARCHAR | `Miliar Rp`, `Juta USD`, `Persen Per Tahun`, … |

Roughly **1.0M observations** across **1,433 indicators**, 1968 to the present.

Sheets overlap on purpose — each table carries a current sheet plus year-range
history sheets, and the same observation appears on both. Where they disagree the
later revision wins: the value on the sheet whose data runs furthest forward.

## Run it

```bash
uv run lake scrape seki                 # ~24 MB, ~20s
uv run lake transform seki_indicators   # needs: uv sync --extra transform
uv run lake serve build
```

The monthly systemd timer (`lake-monthly.timer`, the 3rd at 07:00) picks this
source up from `configs/sources.yaml` with no new unit — `scrape-schedule
monthly` dispatches every enabled source with `schedule: monthly`.
