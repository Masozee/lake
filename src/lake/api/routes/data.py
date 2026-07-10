"""Data routes: catalog, query, and streaming.

Everything here is GET or a read-only POST. There is deliberately no route that
writes — not disabled, absent. The engine could not honour one anyway.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from lake.api import catalog, engine, export
from lake.api.schemas import ColumnInfo, QueryRequest, QueryResponse, TableInfo
from lake.api.sql_guard import UnsafeQuery, validate
from lake.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter()


@router.get("/tables", response_model=list[str])
def get_tables() -> list[str]:
    return catalog.list_tables()


@router.get("/tables/{name}", response_model=TableInfo)
def get_table(name: str) -> TableInfo:
    try:
        table = catalog.describe_table(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    return TableInfo(
        name=table.name,
        row_count=table.row_count,
        columns=[ColumnInfo(name=c.name, type=c.type, nullable=c.nullable) for c in table.columns],
    )


@router.get("/tables/{name}/sample")
def get_sample(name: str, limit: int = Query(default=5, ge=1, le=100)) -> dict:
    try:
        return catalog.sample_table(name, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc


@router.get("/tables/{name}/profile")
def get_profile(name: str) -> dict:
    try:
        return {"table": name, "profile": catalog.column_profile(name)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc


@router.post("/query", response_model=QueryResponse)
def post_query(request: QueryRequest) -> QueryResponse:
    """Run a read-only SELECT and return the full (capped) result as JSON."""
    try:
        validated = validate(request.sql, connection=engine.serving())
    except UnsafeQuery as exc:
        # 422: the request is well-formed but the query is not permitted.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        result = engine.run_query(validated.sql, limit=request.limit)
    except engine.QueryTimeout as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc

    return QueryResponse(**result)


@router.post("/query/stream")
def post_query_stream(
    request: QueryRequest,
    max_rows: int = Query(default=engine.MAX_ROW_LIMIT, ge=1, le=engine.MAX_ROW_LIMIT),
) -> StreamingResponse:
    """Stream a read-only SELECT as newline-delimited JSON.

    The first line is a header `{"columns": [...]}`; every subsequent line is one
    row as a JSON array. The result is never fully materialised in the server —
    this is what lets a client pull a large table without the server buffering it.
    """
    try:
        validated = validate(request.sql, connection=engine.serving())
    except UnsafeQuery as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def generate() -> Iterator[bytes]:
        header_sent = False
        try:
            for columns, rows in engine.stream_batches(validated.sql, max_rows=max_rows):
                if not header_sent:
                    yield (json.dumps({"columns": columns}) + "\n").encode()
                    header_sent = True
                for row in rows:
                    yield (json.dumps(row, separators=(",", ":")) + "\n").encode()
            if not header_sent:
                yield (json.dumps({"columns": []}) + "\n").encode()
        except engine.QueryTimeout as exc:
            # Mid-stream errors cannot change the HTTP status (already 200), so
            # signal them in-band as a final error line the client checks for.
            yield (json.dumps({"error": str(exc)}) + "\n").encode()
        except Exception as exc:
            log.exception("stream.failed")
            yield (json.dumps({"error": f"{type(exc).__name__}"}) + "\n").encode()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# -- export (the researcher's real ask: give me the spreadsheet) --------------


def _validated_or_422(sql: str) -> str:
    try:
        return validate(sql, connection=engine.serving()).sql
    except UnsafeQuery as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/query/export.csv")
def export_query_csv(
    sql: str = Query(..., description="a single read-only SELECT"),
    filename: str = Query("export"),
) -> StreamingResponse:
    """Download a query result as CSV. Streams — the server never buffers it all."""
    validated = _validated_or_422(sql)
    name = export.safe_filename(filename, "csv")
    return StreamingResponse(
        export.stream_csv(validated),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/query/export.xlsx")
def export_query_xlsx(
    sql: str = Query(...),
    filename: str = Query("export"),
) -> Response:
    """Download a query result as Excel."""
    validated = _validated_or_422(sql)
    name = export.safe_filename(filename, "xlsx")
    try:
        content = export.build_xlsx(validated)
    except engine.QueryTimeout as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/tables/{name}/export.csv")
def export_table_csv(name: str) -> StreamingResponse:
    """Download a whole table as CSV. The one-click researcher path."""
    try:
        table = catalog.describe_table(name)  # validates the name against the catalog
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    fname = export.safe_filename(table.name, "csv")
    return StreamingResponse(
        export.stream_csv(f'SELECT * FROM {engine.SCHEMA}."{table.name}"'),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/tables/{name}/export.xlsx")
def export_table_xlsx(name: str) -> Response:
    """Download a whole table as Excel."""
    try:
        table = catalog.describe_table(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc
    fname = export.safe_filename(table.name, "xlsx")
    content = export.build_xlsx(f'SELECT * FROM {engine.SCHEMA}."{table.name}"')
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
