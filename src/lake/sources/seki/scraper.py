"""Monthly scraper — Bank Indonesia's SEKI: an index page plus ~108 Excel tables.

Statistik Ekonomi dan Keuangan Indonesia is Indonesia's monthly economic and
financial statistics release. The index page lists every table with its own
numbering; each row links a PDF and an .xls. We take the .xls files, because the
PDFs are the same numbers rendered for print.

Three things about this source shape the code:

  * Bank Indonesia publishes legacy BIFF .xls, not .xlsx. The bytes are checked
    against the OLE2 signature, so an HTML error page served as `TABEL1_1.xls`
    is quarantined rather than parsed months later.
  * The endpoints are intermittently flaky: a table that answers 302 with an
    empty body on one request serves 130 kB of Excel on the next. A transient
    failure must not discard the other 107 tables, so per-file failures are
    collected — exactly as gov_news does for its PDFs.
  * A site-wide outage looks the same as one flaky file, one file at a time. So
    the run fails if fewer than `min_success_ratio` of the tables land, and a
    partial month never masquerades as a complete one.

The index page is stored alongside the tables. It is the only record of what was
offered on the day we collected, and the transform reads table titles from it.
"""

from __future__ import annotations

import json

import httpx

from lake.core.base_scraper import BaseScraper
from lake.core.exceptions import FetchError
from lake.core.logging import get_logger
from lake.core.models import Artifact, FetchedFile, RunContext
from lake.core.retry import retrying
from lake.core.sniff import describe, looks_like_html, matches_extension
from lake.sources.seki.parser import extract_tables

log = get_logger(__name__)

#: Fail the run below this share of tables. One dead link is noise; half the
#: site failing is an outage, and a partial month must not look like a full one.
DEFAULT_MIN_SUCCESS_RATIO = 0.90


class SekiScraper(BaseScraper):
    source_id = "seki"
    schedule = "monthly"

    def _get(self, client: httpx.Client, url: str, **kwargs) -> httpx.Response:
        retry_cfg = self.config.get("retry", {})
        for attempt in retrying(
            attempts=retry_cfg.get("attempts", 5),
            initial_seconds=retry_cfg.get("backoff_seconds", 10),
            max_seconds=retry_cfg.get("max_backoff_seconds", 600),
        ):
            with attempt:
                response = client.get(url, **kwargs)
                response.raise_for_status()
                return response
        raise AssertionError("unreachable")

    def fetch(self, ctx: RunContext) -> list[Artifact]:
        index_url = self.config["index_url"]
        max_tables = self.config.get("max_tables")
        min_ratio = float(self.config.get("min_success_ratio", DEFAULT_MIN_SUCCESS_RATIO))
        stamp = f"{ctx.logical_date:%Y%m%d}"

        with httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            index = self._get(client, index_url)
            artifacts: list[Artifact] = [
                FetchedFile(
                    filename=f"{self.source_id}_{stamp}_index.html",
                    content=index.content,
                    url=str(index.url),
                    http_status=index.status_code,
                    content_type=index.headers.get("content-type"),
                    etag=index.headers.get("etag"),
                    last_modified=index.headers.get("last-modified"),
                )
            ]

            tables = extract_tables(
                index.content,
                str(index.url),
                limit=int(max_tables) if max_tables else None,
            )
            if not tables:
                raise FetchError(f"{self.source_id}: no Excel tables found on {index_url}")
            log.info("fetch.index_parsed", tables=len(tables))

            # The catalogue travels with the data: table titles and section names
            # live only on the index page, and a raw .xls does not carry them.
            artifacts.append(
                FetchedFile(
                    filename=f"{self.source_id}_{stamp}_catalogue.json",
                    content=json.dumps(
                        [
                            {
                                "table_id": t.table_id,
                                "number": t.number,
                                "title": t.title,
                                "section": t.section,
                                "url": t.url,
                            }
                            for t in tables
                        ],
                        ensure_ascii=False,
                        indent=2,
                    ).encode("utf-8"),
                    url=str(index.url),
                    http_status=index.status_code,
                    content_type="application/json",
                )
            )

            failures: list[str] = []
            for table in tables:
                content = self._fetch_table(client, table.table_id, table.url, failures)
                if content is None:
                    continue
                artifacts.append(
                    FetchedFile(
                        filename=f"{self.source_id}_{stamp}_{table.table_id}.xls",
                        content=content.content,
                        url=str(content.url),
                        http_status=content.status_code,
                        content_type=content.headers.get("content-type"),
                        etag=content.headers.get("etag"),
                        last_modified=content.headers.get("last-modified"),
                    )
                )

        landed = len(tables) - len(failures)
        ratio = landed / len(tables)
        if failures:
            log.warning(
                "fetch.partial",
                failed=len(failures),
                landed=landed,
                of=len(tables),
                examples=failures[:5],
            )
        if ratio < min_ratio:
            raise FetchError(
                f"{self.source_id}: only {landed}/{len(tables)} tables landed "
                f"({ratio:.0%} < {min_ratio:.0%}) — treating as an upstream outage"
            )

        log.info("fetch.complete", files=len(artifacts), tables=landed, failed=len(failures))
        return artifacts

    def _fetch_table(
        self, client: httpx.Client, table_id: str, url: str, failures: list[str]
    ) -> httpx.Response | None:
        """One table, or None with the failure recorded. Never raises."""
        try:
            response = self._get(client, url)
        except (httpx.HTTPError, FetchError) as exc:
            log.warning("fetch.table_failed", table_id=table_id, error=str(exc)[:200])
            failures.append(table_id)
            return None

        # A 302 to a friendly error page is a 200 with HTML in it. Both the empty
        # body and the HTML must be caught here, not by a parser six months on.
        if not response.content:
            log.warning("fetch.table_empty", table_id=table_id, status=response.status_code)
            failures.append(table_id)
            return None
        if looks_like_html(response.content) or not matches_extension(response.content, "xls"):
            log.warning(
                "fetch.table_not_excel",
                table_id=table_id,
                sniffed=describe(response.content),
            )
            failures.append(table_id)
            return None

        return response
