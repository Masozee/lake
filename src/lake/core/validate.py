"""Structural validation — the cheapest gate, run before anything touches raw/.

Three gates exist in the system, cheapest first:
    1. structural (here): non-empty, magic bytes match extension, decompresses
    2. schema (sources/*/schema.py): pydantic, per record
    3. statistical (transform/quality.py): row count within N sigma, null rates

Fail closed. A pipeline that quietly publishes bad data is worse than one that
stops and pages you.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from lake.core.exceptions import ValidationFailed
from lake.core.models import Artifact, FetchedFile, StreamedFile
from lake.core.sniff import describe, looks_like_html, matches_extension

MIN_BYTES = 16


def _head(artifact: Artifact, n: int = 512) -> bytes:
    if isinstance(artifact, FetchedFile):
        return artifact.content[:n]
    if isinstance(artifact, StreamedFile):
        with open(artifact.path, "rb") as fh:
            return fh.read(n)
    raise TypeError(f"cannot read head of {type(artifact).__name__}")


def check_structural(artifact: Artifact, *, min_bytes: int = MIN_BYTES) -> None:
    """Raise ValidationFailed if the bytes obviously are not what we asked for."""
    ext = Path(artifact.filename).suffix.lstrip(".").lower()
    size = artifact.size_bytes

    if size < min_bytes:
        raise ValidationFailed(
            f"{artifact.filename}: {size} bytes is below the {min_bytes}-byte floor",
            check_name="non_empty",
            detail={"size_bytes": size},
        )

    head = _head(artifact)

    # An HTML error page wearing a data extension. Catch it here or never.
    if ext not in {"html", "htm", "txt"} and looks_like_html(head):
        raise ValidationFailed(
            f"{artifact.filename}: got an HTML page where .{ext} was expected "
            f"(upstream error page returned with HTTP {artifact.http_status}?)",
            check_name="magic_bytes",
            detail={
                "detected": "html",
                "expected": ext,
                "head": head[:120].decode(errors="replace"),
            },
        )

    if not matches_extension(head, ext):
        raise ValidationFailed(
            f"{artifact.filename}: magic bytes say {describe(head)!r}, extension says {ext!r}",
            check_name="magic_bytes",
            detail={"detected": describe(head), "expected": ext},
        )


def check_json_parses(artifact: Artifact) -> None:
    if not isinstance(artifact, FetchedFile):
        raise TypeError("check_json_parses expects an in-memory FetchedFile")
    try:
        json.loads(artifact.content)
    except json.JSONDecodeError as exc:
        raise ValidationFailed(
            f"{artifact.filename}: not valid JSON at pos {exc.pos}: {exc.msg}",
            check_name="json_parses",
        ) from exc


def check_gzip_decompresses(path: Path, *, probe_bytes: int = 1 << 20) -> None:
    """Read a slice through the gzip stream. Catches truncated downloads."""
    try:
        with gzip.open(path, "rb") as fh:
            fh.read(probe_bytes)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise ValidationFailed(
            f"{path.name}: gzip stream is corrupt or truncated: {exc}",
            check_name="gzip_decompresses",
        ) from exc


def check_all(artifacts: list[Artifact]) -> None:
    """Structural gate for a whole run. First failure aborts and quarantines."""
    if not artifacts:
        raise ValidationFailed(
            "scraper returned zero artifacts — upstream published nothing, or a selector broke",
            check_name="non_empty_run",
        )
    for artifact in artifacts:
        check_structural(artifact)
