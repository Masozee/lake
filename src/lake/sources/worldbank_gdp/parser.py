"""Pure function: bytes -> list[dict]. No network. No disk. No config.

That purity is the point — every parser is testable against a captured fixture
in under a millisecond, which is what lets you notice the day upstream silently
renames a field.
"""

from __future__ import annotations

import json
from typing import Any


def parse(raw: bytes) -> list[dict[str, Any]]:
    body = json.loads(raw)
    if not isinstance(body, list) or len(body) < 2 or body[1] is None:
        return []

    records = []
    for row in body[1]:
        iso3 = row.get("countryiso3code")
        if not iso3:  # aggregates (e.g. "World") carry an empty iso3
            continue
        records.append(
            {
                "country_iso3": iso3,
                "country_name": (row.get("country") or {}).get("value"),
                "year": int(row["date"]),
                "gdp_usd": row.get("value"),
                "indicator": (row.get("indicator") or {}).get("id"),
            }
        )
    return records
