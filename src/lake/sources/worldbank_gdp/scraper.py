"""Daily scraper — paginated JSON API.

Illustrates: in-run retry, pagination, one artifact per page. Pages are stored
as fetched. We do not merge or reshape them here; that is transform.py's job,
and keeping raw byte-identical to the response is what makes replay possible.
"""

from __future__ import annotations

import json

import httpx

from lake.core.base_scraper import BaseScraper
from lake.core.logging import get_logger
from lake.core.models import Artifact, FetchedFile, RunContext
from lake.core.retry import retrying

log = get_logger(__name__)

MAX_PAGES = 100  # guard against a runaway `pages` value from upstream


class WorldBankGDPScraper(BaseScraper):
    source_id = "worldbank_gdp"
    schedule = "daily"

    def _get_page(self, client: httpx.Client, page: int) -> httpx.Response:
        retry_cfg = self.config.get("retry", {})
        for attempt in retrying(
            attempts=retry_cfg.get("attempts", 5),
            initial_seconds=retry_cfg.get("backoff_seconds", 10),
            max_seconds=retry_cfg.get("max_backoff_seconds", 600),
        ):
            with attempt:
                response = client.get(
                    self.config["base_url"],
                    params={**self.config.get("params", {}), "page": page},
                )
                response.raise_for_status()
                return response
        raise AssertionError("unreachable: retrying() re-raises on exhaustion")

    def fetch(self, ctx: RunContext) -> list[Artifact]:
        artifacts: list[Artifact] = []

        with httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent},
            follow_redirects=True,
        ) as client:
            page, total_pages = 1, 1

            while page <= total_pages and page <= MAX_PAGES:
                response = self._get_page(client, page)
                body = response.json()

                # World Bank shape: [metadata, [records...]]
                if not isinstance(body, list) or len(body) < 2:
                    raise ValueError(f"unexpected API envelope on page {page}: {str(body)[:200]}")

                total_pages = int(body[0].get("pages", 1))
                artifacts.append(
                    FetchedFile(
                        filename=f"{self.source_id}_{ctx.logical_date:%Y%m%d}_{page:03d}.json",
                        content=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                        url=str(response.url),
                        http_status=response.status_code,
                        content_type=response.headers.get("content-type"),
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                    )
                )
                log.debug("fetch.page", page=page, total_pages=total_pages)
                page += 1

        log.info("fetch.complete", pages=len(artifacts))
        return artifacts
