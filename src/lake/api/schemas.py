"""Request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from lake.api.engine import DEFAULT_ROW_LIMIT, MAX_ROW_LIMIT


class QueryRequest(BaseModel):
    sql: str = Field(min_length=1, description="a single read-only SELECT")
    limit: int = Field(default=DEFAULT_ROW_LIMIT, ge=1, le=MAX_ROW_LIMIT)


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool


class TableInfo(BaseModel):
    name: str
    row_count: int
    columns: list[ColumnInfo]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ErrorResponse(BaseModel):
    error: str
