# gov_news

**Schedule:** weekly, Monday 06:30 UTC ±30m · **Owner:** data-team · **SLA:** 200h (~8d)

Press releases: one HTML index page plus every PDF it links to.

## Upstream

```
https://example.gov/news
```

The index is stored as fetched. PDFs are resolved to absolute URLs, deduplicated,
and capped at `max_pdfs` (default 50).

## Partial success is real

A single unreachable PDF must not discard the index page and the other twelve
PDFs. Per-file failures are logged (`fetch.pdf_failed`) and the run continues.
Only a failure to fetch the *index* fails the run.

This means a run can succeed while silently having skipped a PDF. Check:

```bash
journalctl -u lake-scrape@gov_news --since '8 days ago' -o cat \
  | jq -c 'select(.event == "fetch.pdf_failed")'
```

## Quirks

* **A PDF link can serve HTML.** A dead link behind a redirect returns a login
  page with HTTP 200. `looks_like_html()` catches it per-file; that PDF is skipped
  rather than saved as `gov_news_20260706_003.pdf`.
* **Query strings on PDF links.** `report.pdf?v=2` is a PDF. The extension check
  strips the query before testing.
* **The same PDF appears twice** on the index (once in the article, once in a
  "recent files" sidebar). `extract_pdf_links` deduplicates while preserving order,
  and the checksum layer would have caught it anyway.
* **`max_pdfs` silently truncates.** If the index ever lists more than 50 PDFs you
  will quietly miss the tail. Raise it, or split the source.

## Retention

`raw_days: 3650` — a ten-year legal hold on published statements. This overrides
the five-year default, in `configs/sources.yaml`.

## Files

```
raw/source=gov_news/year=2026/month=07/day=06/run=.../
    gov_news_20260706_index.html
    gov_news_20260706_001.pdf
    gov_news_20260706_002.pdf
    ...
    _MANIFEST.json
```

## Refreshing the test fixture

```bash
curl -s https://example.gov/news > tests/fixtures/gov_news_index.html
```

The parser test asserts on link resolution, deduplication, and query-string
handling. If the site's markup changes, that test goes red before the scraper
starts silently returning zero PDFs.
