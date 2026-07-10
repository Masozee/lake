"""Contract for one validated record. A row that fails here is quarantined."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GdpRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    country_iso3: str = Field(min_length=3, max_length=3)
    country_name: str | None = None
    year: int = Field(ge=1960, le=2100)
    gdp_usd: float | None = None
    indicator: str | None = None

    @field_validator("country_iso3")
    @classmethod
    def upper_alpha(cls, v: str) -> str:
        if not v.isalpha():
            raise ValueError(f"country_iso3 must be alphabetic, got {v!r}")
        return v.upper()

    @field_validator("gdp_usd")
    @classmethod
    def non_negative(cls, v: float | None) -> float | None:
        if v is not None and v < 0:
            raise ValueError(f"gdp_usd cannot be negative, got {v}")
        return v
