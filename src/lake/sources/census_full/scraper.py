"""Yearly scraper — one very large gzipped CSV.

A NUC has 8-32 GB of RAM. `response.content` on a 4 GB dump is an OOM kill, and
the OOM killer does not write a nice traceback. Anything that might exceed a few
hundred megabytes gets streamed to staging in chunks, digested on the way past,
and verified before it is allowed near raw/.
"""

from __future__ import annotations

import hashlib
import os

import httpx

from lake.core.base_scraper import BaseScraper
from lake.core.exceptions import FetchError, ValidationFailed
from lake.core.logging import get_logger
from lake.core.models import Artifact, RunContext, StreamedFile
from lake.core.validate import check_gzip_decompresses

log = get_logger(__name__)

CHUNK = 1 << 20  # 1 MiB


class CensusScraper(BaseScraper):
    source_id = "census_full"
    schedule = "yearly"

    def fetch(self, ctx: RunContext) -> list[Artifact]:
        url = self.config["url"]
        filename = f"{self.source_id}_{ctx.logical_date:%Y}_000.csv.gz"
        dest = self.storage.staging_path(ctx, filename)

        digest = hashlib.sha256()
        written = 0

        # Long, single-shot transfer: no read timeout, but keep a connect timeout
        # so a dead host fails fast instead of hanging the yearly job.
        timeout = httpx.Timeout(connect=30.0, read=None, write=None, pool=30.0)

        with (
            httpx.Client(timeout=timeout, follow_redirects=True) as client,
            client.stream("GET", url, headers={"User-Agent": self.user_agent}) as response,
        ):
            if response.status_code >= 400:
                raise FetchError(
                    f"{url} returned HTTP {response.status_code}",
                    transient=response.status_code >= 500,
                    http_status=response.status_code,
                )

            expected = response.headers.get("content-length")
            with open(dest, "wb") as fh:
                for chunk in response.iter_bytes(CHUNK):
                    digest.update(chunk)
                    fh.write(chunk)
                    written += len(chunk)
                fh.flush()
                os.fsync(fh.fileno())

            headers = dict(response.headers)

        # A truncated transfer is the failure mode here, and it looks like success.
        if expected is not None and written != int(expected):
            raise ValidationFailed(
                f"{filename}: truncated download, got {written} of {expected} bytes",
                check_name="content_length",
                detail={"expected": int(expected), "actual": written},
            )

        check_gzip_decompresses(dest)

        log.info("fetch.streamed", filename=filename, bytes=written)
        return [
            StreamedFile(
                filename=filename,
                path=dest,
                url=url,
                http_status=200,
                content_type=headers.get("content-type"),
                etag=headers.get("etag"),
                last_modified=headers.get("last-modified"),
                _sha256=digest.hexdigest(),
            )
        ]
