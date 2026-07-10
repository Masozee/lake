"""Streaming parser for the yearly gzipped CSV.

Unlike the other parsers, this one takes a path rather than bytes. The file is
gigabytes; `parse(raw: bytes)` would mean holding all of it in RAM, and a NUC
does not have the RAM. The signature difference is deliberate — do not "fix" it
for symmetry.

Everything here is a generator. Nothing accumulates.
"""

from __future__ import annotations

import csv
import gzip
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# A missing value is not an empty string, and it is certainly not zero. Upstream
# uses several sentinels; map them all to None so a single NULL check suffices.
NULL_SENTINELS = frozenset({"", "NA", "N/A", "null", "NULL", "-", "--", "-999", "-9999"})


def iter_records(path: Path, *, encoding: str = "utf-8") -> Iterator[dict[str, Any]]:
    """Yield one dict per row. Never holds more than one row in memory."""
    with gzip.open(path, "rt", encoding=encoding, newline="") as fh:
        for row in csv.DictReader(fh):
            yield {normalise_key(k): clean(v) for k, v in row.items() if k}


def normalise_key(key: str) -> str:
    """`Total Population ` -> `total_population`."""
    return key.strip().lower().replace(" ", "_").replace("-", "_")


def clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return None if stripped in NULL_SENTINELS else stripped


def count_rows(path: Path) -> int:
    """Row count without materialising the file. Feeds the 3-sigma quality gate."""
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        return sum(1 for _ in fh) - 1  # minus the header


def peek(path: Path, n: int = 5) -> list[dict[str, Any]]:
    """First n records, for a notebook or a quick sanity check."""
    from itertools import islice

    return list(islice(iter_records(path), n))
