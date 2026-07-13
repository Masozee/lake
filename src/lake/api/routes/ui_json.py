"""JSON endpoints for the frontend.

The UI in `web/` is a separate TanStack Start app; it fetches these on the server
during SSR, so nothing here needs to be reachable from a browser to work. Every
route reads only the serving replica and the source registry — no Postgres — so
the public site keeps rendering when the catalog database is down.

Nothing here computes anything the data routes could not. It is shaped the way a
page needs it rather than the way a table is stored, which is the whole reason it
is a separate router: `/api/tables/{name}` describes storage, `/api/ui/datasets`
describes what a reader can open.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from lake.api import catalog, engine
from lake.api.routes._errors import message
from lake.core.logging import get_logger
from lake.registry import load_sources

log = get_logger(__name__)
router = APIRouter()

#: Shown when no replica has been built. The page explains itself regardless.
_EMPTY_STATS: dict[str, Any] = {
    "table_count": 0,
    "total_rows": 0,
    "total_columns": 0,
    "built_at": None,
    "tables": [],
}

#: Contact-form bounds. Caps keep a hostile payload out of the log and the reply.
MIN_MESSAGE = 10
MAX_MESSAGE = 4_000
MAX_NAME = 120
MAX_EMAIL = 254  # RFC 5321 maximum forward-path length

#: Points a sparkline is drawn from. More than this and the line is noise at the
#: size it renders. The client draws the path; we send the numbers.
SERIES_POINTS = 240

#: How many of a dataset's children a detail page lists. `gdp_annual` has 260 and
#: `seki_indicators` 108, so this shows all of both — a bigger source would be cut
#: off, and the page says so rather than pretending the list is complete.
CHILDREN = 300


def _stats() -> dict[str, Any]:
    try:
        return catalog.lake_stats()
    except FileNotFoundError:
        return dict(_EMPTY_STATS)


def _sources() -> list[dict[str, Any]]:
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


@router.get("/stats")
def get_stats() -> dict[str, Any]:
    """Lake-wide totals plus the sources behind them — everything the shell needs."""
    return {"stats": _stats(), "sources": _sources()}


@router.get("/overview")
def get_overview() -> dict[str, Any]:
    """The landing page: totals, sources, and the headline series.

    The hero chart is never worth failing the page over — a lake with no
    `gdp_annual` still has a landing page, it just has no chart on it.
    """
    try:
        series = catalog.headline_series()
    except Exception:
        log.warning("ui.series_unavailable", exc_info=True)
        series = None

    return {"stats": _stats(), "sources": _sources(), "series": series}


#: Cards per page. There are 4,030 of them — 112 statistical tables and the 3,918
#: series inside them — and rendering all of them at once is ~1.8 MB of JSON and
#: four thousand DOM nodes. The reader gets a page; the filters run over the whole
#: set on the server.
PAGE_SIZE = 60
MAX_PAGE_SIZE = 200


@lru_cache(maxsize=1)
def _all_cards(replica_stamp: float) -> list[dict[str, Any]]:
    """Every card, cached against the replica's build time.

    Building them is a full scan of a million-row table — about a second — and it
    is the same answer for every reader until the replica is rebuilt. `lake serve
    build` swaps a new file into place, so its mtime changing is exactly the signal
    that the cache is stale. Keying on it means the cache never has to be
    invalidated by hand, and never serves data from a replica that is gone.
    """
    return catalog.dataset_cards(_sources())


def _cards() -> list[dict[str, Any]]:
    path = engine.replica_path()
    stamp = path.stat().st_mtime if path.exists() else 0.0
    return _all_cards(stamp)


def cards_for_admin() -> list[dict[str, Any]]:
    """The same cached cards, for the admin Data browser.

    Shared rather than rebuilt: it is a one-second scan of a million-row table, and
    there is no version of this where the admin panel should disagree with the
    public catalogue about what a dataset is.
    """
    return _cards()


@router.get("/datasets")
def get_datasets(
    q: str = "",
    kind: str = "",
    status: str = "",
    section: str = "",
    level: str = "",
    page: int = 0,
    size: int = PAGE_SIZE,
) -> dict[str, Any]:
    """Dataset cards, filtered and paged.

    A dataset is a thing you can open, query, and export. SEKI is one *source*
    publishing 108 statistical *tables*, and each of those is a table of *series* —
    "Uang Beredar dan Faktor-Faktor yang Mempengaruhinya" is one table, and "Aktiva
    Dalam Negeri Bersih" is one of its 59 series. All three levels below the source
    are datasets, which is why there are 4,030 cards and not 112.

    The filter facets and the totals are computed from the *unfiltered* set, so
    narrowing by section never makes the other sections disappear from the dropdown,
    and "60 of 4,030" does not silently become "60 of 60".
    """
    cards = _cards()
    matched = catalog.filter_cards(
        cards, q=q, kind=kind, status=status, section=section, level=level
    )

    size = max(1, min(size, MAX_PAGE_SIZE))
    page = max(0, page)
    start = page * size

    return {
        "cards": matched[start : start + size],
        "matched": len(matched),
        "total": len(cards),
        "page": page,
        "size": size,
        "pages": (len(matched) + size - 1) // size,
        # The three rungs below a source, with their real counts, so the UI can say
        # "2 datasets · 109 groups · 4,178 series" rather than inventing the numbers.
        #
        # `queryable` is the guard: a source that has collected nothing yet also gets
        # a card, with no group and no series, and counting it as a dataset would
        # claim we serve data we do not have.
        "levels": {
            "dataset": sum(
                1 for c in cards if c["queryable"] and not c["group_id"] and not c["series"]
            ),
            "group": sum(1 for c in cards if c["group_id"] and not c["series"]),
            "series": sum(1 for c in cards if c["series"]),
        },
        "kinds": sorted({c["kind"] for c in cards if c["kind"]}),
        "sections": sorted({c["section"] for c in cards if c["section"]}),
        "stats": _stats(),
    }


@router.get("/dataset/{thing_id}")
def get_dataset(thing_id: str) -> dict[str, Any]:
    """One dataset, at whichever rung it sits on.

    A dataset, a statistical table inside one, and a single series are all things a
    reader can open — so they all come through here, and the merge means all three
    are the same query with a longer filter.

    `children` is what is inside it: a dataset's statistical tables, a table's
    series, and — for a series, the bottom rung — nothing.
    """
    try:
        dataset = catalog.describe_dataset(thing_id)
        sample = catalog.dataset_sample(thing_id)
        series = catalog.dataset_series(thing_id, limit_points=SERIES_POINTS)
        children = catalog.children_of(thing_id, limit=CHILDREN)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="no serving replica built yet") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=message(exc)) from exc

    source = next((s for s in _sources() if s["source_id"] == dataset.get("source_id")), None)
    return {
        "dataset": dataset,
        "sample": sample,
        "series": series,
        "children": children,
        "source": source,
    }


@router.get("/table/{name}")
def get_table_detail(name: str) -> dict[str, Any]:
    """A raw DuckDB table: its schema, per-column profile, and a sample.

    Distinct from `/api/ui/dataset/{id}` on purpose. This is the storage view —
    what the columns are and what lives in them — and it is what the reader wants
    before writing SQL against the table.
    """
    try:
        table = catalog.describe_table(name)
        profile = {p["column_name"]: p for p in catalog.column_profile(name)}
        sample = engine.run_query(f'SELECT * FROM {engine.SCHEMA}."{table.name}"', limit=20)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="no serving replica built yet") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip('"')) from exc

    return {
        "table": {
            "name": table.name,
            "row_count": table.row_count,
            "columns": [
                {"name": c.name, "type": c.type, "nullable": c.nullable} for c in table.columns
            ],
        },
        "profile": profile,
        "sample": sample,
    }


class ContactMessage(BaseModel):
    name: str = Field(default="")
    email: str = Field(default="")
    message: str = Field(default="")


@router.post("/contact")
def post_contact(body: ContactMessage) -> dict[str, Any]:
    """Validate a contact message and record it.

    This does not send mail. There is no mail configuration in this project, and
    quietly wiring an outbound sender into a public form would make the page an
    open relay for spam. The message is logged; the page tells the reader plainly
    to email instead, and gives them the address.
    """
    name, email, message = body.name.strip(), body.email.strip(), body.message.strip()

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
        # 422: well-formed request, unacceptable content. The UI renders the list.
        raise HTTPException(status_code=422, detail={"errors": errors})

    log.info("ui.contact_message", name=name[:MAX_NAME], email=email, chars=len(message))
    return {"ok": True, "name": name[:MAX_NAME]}
