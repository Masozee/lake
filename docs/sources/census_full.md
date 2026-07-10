# census_full

**Schedule:** yearly, 15 January at 03:00 UTC ±2h · **Owner:** research · **SLA:** 9000h (~375d)
**Status:** `enabled: false` — flip it on once the URL is real.

One very large gzipped CSV, once a year.

## Why this one is different

A NUC has 8–32 GB of RAM. `response.content` on a 4 GB dump is an OOM kill, and
the OOM killer does not leave a traceback — the run simply vanishes.

So this scraper is the only one that streams:

* `httpx.stream()` in 1 MiB chunks, straight to staging on the NUC's local SSD.
* The SHA-256 is computed **on the way past**, never by re-reading the file.
* It returns a `StreamedFile` (a path) rather than a `FetchedFile` (bytes).
  `Storage.commit()` handles both; nothing else changes.

Likewise `parser.py` takes a `Path` and yields a generator. The asymmetry with
every other parser is deliberate. Do not "fix" it for consistency.

## Truncation is the failure mode, and it looks like success

A dropped connection at 90% gives you a valid-looking gzip prefix and HTTP 200.
Three defences, in order:

1. `Content-Length` vs bytes actually written → `ValidationFailed`.
2. `check_gzip_decompresses()` reads a slice through the stream → catches a
   truncated member even when the length header lied or was absent.
3. The digest is verified again from disk inside `commit()` before the atomic
   rename. Nothing partial ever reaches `raw/`.

## Timeouts

```python
httpx.Timeout(connect=30.0, read=None, write=None, pool=30.0)
```

No read timeout — a legitimate multi-hour transfer must not be killed. A connect
timeout, so a dead host fails in 30 seconds rather than hanging the yearly job.
`TimeoutStartSec=21600` on the dispatcher unit is the real backstop.

## Quirks

* **Null sentinels.** Upstream uses `NA`, `N/A`, `-`, `-999`, and `-9999`. All map
  to `None` in `parser.clean()`. A genuine `0` survives, as it must.
* **Column names have trailing spaces** and mixed case. `normalise_key()` handles it.
* **`archive_after_days: 730`.** Census data is small and precious; archive late.
  `raw_days: null` in `configs/retention.yaml` means *never delete* — the source
  may not be reproducible.

## Files

```
raw/source=census_full/year=2026/month=01/day=15/run=.../
    census_full_2026_000.csv.gz
    census_full_2026_000.csv.gz.meta.json
    _MANIFEST.json
```

Note `logical_date` is `2026-01-15` but the filename carries only the year — a
yearly source has one file per year, and the day is an artifact of when we ran.

## Reading it

```python
from pathlib import Path
from lake.sources.census_full.parser import count_rows, iter_records, peek

path = Path("/mnt/nas/lake/raw/source=census_full/year=2026/.../census_full_2026_000.csv.gz")
peek(path, 5)          # first five records, cheap
count_rows(path)       # streams; does not materialise
for record in iter_records(path):   # a generator. never accumulates.
    ...
```
