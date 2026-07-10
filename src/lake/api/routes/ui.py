"""htmx UI: server-rendered pages plus fragment endpoints.

The whole frontend is served from this one FastAPI app — no separate Node server,
no build step. Pages are full HTML; the query form posts to `/query/run` and gets
back an HTML fragment that htmx swaps in place. The one genuinely-streaming piece
(the AI answer) uses a little fetch reader on the client; everything else is plain
server-rendered HTML.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from lake.api import catalog, engine
from lake.api.sql_guard import UnsafeQuery, validate
from lake.core.logging import get_logger
from lake.registry import load_sources

log = get_logger(__name__)
router = APIRouter()

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

MAX_BARS = 30

#: Contact-form bounds. Caps keep a hostile payload out of the log and the reply.
MIN_MESSAGE = 10
MAX_MESSAGE = 4_000
MAX_NAME = 120
MAX_EMAIL = 254  # RFC 5321 maximum forward-path length


def _bar_spec(result: dict) -> list[dict] | None:
    """Turn a (label, number) result into bars. Single series, direct-labelled.

    Only when the shape actually fits: two columns, one text label and one number,
    few enough rows to read. Two numeric measures must never share an axis, so
    those stay a plain table.
    """
    cols, rows = result["columns"], result["rows"]
    if len(cols) != 2 or not rows or len(rows) > MAX_BARS:
        return None
    if not all(r[1] is None or isinstance(r[1], int | float) for r in rows):
        return None
    if not all(isinstance(r[0], str | int | float) for r in rows):
        return None

    values = [(r[1] or 0) for r in rows]
    peak = max((abs(v) for v in values), default=0) or 1
    return [
        {
            "label": str(r[0]),
            "pct": round(abs(r[1] or 0) / peak * 100, 2),
            "display": "∅" if r[1] is None else f"{r[1]:,}",
        }
        for r in rows
    ]


#: Shown when no replica has been built. The page explains itself regardless.
_EMPTY_STATS: dict = {
    "table_count": 0,
    "total_rows": 0,
    "total_columns": 0,
    "built_at": None,
    "tables": [],
}


def _stats() -> dict:
    try:
        return catalog.lake_stats()
    except FileNotFoundError:
        return dict(_EMPTY_STATS)


def _series() -> dict | None:
    """The hero chart. Never worth failing a page over."""
    try:
        return catalog.headline_series()
    except Exception:
        log.warning("ui.series_unavailable", exc_info=True)
        return None


def _sources() -> list[dict]:
    """The source registry, ordered active-first. A broken registry is not fatal."""
    try:
        rows = [
            {
                "source_id": cfg["source_id"],
                "display_name": cfg.get("display_name", cfg["source_id"]),
                "description": cfg.get("description"),
                "kind": cfg.get("kind", "—"),
                "schedule": cfg.get("schedule", "—"),
                "enabled": bool(cfg.get("enabled", False)),
            }
            for cfg in load_sources().values()
        ]
    except Exception:
        log.warning("ui.sources_unavailable", exc_info=True)
        return []
    rows.sort(key=lambda s: (not s["enabled"], s["source_id"]))
    return rows


@router.get("/", response_class=HTMLResponse)
def page_tables(request: Request) -> HTMLResponse:
    """The public dashboard: what this is, what's in it, how to reach us."""
    stats = _stats()
    return _TEMPLATES.TemplateResponse(
        request,
        "tables.html",
        {
            "page": "tables",
            "stats": stats,
            "tables": stats["tables"],
            "sources": _sources(),
            "series": _series(),
        },
    )


def _cards_context(q: str, kind: str, status: str) -> dict:
    """Shared by the full page and the htmx fragment, so both filter identically."""
    cards = catalog.dataset_cards(_sources())
    return {
        "cards": catalog.filter_cards(cards, q=q, kind=kind, status=status),
        "total": len(cards),
        "kinds": sorted({c["kind"] for c in cards if c["kind"]}),
        "q": q,
        "kind": kind,
        "status": status,
    }


@router.get("/datasets", response_class=HTMLResponse)
def page_datasets(
    request: Request, q: str = "", kind: str = "", status: str = ""
) -> HTMLResponse:
    """Everything the lake collects, searchable.

    Filters live in the query string, so a filtered view is a link you can send
    someone, and the form still works with JavaScript off.
    """
    context = _cards_context(q, kind, status)
    context.update({"page": "datasets", "stats": _stats()})
    return _TEMPLATES.TemplateResponse(request, "datasets.html", context)


@router.get("/datasets/cards", response_class=HTMLResponse)
def htmx_dataset_cards(
    request: Request, q: str = "", kind: str = "", status: str = ""
) -> HTMLResponse:
    """The card grid alone, for htmx to swap in as the reader types.

    The browser's address bar must end up showing `/datasets?...`, never this
    endpoint: someone who copies the URL after searching should get the whole
    page back, not a bare grid with no nav. htmx would otherwise push the URL it
    actually fetched, so the page URL is handed back in `HX-Push-Url`.
    """
    query = urlencode({k: v for k, v in (("q", q), ("kind", kind), ("status", status)) if v})
    response = _TEMPLATES.TemplateResponse(
        request, "_dataset_cards.html", _cards_context(q, kind, status)
    )
    response.headers["HX-Push-Url"] = f"/datasets?{query}" if query else "/datasets"
    return response


@router.get("/about", response_class=HTMLResponse)
def page_about(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "about.html",
        {"page": "about", "stats": _stats(), "sources": _sources()},
    )


@router.get("/contact", response_class=HTMLResponse)
def page_contact(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "contact.html", {"page": "contact"})


@router.get("/table/{name}", response_class=HTMLResponse)
def page_table_detail(request: Request, name: str) -> HTMLResponse:
    try:
        table = catalog.describe_table(name)
        profile = {p["column_name"]: p for p in catalog.column_profile(name)}
        sample = engine.run_query(f'SELECT * FROM {engine.SCHEMA}."{table.name}"', limit=20)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc

    # not "tables": a dataset page is below the nav, so nothing in it is current
    return _TEMPLATES.TemplateResponse(
        request,
        "table_detail.html",
        {"page": "table", "table": table, "profile": profile, "result": sample},
    )


@router.get("/query", response_class=HTMLResponse)
def page_query(request: Request, sql: str = "") -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "query.html", {"page": "query", "sql": sql, "result": None}
    )


@router.post("/query/run", response_class=HTMLResponse)
def htmx_query_run(request: Request, sql: str = Form(...)) -> HTMLResponse:
    """Run a query and return only the result fragment for htmx to swap in."""
    context: dict = {"sql": sql}
    try:
        validated = validate(sql, connection=engine.serving())
        result = engine.run_query(validated.sql, limit=5000)  # rows already JSON-safe
        context["result"] = result
        context["bars"] = _bar_spec(result)
    except UnsafeQuery as exc:
        context["error"] = str(exc)
    except engine.QueryTimeout as exc:
        context["error"] = str(exc)
    except Exception as exc:
        context["error"] = f"{type(exc).__name__}: {exc}"

    return _TEMPLATES.TemplateResponse(request, "_query_result.html", context)


@router.post("/contact/send", response_class=HTMLResponse)
def htmx_contact_send(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    message: str = Form(""),
) -> HTMLResponse:
    """Validate a contact message and return the htmx result fragment.

    This does not send mail. There is no mail configuration in this project, and
    quietly wiring an outbound sender into a public form would make the page an
    open relay for spam. The message is logged; the page tells the reader plainly
    to email instead, and gives them the address.
    """
    name, email, message = name.strip(), email.strip(), message.strip()

    errors: list[str] = []
    if not name:
        errors.append("Tell us who you are.")
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > MAX_EMAIL:
        errors.append("That email address doesn't look right.")
    if len(message) < MIN_MESSAGE:
        errors.append(f"Add a little more detail — at least {MIN_MESSAGE} characters.")
    if len(message) > MAX_MESSAGE:
        errors.append(f"That message is too long (max {MAX_MESSAGE:,} characters).")

    if errors:
        return _TEMPLATES.TemplateResponse(
            request, "_contact_result.html", {"errors": errors}, status_code=422
        )

    log.info("ui.contact_message", name=name[:MAX_NAME], email=email, chars=len(message))
    return _TEMPLATES.TemplateResponse(
        request, "_contact_result.html", {"errors": [], "name": name[:MAX_NAME]}
    )


@router.get("/ask", response_class=HTMLResponse)
def page_ask(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "ask.html", {"page": "ask"})
