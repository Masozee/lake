"""Statistical gate — the third and last validation layer.

Publishing zero rows is a perfectly successful HTTP 200. Nothing upstream of
here can tell you that a source quietly started returning an empty table, or
that a column went 90% null after a schema change. This is where you catch it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from lake.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class CheckResult:
    check_name: str
    passed: bool
    detail: dict

    def __bool__(self) -> bool:
        return self.passed


def check_row_count_sane(
    rows: int, history: list[int], *, sigma: float = 3.0, min_history: int = 4
) -> CheckResult:
    """Row count within N standard deviations of recent runs.

    With too little history we only assert non-empty — a wide interval computed
    from three points is worse than no interval at all.
    """
    if rows == 0:
        return CheckResult("row_count_sane", False, {"rows": 0, "reason": "empty result"})

    if len(history) < min_history:
        return CheckResult(
            "row_count_sane",
            True,
            {"rows": rows, "reason": "insufficient history", "n": len(history)},
        )

    mean = statistics.fmean(history)
    stdev = statistics.pstdev(history)
    if stdev == 0:
        passed = rows == history[-1]
        return CheckResult("row_count_sane", passed, {"rows": rows, "expected": history[-1]})

    z = abs(rows - mean) / stdev
    return CheckResult(
        "row_count_sane",
        z <= sigma,
        {"rows": rows, "mean": round(mean, 1), "stdev": round(stdev, 1), "z": round(z, 2)},
    )


def check_null_rate(
    null_counts: dict[str, int], total_rows: int, *, max_rate: float = 0.5
) -> CheckResult:
    if total_rows == 0:
        return CheckResult("null_rate", False, {"reason": "no rows"})

    offenders = {
        col: round(n / total_rows, 3) for col, n in null_counts.items() if n / total_rows > max_rate
    }
    return CheckResult("null_rate", not offenders, {"offenders": offenders, "max_rate": max_rate})


def check_primary_key_unique(total_rows: int, distinct_keys: int, key: list[str]) -> CheckResult:
    return CheckResult(
        "primary_key_unique",
        total_rows == distinct_keys,
        {
            "key": key,
            "rows": total_rows,
            "distinct": distinct_keys,
            "dupes": total_rows - distinct_keys,
        },
    )
