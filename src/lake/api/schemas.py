"""Request/response models for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field


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
