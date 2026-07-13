"""Admin routes. Everything here requires a session; nothing here is public.

The panel is the only part of the system that writes, so this is the only router
where a bug can do lasting damage. Two structural defences:

* Every route below — except `login` — takes `principal` as a dependency, so
  "forgot to check auth" is not a mistake that compiles. There is no route that
  authenticates itself inline.

* Every write calls `auth.record(...)`. An unaudited change to the source registry
  should be impossible to introduce without deliberately deleting that line.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from lake.api import catalog, engine
from lake.api.admin import auth, browse, config_editor, monitor
from lake.api.admin.auth import COOKIE_NAME, AuthError, Principal
from lake.api.admin.config_editor import InvalidConfig
from lake.api.routes import ui_json
from lake.api.routes._errors import message
from lake.core.logging import get_logger
from lake.metadata.models import User
from lake.metadata.session import session_scope
from lake.settings import get_settings

log = get_logger(__name__)
router = APIRouter()


def principal(
    lake_admin: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> Principal:
    """The logged-in admin, or a 401. Every route below depends on this."""
    who = auth.resolve(lake_admin)
    if who is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return who


Admin = Annotated[Principal, Depends(principal)]


# --- session -----------------------------------------------------------------


class Credentials(BaseModel):
    email: str
    password: str


@router.post("/login")
def post_login(body: Credentials, request: Request, response: Response) -> dict[str, Any]:
    """Sign in. Sets an httpOnly cookie; the token never reaches JavaScript."""
    client = request.client.host if request.client else ""
    try:
        token = auth.authenticate(
            body.email,
            body.password,
            user_agent=request.headers.get("user-agent", ""),
            ip=client,
        )
    except AuthError as exc:
        # 401 with the same body for every failure. Distinguishing "no such user"
        # from "wrong password" is a way to find out who has an account.
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,  # no script can read it, so XSS cannot steal the session
        samesite="lax",  # does not ride along on a cross-site request
        # Secure in production only: it would break plain-http localhost in dev,
        # and the panel is reached over Tailscale or a TLS proxy in prod anyway.
        secure=get_settings().env == "production",
        max_age=int(auth.SESSION_TTL.total_seconds()),
        path="/",
    )
    who = auth.resolve(token)
    assert who is not None  # we just made it
    return {"email": who.email, "display_name": who.display_name}


@router.post("/logout")
def post_logout(
    response: Response,
    lake_admin: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> dict[str, bool]:
    auth.logout(lake_admin)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def get_me(who: Admin) -> dict[str, str]:
    """Who am I. The frontend calls this to decide whether to show the login form."""
    return {"email": who.email, "display_name": who.display_name}


# --- monitoring (reads) ------------------------------------------------------


@router.get("/overview")
def get_overview(who: Admin) -> dict[str, Any]:
    """Everything the landing view of the panel needs, in one round trip."""
    return {
        "health": monitor.health(),
        "freshness": monitor.freshness(),
        "runs": monitor.recent_runs(limit=25),
        "errors": monitor.recent_errors(limit=25),
        "quiet": monitor.quiet_sources(),
    }


@router.get("/runs")
def get_runs(who: Admin, limit: int = 100, source_id: str | None = None) -> list[dict[str, Any]]:
    return monitor.recent_runs(limit=min(limit, 500), source_id=source_id)


@router.get("/errors")
def get_errors(who: Admin, days: int = 7, limit: int = 100) -> list[dict[str, Any]]:
    return monitor.recent_errors(days=days, limit=min(limit, 500))


@router.get("/storage")
def get_storage(who: Admin) -> dict[str, Any]:
    return {"files": monitor.storage(), "datasets": monitor.datasets()}


@router.get("/audit")
def get_audit(who: Admin, limit: int = 100) -> list[dict[str, Any]]:
    return monitor.audit(limit=min(limit, 500))


# --- the data browser --------------------------------------------------------


def _level_of(card: dict[str, Any]) -> str:
    """Which rung a card sits on: how many of the three keys it carries.

    `source` is not a rung of the data — it is a source that has collected nothing, so
    it has no id and nothing inside it. It still gets a row, because a source that is
    not producing is exactly what an admin opens this page to find.
    """
    if not card["id"]:
        return "source"
    if card["series"]:
        return "series"
    return "group" if card["group_id"] else "dataset"


def _entry(card: dict[str, Any]) -> dict[str, Any]:
    level = _level_of(card)
    entry = {
        "id": card["id"],
        "title": card["title"],
        "level": level,
        "parent_title": card["parent_title"],
        "row_count": card["row_count"],
        "unit": card["unit"],
        "freq": card["freq"],
        "first_period": card["first_period"],
        "last_period": card["last_period"],
        # A dataset or a group has things inside it. A series is the bottom rung, and a
        # source that has collected nothing has no bottom at all — opening either shows
        # what it has rather than another list.
        "openable": level in ("dataset", "group"),
    }

    if level not in ("dataset", "source"):
        # Below the dataset rung the source is inherited, and repeating it on 4,178
        # series rows would be noise: you already know what you drilled into.
        return entry

    # The one rung where a source is a fact rather than an ancestor. The row is titled
    # by what the source is CALLED — "World Bank GDP indicator" — because `gdp_annual`
    # is our internal key and nobody is looking for that. The key stays underneath, for
    # whoever is writing a query.
    return {
        **entry,
        "title": card["source_name"] or card["title"],
        "dataset_id": card["dataset_id"],
        "description": card["description"],
        "source_id": card["source_id"],
        "kind": card["kind"],
        "schedule": card["schedule"],
        "enabled": card["enabled"],
        "last_collected": card["last_collected"],
    }


#: How many children one drill-down step returns. SEKI's biggest statistical table
#: holds a few dozen series, so this is generous — but `seki_indicators` itself has
#: 108 tables and `gdp_annual` has 260 countries, and both must page rather than
#: dump.
CHILDREN_PAGE = 100
MAX_CHILDREN = 500


@router.get("/data")
def get_data_children(
    who: Admin,
    parent: str = "",
    q: str = "",
    page: int = 0,
    size: int = CHILDREN_PAGE,
) -> dict[str, Any]:
    """One level of the data hierarchy: the children of `parent`.

    A drill-down, not a dump. There are ~4,300 datasets, tables, and series between
    them, and a flat list of that is not something a person can browse — so the page
    asks for one rung at a time:

        (no parent)   -> the raw table, and every dataset
        wm72qlsa      -> a dataset's groups
        4qkxwlbo      -> a group's series

    A series has no children; opening one shows its rows instead.

    `parent` is a short id, not a path. Which means the parent-child test is not a
    string comparison — it resolves both sides to their real keys and asks whether
    the child extends the parent by exactly one rung.

    `q` searches *within* the current level, so narrowing SEKI's 108 groups does not
    also drag in the World Bank's countries.
    """
    try:
        cards = ui_json.cards_for_admin()
    except FileNotFoundError:
        # No replica built yet. An empty list, not a 500 — the page says so itself.
        return _empty()

    parent = parent.strip()

    if not parent:
        # The root: the raw table itself, then what each source published.
        try:
            raw = [
                {
                    # The raw table is the one thing addressed by name rather than by
                    # id: it is not a dataset, it is what all of them are views of.
                    "id": t.name,
                    "title": t.name,
                    "level": "raw",
                    "parent_title": None,
                    "row_count": t.row_count,
                    "unit": None,
                    "freq": None,
                    "first_period": None,
                    "last_period": None,
                    # The raw table is browsed, not drilled into: its children are
                    # the datasets, which are already listed beside it.
                    "openable": False,
                }
                for t in (catalog.describe_table(n) for n in catalog.list_tables())
            ]
        except FileNotFoundError:
            return _empty()

        # Every dataset, AND every source that has published nothing yet. A source with
        # no rows is exactly the one an admin opens this page to find — leaving it out
        # would mean the page can only ever show you what is already working.
        published = [_entry(c) for c in cards if _level_of(c) == "dataset"]
        pending = [_entry(c) for c in cards if _level_of(c) == "source"]

        children = _with_freshness(raw + published + pending)
        crumbs: list[dict[str, str]] = []
    else:
        try:
            thing = catalog.resolve(parent)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=message(exc)) from exc

        crumbs = _crumbs(thing)
        if thing.level == "series":
            return _empty(crumbs=crumbs)  # a series is the bottom rung

        want = (thing.dataset_id, thing.group_id, thing.series)
        children = [
            _entry(c)
            for c in cards
            if c["id"] and _is_direct_child((c["dataset_id"], c["group_id"], c["series"]), want)
        ]

    needle = q.strip().lower()
    if needle:
        children = [c for c in children if needle in _haystack(c)]

    size = max(1, min(size, MAX_CHILDREN))
    page = max(0, page)
    start = page * size

    return {
        "items": children[start : start + size],
        "total": len(children),
        "page": page,
        "size": size,
        "pages": (len(children) + size - 1) // size,
        "crumbs": crumbs,
    }


#: What a filter is matched against. Everything the row actually shows — so a reader
#: searching "world bank" finds the dataset whose title says so, and one searching
#: "inflation" finds it in a description they can see on screen.
_FINDABLE = ("title", "description", "dataset_id", "source_id")


def _haystack(entry: dict[str, Any]) -> str:
    return " ".join(str(entry[f]) for f in _FINDABLE if entry.get(f)).lower()


def _with_freshness(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whether each source is inside its collection SLA — the thing an admin came for.

    Read from the catalog's `v_freshness` view, which the alerting CLI reads too, so the
    page and the pager can never disagree about what "stale" means.

    Postgres is not on the path for anything else this page does: the drill-down reads
    the serving replica alone, and it must keep answering when the catalog database is
    down. So a failure here costs the freshness column, not the page — and the column is
    *absent* rather than green, because "we could not check" and "it is fine" are not
    the same statement and the second one is a lie.
    """
    if not any(e.get("source_id") for e in entries):
        return entries

    try:
        by_source = {row["source_id"]: row for row in monitor.freshness()}
    except Exception:
        log.warning("admin.freshness_unavailable", exc_info=True)
        return entries

    out = []
    for entry in entries:
        row = by_source.get(entry.get("source_id") or "")
        if not row:
            out.append(entry)
            continue
        out.append(
            {
                **entry,
                "is_stale": row["is_stale"],
                "last_success_at": row["last_success_at"],
                "hours_since_success": row["hours_since_success"],
                "sla_hours": row["freshness_sla_hours"],
                # The registry says whether a source is *meant* to be running. A paused
                # source is not stale — it is off on purpose — and calling it stale
                # would page someone at 3am for a decision they made themselves.
                "enabled": row["enabled"],
            }
        )
    return out


Triple = tuple[str, str | None, str | None]


def _is_direct_child(child: Triple, parent: Triple) -> bool:
    """Is `child` exactly one rung below `parent`?

    Both are (dataset, group, series). A child extends the parent by filling in the
    next empty slot and matching everything above it:

        parent ('seki_indicators', None,   None)
          child ('seki_indicators', 'I.1.', None)              -> yes, a group
          child ('seki_indicators', 'I.1.', 'M2')              -> no, a grandchild
          child ('gdp_annual',      'NY.…', None)              -> no, another dataset

        parent ('seki_indicators', 'I.1.', None)
          child ('seki_indicators', 'I.1.', 'M2')              -> yes, a series
          child ('seki_indicators', 'I.10.', 'M2')             -> no, another group's

    The keys are compared, never the ids — an id is a hash, and two hashes tell you
    nothing about whether one thing is inside the other.
    """
    dataset, group, series = child
    p_dataset, p_group, p_series = parent

    if p_series is not None:
        return False  # a series is the bottom rung; nothing is below it
    if dataset != p_dataset:
        return False

    if p_group is None:
        return group is not None and series is None  # a dataset's children: its groups
    return group == p_group and series is not None  # a group's children: its series


def _empty(crumbs: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "items": [],
        "total": 0,
        "page": 0,
        "size": CHILDREN_PAGE,
        "pages": 0,
        "crumbs": crumbs or [],
    }


def _crumbs(thing: catalog.Thing) -> list[dict[str, str]]:
    """The trail back up, so a reader three levels deep knows where they are.

    This is the price of an opaque id: `wm72qlsa` tells a reader nothing, so the
    page has to. Titles for the reader, ids for the links.
    """
    return catalog.crumbs(thing)


class ColumnFilter(BaseModel):
    column: str
    op: str = "contains"
    value: Any = None


class BrowseRequest(BaseModel):
    """A page of a table. POST rather than GET because the filters are a list of
    objects, and cramming that into a query string produces something neither
    readable nor reliably escaped."""

    page: int = Field(default=0, ge=0)
    size: int = Field(default=browse.DEFAULT_PAGE_SIZE, ge=1, le=browse.MAX_PAGE_SIZE)
    sort: str | None = None
    descending: bool = False
    filters: list[ColumnFilter] = Field(default_factory=list)


#: Points the detail page's chart is drawn from. More than this and the line is
#: noise at the size it renders.
SERIES_POINTS = 240

#: How many children a detail page lists. `gdp_annual` has 260 and `seki_indicators`
#: 108, so this shows all of both.
CHILDREN = 300


@router.get("/data/{thing_id}/detail")
def get_data_detail(thing_id: str, who: Admin) -> dict[str, Any]:
    """Everything about one dataset, group, or series.

    What the drill-down list cannot say: where it came from, how far back it runs,
    how much of it is missing, what its numbers look like, what is inside it, and what
    sits beside it. An id is opaque, so this is the page that tells a reader what they
    are looking at.

    One request rather than five: the page is useless without any of them, and five
    round trips means five chances to render half of it.
    """
    try:
        return {
            **catalog.describe_dataset(thing_id),
            # The line itself. A series is a shape before it is a table of numbers,
            # and an admin scanning for a break in the data sees it in the chart long
            # before they would find it in the grid.
            "points": catalog.dataset_series(thing_id, limit_points=SERIES_POINTS),
            "children": catalog.children_of(thing_id, limit=CHILDREN),
            # What sits beside it. A series has nothing inside it, and without this
            # its page is a cul-de-sac: the reader who wants the next of 59 series has
            # to navigate back to a list to get there.
            "siblings": catalog.siblings_of(thing_id, limit=CHILDREN),
            # Where this API answers from, as the outside world reaches it — so the
            # copy-paste snippets carry a URL that works off this machine. The page
            # cannot know this; only the server does.
            "api_url": get_settings().api_public_url.rstrip("/"),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="no serving replica built yet") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=message(exc)) from exc


@router.post("/data/{thing_id}")
def post_browse(thing_id: str, body: BrowseRequest, who: Admin) -> dict[str, Any]:
    """One page of one dataset, sorted and filtered *by the database*.

    `seki_indicators` is 970,700 rows. The page the screen shows is 25 of them, and
    that is exactly how many cross the wire — the sort and the filters are compiled
    into SQL, never applied in the browser.
    """
    try:
        return browse.browse(
            thing_id,
            page=body.page,
            size=body.size,
            sort=body.sort,
            descending=body.descending,
            filters=[f.model_dump() for f in body.filters],
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="no serving replica built yet") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=message(exc)) from exc
    except browse.BadFilter as exc:
        # 422: the request is well-formed, the filter is not. The message names the
        # columns that do exist, so the UI can say something useful.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except engine.QueryTimeout as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc


# --- settings ----------------------------------------------------------------


@router.get("/settings")
def get_settings_view(who: Admin) -> dict[str, Any]:
    """The runtime settings, as the process actually sees them.

    Read-only, and deliberately partial: these come from /etc/lake/lake.env via
    systemd, so changing them is a deploy, not a form. Every secret is reported as
    a boolean — whether it is set, never what it is. A panel that can display an
    API key is a panel that can leak one.
    """
    s = get_settings()
    return {
        "env": s.env,
        "nas_root": str(s.nas_root),
        "raw_root": str(s.raw_root),
        "processed_root": str(s.processed_root),
        "staging_root": str(s.staging_root),
        "log_dir": str(s.log_dir),
        "log_level": s.log_level,
        "sources_config": str(s.sources_config),
        "alert_enabled": s.alert_enabled,
        "api_rate_limit_enabled": s.api_rate_limit_enabled,
        "api_rate_catalog_per_min": s.api_rate_catalog_per_min,
        "api_rate_query_per_min": s.api_rate_query_per_min,
        "api_rate_ai_per_min": s.api_rate_ai_per_min,
        # Set or not set. Never the value.
        "anthropic_api_key_set": bool(s.anthropic_api_key),
        "alert_ntfy_url_set": bool(s.alert_ntfy_url),
        # The DSN carries a password in most deployments. Say only that it exists.
        "db_configured": bool(s.db_dsn),
        "note": (
            "These come from the environment (/etc/lake/lake.env via systemd) and "
            "cannot be changed from this page. Editing them is a deploy."
        ),
    }


# --- the source registry (the one real write) --------------------------------


@router.get("/sources")
def get_sources(who: Admin) -> dict[str, Any]:
    """The registry file, verbatim, plus its backups.

    Verbatim text rather than a parsed structure: a round trip through the YAML
    parser would eat every comment, and in this file the comments are the only
    record of *why* a source is configured the way it is.
    """
    path = config_editor.config_path()
    try:
        content = config_editor.read_config()
    except FileNotFoundError as exc:
        # 503, not 500: the service is fine, its configuration is not where it was
        # told to look. `sources_config` defaults to a relative path, so a server
        # started from the wrong directory lands here — say which path was tried,
        # because "not found" without it is a twenty-minute detour.
        raise HTTPException(
            status_code=503,
            detail=(
                f"the source registry is not at {path}. Set LAKE_SOURCES_CONFIG to "
                f"an absolute path, or start the API from the repository root."
            ),
        ) from exc

    return {
        "path": str(path),
        "content": content,
        "backups": config_editor.list_backups(),
    }


class ConfigEdit(BaseModel):
    content: str = Field(min_length=1)


@router.post("/sources/validate")
def post_validate(body: ConfigEdit, who: Admin) -> dict[str, Any]:
    """Check an edit without writing it. What the editor calls as you type."""
    try:
        sources = config_editor.validate(body.content)
    except InvalidConfig as exc:
        return {"ok": False, "errors": exc.errors}
    return {"ok": True, "errors": [], "sources": sorted(sources)}


@router.put("/sources")
def put_sources(body: ConfigEdit, who: Admin) -> dict[str, Any]:
    """Replace the registry.

    Validated before anything touches the disk, backed up before being overwritten,
    written atomically, and audited with the full previous content. A refused edit
    leaves the file exactly as it was.
    """
    previous = config_editor.read_config()
    if previous == body.content:
        return {"ok": True, "unchanged": True, "backup": None}

    try:
        backup = config_editor.write_config(body.content)
    except InvalidConfig as exc:
        # 422: the request is well-formed, the config is not.
        raise HTTPException(status_code=422, detail={"errors": exc.errors}) from exc

    auth.record(
        who,
        "sources.update",
        target=str(config_editor.config_path()),
        # The whole previous file. This is what stands in for the git diff.
        detail={"previous": previous, "backup": backup.name},
    )
    log.info("admin.sources_updated", actor=who.email, backup=backup.name)

    return {
        "ok": True,
        "unchanged": False,
        "backup": backup.name,
        # The catalog still holds the old rows until someone syncs. Say so rather
        # than implying the edit took effect everywhere.
        "note": "Run `lake sync-sources` to push this into the catalog.",
    }


@router.get("/sources/backups/{name}")
def get_backup(name: str, who: Admin) -> dict[str, str]:
    """One backup's content, for previewing before a restore."""
    try:
        return {"name": name, "content": config_editor.read_backup(name)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- users -------------------------------------------------------------------


class NewUser(BaseModel):
    email: str
    display_name: str = ""
    password: str = Field(min_length=auth.MIN_PASSWORD)


@router.get("/users")
def get_users(who: Admin) -> list[dict[str, Any]]:
    with session_scope() as s:
        users = s.scalars(select(User).order_by(User.created_at)).all()
        return [
            {
                "user_id": str(u.user_id),
                "email": u.email,
                "display_name": u.display_name,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat(),
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "is_you": u.user_id == who.user_id,
            }
            for u in users
        ]


@router.post("/users")
def post_user(body: NewUser, who: Admin) -> dict[str, str]:
    """Create an admin. Only an existing admin can — there is no public signup."""
    try:
        user_id = auth.create_user(body.email, body.display_name, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    auth.record(who, "user.create", target=body.email)
    return {"user_id": str(user_id), "email": body.email.strip().lower()}


class ActiveFlag(BaseModel):
    is_active: bool


@router.patch("/users/{user_id}")
def patch_user(user_id: uuid.UUID, body: ActiveFlag, who: Admin) -> dict[str, Any]:
    """Enable or disable an admin. Disabling revokes every session they hold.

    You cannot disable yourself: an admin who locks themselves out of the only
    panel that can unlock them has to go and find a shell, and the whole point of
    the CLI bootstrap is that this should be rare.
    """
    if user_id == who.user_id:
        raise HTTPException(status_code=422, detail="you cannot disable your own account")

    with session_scope() as s:
        user = s.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="no such user")

        # Refuse to disable the last active admin. A panel with no one who can log
        # in is a panel that needs a shell to fix, which is a bad afternoon.
        if not body.is_active:
            active = s.scalar(select(User).where(User.is_active, User.user_id != user_id).limit(1))
            if active is None:
                raise HTTPException(status_code=422, detail="cannot disable the last active admin")

        user.is_active = body.is_active
        email = user.email

    if not body.is_active:
        # What makes "disabled" mean disabled: signed out everywhere, at once.
        auth.revoke_all(user_id)

    auth.record(who, "user.enable" if body.is_active else "user.disable", target=email)
    return {"user_id": str(user_id), "email": email, "is_active": body.is_active}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=auth.MIN_PASSWORD)


@router.post("/password")
def post_password(body: PasswordChange, who: Admin) -> dict[str, bool]:
    """Change your own password. Requires the current one, so a borrowed session
    cannot be turned into a permanent account takeover."""
    # check_password, not authenticate: re-confirming a password must not open a
    # second session as a side effect.
    if not auth.check_password(who.user_id, body.current_password):
        raise HTTPException(status_code=422, detail="current password is wrong")

    with session_scope() as s:
        user = s.get(User, who.user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="no such user")
        user.password_hash = auth.hash_password(body.new_password)

    # Every other browser holding this account is signed out. A password change is
    # what you do when you think someone else has it.
    auth.revoke_all(who.user_id)
    auth.record(who, "user.password_change", target=who.email)
    return {"ok": True}
