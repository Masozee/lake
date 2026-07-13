"""Data routes: the catalog, and the rows behind an id.

Everything here is a GET. There is deliberately no route that writes — not disabled,
absent. The engine could not honour one anyway.

There is also no route that takes SQL. A reader asks for the rows of a *thing* — a
dataset, a statistical table inside one, or a single series — by the same short id its
page is addressed by:

    GET /api/data/i5demefo/rows?period=gte:2000&limit=500

The alternative is spelling out the keys behind that id
(`?dataset_id=seki_indicators&group_id=I.1.&series=Uang+Beredar+Luas%28M2%29`), and
nobody wants to type, escape, or cite that. The id is a hash of those keys, so it
survives a rebuild — a link anyone shared still resolves, with nothing to migrate.

Filters compose on top of it. An id fixes the slice; a filter narrows within it. Any
query param naming a real column is a filter, and its value may carry an operator
prefix — `gte:2000`, `contains:food`, `in:annual,quarterly` — with no prefix meaning
equality. Everything else is one of the reserved controls below, and a param that is
neither raises rather than being ignored: a filter that silently does nothing hands
back the whole thing to someone who asked for part of it, and they cannot tell.

There is one endpoint for the rows and three ways of writing them down:

    GET /api/data/i5demefo/rows                     -> JSON, one page
    GET /api/data/i5demefo/rows   Accept: text/csv  -> CSV, everything
    GET /api/data/i5demefo/rows?format=csv          -> the same, for a client that
                                                       cannot set a header

The format is a property of the request, not of the path — a `.csv` on the end of a
URL is a filename pretending to be a resource. See `_format_for` for why both
mechanisms exist.

`lake.api.rows` compiles the read into SQL. Nothing a caller sends reaches the SQL text
— the id and the column names are looked up in the catalog, and the values are bound.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse

from lake.api import catalog, engine, export, rows
from lake.api.routes._errors import message
from lake.api.schemas import ColumnInfo, TableInfo
from lake.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter()

#: Query params that are controls rather than filters. Everything else in the query
#: string has to name a column of the table the id resolves to.
_RESERVED = frozenset(
    {
        "format",
        "limit",
        "offset",
        "sort",
        "desc",
        "select",
        "filename",
        "group_by",
        "measure",
        "agg",
    }
)


def _not_found(exc: KeyError) -> HTTPException:
    # `message` unwraps KeyError's own repr quoting. A 404 that echoed the caller's
    # string back would reflect whatever they sent — and it tells them nothing they did
    # not already know, since they are the ones who chose the id.
    return HTTPException(status_code=404, detail=message(exc))


def _bad_request(exc: rows.BadRequest) -> HTTPException:
    # 422: the request is well-formed, the filter is not. The message names the columns
    # that do exist, so a caller can fix it without reading the docs.
    return HTTPException(status_code=422, detail=str(exc))


def _read_spec(request: Request, thing_id: str) -> dict[str, Any]:
    """Turn a query string into a validated read of one thing.

    The id is resolved first, because the columns of the table behind it are what
    decide whether a param is a filter or a mistake.
    """
    try:
        table, _ = rows.pinned(thing_id)  # raises KeyError on an unknown id
        columns = {c.name for c in catalog.describe_table(table).columns}
    except KeyError as exc:
        raise _not_found(exc) from exc

    params = dict(request.query_params)

    unknown = [k for k in params if k not in _RESERVED and k not in columns]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown column {', '.join(sorted(unknown))!r}. "
                f"columns: {', '.join(sorted(columns))}"
            ),
        )

    requested = params.get("select", "")
    select = [c.strip() for c in requested.split(",") if c.strip()] or None

    return {
        "select": select,
        "filters": [
            rows.parse_filter(name, value)
            for name, value in params.items()
            if name not in _RESERVED
        ],
        "sort": params.get("sort") or None,
        # `?desc` with no value, `?desc=1`, and `?desc=true` all mean descending.
        # `?desc=false` and `?desc=0` do not. Anything else is a typo, not a request.
        "descending": params.get("desc", "").lower() in ("", "1", "true", "yes")
        if "desc" in params
        else False,
    }


# -- catalog ------------------------------------------------------------------


@router.get("/tables", response_model=list[str])
def get_tables() -> list[str]:
    return catalog.list_tables()


@router.get("/tables/{name}", response_model=TableInfo)
def get_table(name: str) -> TableInfo:
    try:
        table = catalog.describe_table(name)
    except KeyError as exc:
        raise _not_found(exc) from exc
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
        raise _not_found(exc) from exc


@router.get("/tables/{name}/profile")
def get_profile(name: str) -> dict:
    try:
        return {"table": name, "profile": catalog.column_profile(name)}
    except KeyError as exc:
        raise _not_found(exc) from exc


# -- the rows behind an id ----------------------------------------------------
#
# One resource, three representations. `/rows` is the rows; JSON, CSV and Excel are
# three ways of writing them down, and which one you get is a property of the request
# rather than of the path. A `.csv` on the end of a URL is a filename pretending to be
# a resource.

#: What a caller may ask for. JSON is the default because a client that states no
#: preference wants the thing it can parse without a library.
FORMATS = frozenset({"json", "csv", "xlsx"})

_MEDIA = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _format_for(request: Request, explicit: str | None) -> str:
    """Which representation to send.

    `Accept` is the REST mechanism and it works — but it cannot be the *only* one, and
    the reason is not philosophical. `pandas.read_csv(url)` sends no Accept header at
    all. Under Accept-only negotiation it would receive JSON, and `read_csv` parses
    JSON as CSV **without raising**: the reader gets an empty DataFrame whose one column
    name is a blob of JSON, and no reason why. A browser's `<a href>` download and R's
    `read.csv` fail the same way, quietly.

    So `?format=` exists for the clients that physically cannot set a header, and it
    beats Accept when both are present — a caller who typed it was being explicit. It is
    not a shortcut around REST; it is what keeps the one-line snippet honest.
    """
    if explicit is not None:
        if explicit not in FORMATS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown format {explicit!r}. known: {', '.join(sorted(FORMATS))}",
            )
        return explicit

    accept = request.headers.get("accept", "")
    if "text/csv" in accept:
        return "csv"
    if "spreadsheetml" in accept or "application/vnd.ms-excel" in accept:
        return "xlsx"
    return "json"


def _disposition(thing_id: str, request: Request, extension: str) -> dict[str, str]:
    """The headers that make a response a *download*.

    The file keeps its extension even though the URL has lost one — a file on disk
    should say what it is. And it is named after the thing: a reader downloading the M2
    series wants `M2.csv`, not a fourth `observations.csv`.
    """
    stem = request.query_params.get("filename") or rows.default_filename(thing_id)
    filename = export.safe_filename(stem, extension)
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        # Without this, a cache in front of the API can hand a CSV body to the next
        # client that asked for JSON. The representation varies by header, so say so.
        "Vary": "Accept",
    }


@router.get("/data/{thing_id}/rows")
def get_rows(
    request: Request,
    thing_id: str,
    format: str | None = Query(
        None, description=f"one of: {', '.join(sorted(FORMATS))}. Overrides Accept."
    ),
) -> Any:
    """A thing's rows, filtered and sorted by the database.

    JSON by default: one page, with `total` (the count *after* filtering, so a client
    can page without guessing) and `has_more` (so it need not do the sum).

    `?format=csv` or `Accept: text/csv` gives the same rows as a file — and a file is
    the data, not a screen, so it defaults to *everything* that matches rather than to a
    page. An explicit `?limit=` is still honoured in either representation.
    """
    chosen = _format_for(request, format)
    spec = _read_spec(request, thing_id)

    # Read the page from the raw query string rather than a FastAPI default, because
    # "absent" and "1000" have to be distinguishable: the default depends on what is
    # being asked for. A CSV that silently stopped at 1,000 rows would be exactly the
    # class of bug this whole design is guarding against — quiet, and wrong.
    try:
        limit = int(request.query_params["limit"]) if "limit" in request.query_params else None
        offset = int(request.query_params.get("offset", 0))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="limit and offset must be integers") from exc
    if (limit is not None and limit < 1) or offset < 0:
        raise HTTPException(status_code=422, detail="limit must be >= 1 and offset >= 0")

    try:
        if chosen == "json":
            body = rows.rows(
                thing_id,
                limit=min(limit or rows.DEFAULT_LIMIT, rows.MAX_LIMIT),
                offset=offset,
                **spec,
            )
            return JSONResponse(jsonable_encoder(body), headers={"Vary": "Accept"})

        sql, params = rows.export_sql(
            thing_id,
            limit=limit or rows.EXPORT_MAX_ROWS,
            offset=offset,
            **spec,
        )
    except rows.BadRequest as exc:
        raise _bad_request(exc) from exc
    except KeyError as exc:
        raise _not_found(exc) from exc
    except engine.QueryTimeout as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc

    headers = _disposition(thing_id, request, chosen)

    if chosen == "csv":
        # Streams: the server never holds the whole result, so a client can pull a
        # million rows without a million rows existing here at once.
        return StreamingResponse(
            export.stream_csv(sql, params), media_type=_MEDIA["csv"], headers=headers
        )

    try:
        content = export.build_xlsx(sql, params)
    except engine.QueryTimeout as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc
    return Response(content, media_type=_MEDIA["xlsx"], headers=headers)


@router.get("/data/{thing_id}/aggregate")
def get_aggregate(
    request: Request,
    thing_id: str,
    group_by: str = Query(..., description="comma-separated columns to group by"),
    agg: str = Query("count", description=f"one of: {', '.join(sorted(rows.AGGREGATES))}"),
    measure: str | None = Query(None, description="the column to aggregate; not needed for count"),
    limit: int = Query(default=100, ge=1, le=rows.MAX_GROUPS),
) -> dict[str, Any]:
    """A GROUP BY, without a query language.

    `GET /api/data/i5demefo/aggregate?group_by=year&agg=sum&measure=value` — the
    yearly total of one series, and the thing's own filters are applied first.
    """
    spec = _read_spec(request, thing_id)
    keys = [c.strip() for c in group_by.split(",") if c.strip()]
    try:
        return rows.aggregate(
            thing_id,
            group_by=keys,
            agg=agg,
            measure=measure,
            filters=spec["filters"],
            sort=spec["sort"],
            # An aggregate is a ranking by default — biggest first is what a bar chart
            # is for. `?sort=x&desc=false` still climbs.
            descending=spec["descending"] if "desc" in request.query_params else True,
            limit=limit,
        )
    except rows.BadRequest as exc:
        raise _bad_request(exc) from exc
    except KeyError as exc:
        raise _not_found(exc) from exc
    except engine.QueryTimeout as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc
