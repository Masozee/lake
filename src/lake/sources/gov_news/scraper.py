"""Weekly scraper — HTML index page plus the PDFs it links to.

Illustrates: mixed content types in one run, and partial tolerance. A single
unreachable PDF must not discard the index page and the other twelve PDFs, so
per-file failures are collected and surfaced rather than raised.
"""

from __future__ import annotations

import httpx

from lake.core.base_scraper import BaseScraper
from lake.core.exceptions import FetchError
from lake.core.logging import get_logger
from lake.core.models import Artifact, FetchedFile, RunContext
from lake.core.retry import retrying
from lake.core.sniff import looks_like_html
from lake.sources.gov_news.parser import extract_pdf_links

log = get_logger(__name__)


class GovNewsScraper(BaseScraper):
    source_id = "gov_news"
    schedule = "weekly"

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
        max_pdfs = int(self.config.get("max_pdfs", 50))
        stamp = f"{ctx.logical_date:%Y%m%d}"
        artifacts: list[Artifact] = []

        with httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            index = self._get(client, index_url)
            artifacts.append(
                FetchedFile(
                    filename=f"{self.source_id}_{stamp}_index.html",
                    content=index.content,
                    url=str(index.url),
                    http_status=index.status_code,
                    content_type=index.headers.get("content-type"),
                    etag=index.headers.get("etag"),
                    last_modified=index.headers.get("last-modified"),
                )
            )

            links = extract_pdf_links(index.content, str(index.url), limit=max_pdfs)
            log.info("fetch.index_parsed", pdf_links=len(links))

            failures: list[str] = []
            for i, href in enumerate(links, start=1):
                try:
                    pdf = self._get(client, href)
                except (httpx.HTTPError, FetchError) as exc:
                    # One bad PDF must not sink the whole weekly run.
                    log.warning("fetch.pdf_failed", url=href, error=str(exc)[:200])
                    failures.append(href)
                    continue

                if looks_like_html(pdf.content):
                    log.warning("fetch.pdf_was_html", url=href)
                    failures.append(href)
                    continue

                artifacts.append(
                    FetchedFile(
                        filename=f"{self.source_id}_{stamp}_{i:03d}.pdf",
                        content=pdf.content,
                        url=str(pdf.url),
                        http_status=pdf.status_code,
                        content_type=pdf.headers.get("content-type"),
                        etag=pdf.headers.get("etag"),
                        last_modified=pdf.headers.get("last-modified"),
                    )
                )

        if failures:
            log.warning("fetch.partial", failed=len(failures), succeeded=len(artifacts))
        log.info("fetch.complete", files=len(artifacts))
        return artifacts
