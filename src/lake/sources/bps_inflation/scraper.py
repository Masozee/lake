"""Monthly scraper — a single Excel file, fetched conditionally.

Illustrates dedupe layer 3: we send the ETag / Last-Modified from our last
successful run. A 304 means upstream published nothing new, which is a *skip*,
not a failure — and recording it as such is what keeps the freshness alert
honest.

Monthly sources publish late. The timer fires on the 3rd, not the 1st, and the
logical_date is the first of the month the data describes.
"""

from __future__ import annotations

import httpx

from lake.core.base_scraper import BaseScraper
from lake.core.exceptions import SourceUnchanged
from lake.core.logging import get_logger
from lake.core.models import Artifact, FetchedFile, RunContext
from lake.core.retry import retrying

log = get_logger(__name__)


class BpsInflationScraper(BaseScraper):
    source_id = "bps_inflation"
    schedule = "monthly"

    def fetch(self, ctx: RunContext) -> list[Artifact]:
        url = self.config["url"]
        headers = {"User-Agent": self.user_agent, **self.prior_conditional_headers()}
        if len(headers) > 1:
            log.debug("fetch.conditional", headers=sorted(set(headers) - {"User-Agent"}))

        retry_cfg = self.config.get("retry", {})
        response: httpx.Response | None = None

        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for attempt in retrying(
                attempts=retry_cfg.get("attempts", 5),
                initial_seconds=retry_cfg.get("backoff_seconds", 10),
                max_seconds=retry_cfg.get("max_backoff_seconds", 600),
            ):
                with attempt:
                    response = client.get(url, headers=headers)
                    # 304 is not an error and must not be retried.
                    if response.status_code != 304:
                        response.raise_for_status()

        assert response is not None

        if response.status_code == 304:
            raise SourceUnchanged(f"{self.source_id}: upstream returned 304 Not Modified")

        return [
            FetchedFile(
                filename=f"{self.source_id}_{ctx.logical_date:%Y%m%d}_000.xlsx",
                content=response.content,
                url=str(response.url),
                http_status=response.status_code,
                content_type=response.headers.get("content-type"),
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            )
        ]
