"""Contract for one validated inflation observation.

The bounds here are the useful part, and they hold no matter what the spreadsheet
layout turns out to be: a monthly inflation figure outside [-50%, +100%] is a
parsing error — a percentage read as a raw index, a column offset by one — not an
economy. Fail closed and quarantine it.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InflationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    region: str = Field(min_length=1, max_length=128)
    #: always the first of the month the figure describes, never the fetch date
    period: date
    inflation_pct: float = Field(ge=-50.0, le=100.0)

    @field_validator("period")
    @classmethod
    def first_of_month(cls, v: date) -> date:
        if v.day != 1:
            raise ValueError(f"period must be the first of the month, got {v}")
        return v

    @field_validator("region")
    @classmethod
    def not_a_total_row(cls, v: str) -> str:
        # Spreadsheets carry summary rows that look exactly like data rows.
        if v.strip().lower() in {"total", "jumlah", "nasional", "national", "indonesia"}:
            raise ValueError(f"{v!r} is an aggregate row, not a region")
        return v
