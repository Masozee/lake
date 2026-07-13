"""Schema discovery for humans and for the AI.

Everything here goes through `information_schema`, never `PRAGMA`. That is not a
style choice: `PRAGMA database_list` returns the absolute on-disk path of the
database file, and it classifies as a SELECT to DuckDB's parser. Same for the
`pragma_*` and `duckdb_*` table functions. `information_schema` leaks nothing.

An AI agent gets exactly what it needs to write a correct query — table names,
column names, types, row counts, and a sample of values — and nothing about the
machine the data sits on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from lake.api import ids
from lake.api.engine import SCHEMA, jsonable, read_cursor, replica_path, scalar
from lake.settings import get_settings

#: Sample rows shown per table. Enough for an AI to infer value shapes; small
#: enough that a wide table does not blow up the context window.
SAMPLE_ROWS = 5

#: Distinct values listed for a low-cardinality column, so an AI can filter on
#: real values instead of guessing at them.
MAX_DISTINCT = 20
DISTINCT_THRESHOLD = 50


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    columns: list[Column]
    row_count: int


def list_tables() -> list[str]:
    with read_cursor(timeout=5) as cur:
        rows = cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = ?
            ORDER BY table_name
            """,
            [SCHEMA],
        ).fetchall()
    return [r[0] for r in rows]


def _assert_known_table(name: str) -> str:
    """Resolve a caller-supplied name against the real catalog.

    Never interpolate a caller's string into SQL. We look it up, and use the
    catalog's own copy of the name — so an injected identifier cannot survive
    the round trip even if quoting were somehow bypassed.
    """
    for known in list_tables():
        if known == name:
            return known
    raise KeyError(f"unknown table {name!r}")


def describe_table(name: str) -> Table:
    table = _assert_known_table(name)

    with read_cursor(timeout=5) as cur:
        columns = cur.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = ? AND table_name = ?
            ORDER BY ordinal_position
            """,
            [SCHEMA, table],
        ).fetchall()
        row_count = scalar(cur.execute(f'SELECT count(*) FROM {SCHEMA}."{table}"'))

    return Table(
        name=table,
        columns=[Column(name=c, type=t, nullable=(n == "YES")) for c, t, n in columns],
        row_count=row_count,
    )


def sample_table(name: str, limit: int = SAMPLE_ROWS) -> dict[str, Any]:
    table = _assert_known_table(name)
    limit = max(1, min(limit, 100))

    with read_cursor(timeout=10) as cur:
        result = cur.execute(f'SELECT * FROM {SCHEMA}."{table}" LIMIT {limit}')
        columns = [d[0] for d in (result.description or [])]
        rows = result.fetchall()

    return {"table": table, "columns": columns, "rows": [list(r) for r in rows]}


def column_profile(name: str) -> list[dict[str, Any]]:
    """Per-column statistics, via SUMMARIZE. Works on a read-only connection.

    For low-cardinality columns we also list the distinct values, because an AI
    that can see `region IN ('Java','Sumatra')` writes a correct filter, and one
    that cannot writes `region = 'java'` and gets zero rows.
    """
    table = _assert_known_table(name)

    with read_cursor(timeout=30) as cur:
        result = cur.execute(f'SUMMARIZE {SCHEMA}."{table}"')
        columns = [d[0] for d in (result.description or [])]
        summary = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]

        for entry in summary:
            column = entry.get("column_name")
            approx = entry.get("approx_unique")
            if not column or approx is None or approx > DISTINCT_THRESHOLD:
                continue
            values = cur.execute(
                f'SELECT DISTINCT "{column}" FROM {SCHEMA}."{table}" '
                f'WHERE "{column}" IS NOT NULL ORDER BY 1 LIMIT {MAX_DISTINCT}'
            ).fetchall()
            entry["distinct_values"] = [v[0] for v in values]

    return summary


def lake_stats() -> dict[str, Any]:
    """Lake-wide totals for the landing page.

    Read from the serving replica alone — no Postgres — so the public page keeps
    rendering when the catalog database is down. `built_at` is the replica file's
    mtime: `build_replica` swaps a freshly-written file into place, so its mtime
    is the moment the data currently being served was published.
    """
    tables = [describe_table(name) for name in list_tables()]
    built_at: datetime | None = None
    path = replica_path()
    if path.exists():
        built_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    return {
        "table_count": len(tables),
        "total_rows": sum(t.row_count for t in tables),
        "total_columns": sum(len(t.columns) for t in tables),
        "built_at": built_at,
        "tables": [
            {"name": t.name, "row_count": t.row_count, "columns": len(t.columns)} for t in tables
        ],
    }


# --- the merged table --------------------------------------------------------
#
# Every source lands in one table, `lake.observations`, in one shape: at some
# `period`, some `series` had some `value`, in some `unit`. That is as true of
# "Indonesia's GDP in 1998" as of "M2 in May 2026", so both are stored the same
# way and there is one table to query, one shape for the AI to learn, and one grid
# for the browser.
#
# The three rungs below a source are all columns of that table:
#
#   dataset_id   what was published    'gdp_annual', 'seki_indicators'
#   group_id     a group within it     'NY.GDP.MKTP.CD', 'I.1.'
#   series       one line of numbers   'Indonesia', 'Uang Beredar Luas(M2)'
#
# So a rung is not a table of its own — it is a WHERE clause over this one. An id
# resolves to however many of those three keys it names, and every page below is
# the same query with a longer filter.
#
# Every rung is always there. `group_id` used to be NULL for a source that published
# one flat table, which meant every consumer below carried a branch for "a dataset
# whose children are its series" — and a shape that is only sometimes there is a
# shape every caller gets to be wrong about. Each source now names its own groups
# from what its publisher already gives (see lake/transform/runner.py), so the tree
# is dataset -> group -> series, always, and there is nothing to special-case.

#: The one table everything is served from.
OBSERVATIONS = "observations"

#: The columns that carry the three rungs. Named rather than inlined so a query
#: cannot quietly disagree with the resolver about what a dataset is.
DATASET_COLUMN = "dataset_id"
GROUP_COLUMN = "group_id"
SERIES_COLUMN = "series"

#: How to count series across more than one group. A name is not a series: "Lainnya"
#: is the title of 23 of SEKI's, each a different line of numbers in a different
#: table. Counting DISTINCT series would collapse all 23 into one and report 3,895
#: where the lake holds 3,918.
SERIES_COUNT = f"count(DISTINCT ({GROUP_COLUMN}, {SERIES_COLUMN}))"


_MANIFEST_NAME = "_MANIFEST.json"


def last_collected(source_id: str) -> datetime | None:
    """When this source last landed a complete run, from the raw archive.

    Read off the filesystem rather than Postgres, so the public page keeps
    answering when the catalog database is down. A run directory only counts if
    it holds a manifest saying `status: "complete"` — a half-written run is not
    an update. Returns None when the source has never collected anything.
    """
    try:
        base = get_settings().raw_root / f"source={source_id}"
    except Exception:
        return None
    if not base.is_dir():
        return None

    newest: datetime | None = None
    for manifest in base.rglob(_MANIFEST_NAME):
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # an unreadable manifest is not an update either
        if doc.get("status") != "complete":
            continue
        stamp = doc.get("started_at")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if newest is None or when > newest:
            newest = when
    return newest


def datasets_of() -> list[dict[str, Any]]:
    """Every published dataset, with its shape. One grouped scan of the one table.

    A dataset is the top rung: `gdp_annual`, `seki_indicators`. Its `groups` count is
    how many groups it fans out into — 108 for SEKI's statistical tables, 1 for the
    World Bank's single indicator.
    """
    with read_cursor(timeout=15) as cur:
        rows = cur.execute(
            f"""
            SELECT
                {DATASET_COLUMN}                    AS dataset_id,
                any_value(source_id)                AS source_id,
                count(*)                            AS row_count,
                -- Quoted: `groups` is a reserved word in DuckDB, and an unquoted
                -- alias is a parser error rather than a wrong answer — but only when
                -- this query runs, which is a bad place to find out.
                count(DISTINCT {GROUP_COLUMN})      AS "groups",
                {SERIES_COUNT}                      AS series,
                min(period)                         AS first_period,
                max(period)                         AS last_period,
                any_value(unit)                     AS unit,
                any_value(freq)                     AS freq
            FROM {SCHEMA}."{OBSERVATIONS}"
            GROUP BY {DATASET_COLUMN}
            ORDER BY {DATASET_COLUMN}
            """
        ).fetchall()

    return [
        {
            "dataset_id": r[0],
            "source_id": r[1],
            "row_count": r[2],
            "groups": r[3],
            "series": r[4],
            "first_period": r[5],
            "last_period": r[6],
            "unit": r[7],
            "freq": r[8],
        }
        for r in rows
    ]


def groups_of() -> list[dict[str, Any]]:
    """Every group inside every dataset.

    The middle rung, and it is always there: SEKI's 108 statistical tables and the
    World Bank's one indicator are the same kind of thing, so both come out of this
    one scan. `any_value` is safe because the transform writes a group's title and
    section once per group.
    """
    with read_cursor(timeout=15) as cur:
        rows = cur.execute(
            f"""
            SELECT
                {DATASET_COLUMN}                    AS dataset_id,
                {GROUP_COLUMN}                      AS group_id,
                any_value(source_id)                AS source_id,
                any_value(group_title)              AS title,
                any_value(section)                  AS section,
                count(*)                            AS row_count,
                count(DISTINCT {SERIES_COLUMN})     AS series,
                min(period)                         AS first_period,
                max(period)                         AS last_period,
                any_value(unit)                     AS unit,
                any_value(freq)                     AS freq
            FROM {SCHEMA}."{OBSERVATIONS}"
            GROUP BY {DATASET_COLUMN}, {GROUP_COLUMN}
            ORDER BY {DATASET_COLUMN}, {GROUP_COLUMN}
            """
        ).fetchall()

    return [
        {
            "dataset_id": r[0],
            "group_id": r[1],
            "source_id": r[2],
            # A group without a title falls back to its key. `I.1.` is a worse name
            # than "Uang Beredar dan Faktor-Faktor…" but it is better than a blank.
            "title": r[3] or r[1],
            "section": r[4],
            "row_count": r[5],
            "series": r[6],
            "first_period": r[7],
            "last_period": r[8],
            "unit": r[9],
            "freq": r[10],
        }
        for r in rows
    ]


def _card(**kw: Any) -> dict[str, Any]:
    base = {
        #: The short id this card is addressed by: `/dataset/wm72qlsa`. None for a
        #: source that has published nothing — there is no data to open.
        "id": None,
        "title": None,
        #: The keys behind the id. `dataset_id` is what the source published
        #: ('seki_indicators'); `group_id` is a group inside it ('I.1.'); `series` is
        #: one line of numbers inside that. Together they are what the id resolves
        #: to, and what a WHERE clause is built from.
        "dataset_id": None,
        "group_id": None,
        #: Set only on a series card. `Aktiva Dalam Negeri Bersih` is a dataset in
        #: its own right, but its name is not unique — "Lainnya" is the title of 23
        #: of them — so a series card also carries the group it came from.
        "series": None,
        "parent_title": None,
        "parent_id": None,
        "queryable": False,
        "source_id": None,
        "source_name": None,
        "description": None,
        "kind": None,
        "schedule": None,
        "enabled": True,
        "section": None,
        "labels": [],
        "last_collected": None,
        "row_count": None,
        "column_count": None,
        "indicators": None,
        "first_period": None,
        "last_period": None,
        "unit": None,
        "freq": None,
    }
    return {**base, **kw}


def series_of() -> list[dict[str, Any]]:
    """Every series in the lake: one row per (dataset, group, series).

    The bottom rung, and the one the merge earns. SEKI's 3,918 indicators and the
    World Bank's 260 countries come out of the *same* grouped scan, because once a
    country is a series they are the same kind of thing.

    Keyed on the triple, never on the name: "Lainnya" is the name of 23 different
    SEKI series, and 27 World Bank country names collide with SEKI indicator names.
    Nothing here is identified by what it is called.
    """
    with read_cursor(timeout=30) as cur:
        rows = cur.execute(
            f"""
            SELECT
                {DATASET_COLUMN}                    AS dataset_id,
                {GROUP_COLUMN}                      AS group_id,
                {SERIES_COLUMN}                     AS series,
                any_value(source_id)                AS source_id,
                any_value(series_code)              AS series_code,
                any_value(group_title)              AS parent_title,
                any_value(section)                  AS section,
                count(*)                            AS row_count,
                min(period)                         AS first_period,
                max(period)                         AS last_period,
                any_value(unit)                     AS unit,
                any_value(freq)                     AS freq,
                -- The publisher's own row order within a group, so SEKI reads the
                -- way Bank Indonesia prints it. NULL for sources that give none.
                min(row_no)                         AS row_no
            FROM {SCHEMA}."{OBSERVATIONS}"
            GROUP BY {DATASET_COLUMN}, {GROUP_COLUMN}, {SERIES_COLUMN}
            ORDER BY {DATASET_COLUMN}, {GROUP_COLUMN}, min(row_no), {SERIES_COLUMN}
            """
        ).fetchall()

    return [
        {
            "dataset_id": r[0],
            "group_id": r[1],
            "series": r[2],
            "source_id": r[3],
            "series_code": r[4],
            # The group a series belongs to, by name — the only thing telling the 23
            # "Lainnya" apart on a card.
            "parent_title": r[5],
            "section": r[6],
            "row_count": r[7],
            "first_period": r[8],
            "last_period": r[9],
            "unit": r[10],
            "freq": r[11],
            "row_no": r[12],
        }
        for r in rows
    ]


def dataset_cards(sources: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One card per dataset — not per source.

    A source is a thing we scrape; a dataset is a thing you can read. SEKI is one
    source that publishes 108 statistical tables, and "Uang Beredar dan
    Faktor-Faktor yang Mempengaruhinya" is what a reader is looking for. Listing
    the source alone would hide all 108 behind an id nobody searches for.

    A source that has published nothing yet still gets one card, so the page says
    what is being collected rather than pretending it does not exist.
    """
    try:
        column_count = len(describe_table(OBSERVATIONS).columns)
        datasets = datasets_of()
        groups = groups_of()
        series = series_of()
    except (FileNotFoundError, KeyError):
        # No replica, or no observations table in it. Sources still get their
        # "collected, not yet queryable" cards below.
        column_count, datasets, groups, series = 0, [], [], []

    by_id = {s["source_id"]: s for s in (sources or [])}
    cards: list[dict[str, Any]] = []
    published_sources: set[str] = set()

    def common_for(source_id: str | None) -> dict[str, Any]:
        source = by_id.get(source_id) if source_id else None
        if source_id:
            published_sources.add(source_id)
        return {
            "queryable": True,
            "source_id": source_id,
            "source_name": source.get("display_name") if source else None,
            "kind": source.get("kind") if source else None,
            "schedule": source.get("schedule") if source else None,
            "enabled": bool(source.get("enabled", True)) if source else True,
            "last_collected": last_collected(source_id) if source_id else None,
        }

    # The top rung: what each source published.
    for item in datasets:
        common = common_for(item["source_id"])
        source = by_id.get(item["source_id"]) if item["source_id"] else None
        cards.append(
            _card(
                **common,
                id=ids.make_id(item["dataset_id"]),
                dataset_id=item["dataset_id"],
                title=item["dataset_id"],
                description=source.get("description") if source else None,
                row_count=item["row_count"],
                column_count=column_count,
                indicators=item["series"],
                first_period=item["first_period"],
                last_period=item["last_period"],
                # A dataset that mixes units (SEKI has 19) cannot claim one.
                unit=item["unit"] if item["groups"] <= 1 else None,
                freq=item["freq"],
                labels=_labels(common, queryable=True, extra=[item["freq"], "dataset"]),
            )
        )

    # The middle rung: one card per group. SEKI has 108 statistical tables; the World
    # Bank has one indicator. Both are groups, and both come through here.
    for item in groups:
        common = common_for(item["source_id"])
        cards.append(
            _card(
                **common,
                id=ids.make_id(item["dataset_id"], item["group_id"]),
                dataset_id=item["dataset_id"],
                group_id=item["group_id"],
                title=item["title"],
                section=item["section"],
                row_count=item["row_count"],
                column_count=column_count,
                indicators=item["series"],
                first_period=item["first_period"],
                last_period=item["last_period"],
                unit=item["unit"],
                freq=item["freq"],
                labels=_labels(common, queryable=True, extra=[item["freq"], "group"]),
            )
        )

    # The bottom rung: one card per series. `Aktiva Dalam Negeri Bersih` is row 1
    # of "Uang Beredar…" and `Indonesia` is one of the World Bank's 260 — both are
    # datasets a reader can open, chart, and export on their own.
    for item in series:
        common = common_for(item["source_id"])
        group_id = item["group_id"]
        cards.append(
            _card(
                **common,
                id=ids.make_id(item["dataset_id"], group_id, item["series"]),
                dataset_id=item["dataset_id"],
                group_id=group_id,
                title=item["series"],
                series=item["series"],
                parent_title=item["parent_title"] or group_id,
                parent_id=ids.make_id(item["dataset_id"], group_id),
                section=item["section"],
                row_count=item["row_count"],
                column_count=column_count,
                first_period=item["first_period"],
                last_period=item["last_period"],
                unit=item["unit"],
                freq=item["freq"],
                labels=_labels(common, queryable=True, extra=[item["freq"], "series"]),
                # Carried only for sorting, so a series lands under its own table in
                # the publisher's own row order. Stripped below.
                _row_no=item["row_no"],
            )
        )

    # A source that has collected nothing yet is still worth naming: the page
    # should say what is being gathered, not only what is already queryable.
    for source in sources or []:
        if source["source_id"] in published_sources:
            continue
        cards.append(
            _card(
                id=None,
                title=source["source_id"],
                queryable=False,
                source_id=source["source_id"],
                source_name=source.get("display_name", source["source_id"]),
                description=source.get("description"),
                kind=source.get("kind"),
                schedule=source.get("schedule"),
                enabled=bool(source.get("enabled", False)),
                last_collected=last_collected(source["source_id"]),
                labels=_labels(source, queryable=False),
            )
        )

    # queryable first, then active, then by section, then by the publisher's own key
    # — which for SEKI IS its numbering (`I.1.`, `I.2.`, …), so the catalogue reads
    # in the order Bank Indonesia prints it. Within one group the group's own card
    # comes first, then its series in the order the publisher lists them: a series is
    # a row *of* that group, and alphabetising it across the whole catalogue would
    # scatter the rows of one table among thousands of unrelated ones.
    cards.sort(
        key=lambda c: (
            not c["queryable"],
            not c["enabled"],
            # Datasets never interleave: `I.1.` and `NY.GDP.MKTP.CD` are two
            # publishers' numbering schemes and sorting them against each other means
            # nothing.
            c["dataset_id"] or "",
            c["section"] or "",
            _number_key(c["group_id"]),
            c["series"] is not None,  # the group before the series it contains
            c.get("_row_no") if c.get("_row_no") is not None else 0,
            c["title"].lower(),
        )
    )
    for card in cards:
        card.pop("_row_no", None)  # a sort key, not something the page needs
    return cards


def _labels(
    source: dict[str, Any], *, queryable: bool, extra: list[str] | None = None
) -> list[str]:
    """Chips for one card, in a stable order and never repeated.

    A source's collection schedule and its data's frequency are different facts
    that often share a word — SEKI is scraped monthly and its series are monthly
    — so the duplicate is dropped rather than shown twice.
    """
    candidates = [source.get("kind"), source.get("schedule"), *(extra or [])]
    labels: list[str] = []
    for label in candidates:
        if label and label not in labels:
            labels.append(label)
    labels.append("queryable" if queryable else "raw only")
    if not source.get("enabled", True):
        labels.append("paused")
    return labels


def _number_key(number: str | None) -> tuple:
    """Sort `I.10.` after `I.9.`, not between `I.1.` and `I.2.`.

    Each part becomes `(is_text, number, text)` rather than a bare int or str, so a
    numeric part and a textual one can be compared without raising. Different
    publishers number differently — `I.1.` and `NY.GDP.MKTP.CD` — and a sort key that
    only works while every key has the same shape is one that fails the day a third
    source lands.
    """
    if not number:
        return ()
    return tuple(
        (False, int(p), "") if p.isdigit() else (True, 0, p)
        for p in re.split(r"[.\s]+", number)
        if p
    )


def filter_cards(
    cards: list[dict[str, Any]],
    *,
    q: str = "",
    kind: str = "",
    status: str = "",
    section: str = "",
    level: str = "",
) -> list[dict[str, Any]]:
    """Narrow the card list. Pure function of its inputs, so it is trivial to test.

    Search matches the fields a reader can actually see on the card: its title, the
    group it belongs to, the source it came from, the description, the section it
    sits under, and the publisher's own key — so "uang beredar" finds the group and
    all 59 series inside it, and "I.1." finds them too.

    `level` narrows to one rung of the hierarchy. Without it the index is nearly four
    thousand cards, most of them single series; with `level=group` it is the 109
    things Bank Indonesia and the World Bank actually publish.
    """
    out = cards
    needle = q.strip().lower()
    if needle:
        out = [c for c in out if needle in _haystack(c)]
    if kind:
        out = [c for c in out if c.get("kind") == kind]
    if section:
        out = [c for c in out if c.get("section") == section]
    # The three rungs below a source, told apart by which keys a card carries.
    # `queryable` guards the dataset rung: a source that has collected nothing yet
    # also has no group and no series, and it is not a dataset.
    if level == "series":
        out = [c for c in out if c.get("series")]
    elif level == "group":
        out = [c for c in out if c.get("group_id") and not c.get("series")]
    elif level == "dataset":
        out = [
            c for c in out if c.get("queryable") and not c.get("group_id") and not c.get("series")
        ]
    if status == "queryable":
        out = [c for c in out if c.get("queryable")]
    elif status == "raw":
        out = [c for c in out if not c.get("queryable")]
    elif status == "paused":
        out = [c for c in out if not c.get("enabled", True)]
    return out


# --- ids -> keys -------------------------------------------------------------
#
# A URL carries an id: `wm72qlsa`. Eight characters, one flat segment, nothing to
# percent-encode, and it addresses exactly one thing.
#
# What the URL says and what the database stores are deliberately different. A group
# is keyed by the publisher's own numbering (`I.1.`, `NY.GDP.MKTP.CD`) and a series by
# its full name, spaces, parentheses and all. Neither belongs in a URL.
#
# This is where an id becomes the keys a query needs. The lookup is also the
# defence: an id is never interpolated into SQL, it is resolved against the real
# catalog and the catalog's own copy of the key is what gets bound. An id naming
# nothing raises.


@dataclass(frozen=True, slots=True)
class Thing:
    """One addressable thing, at any rung of the hierarchy.

    `group_id` is None only on a dataset — the rung *above* the groups. Below that it
    is always set, because every source names its own groups.
    """

    id: str
    level: str  # dataset | group | series
    dataset_id: str
    group_id: str | None
    series: str | None
    title: str


def _build_id_map() -> dict[str, Thing]:
    """Every id in the lake, in one pass.

    Built from the same grouped scans the cards are, so an id and a card can never
    disagree about what a thing is.

    The ids are derived, not stored — `make_id` hashes the keys — so this map is a
    cache, not a source of truth. Losing it costs a scan, not a set of dead links.
    """
    out: dict[str, Thing] = {}

    def add(level: str, dataset: str, group: str | None, series: str | None, title: str) -> None:
        key = ids.make_id(dataset, group, series)
        # 40 bits over a few thousand things: a collision is vanishingly unlikely,
        # and serving the wrong series would be worse than failing to serve one.
        if key in out:
            raise RuntimeError(
                f"id collision on {key!r}: {out[key]} and {(dataset, group, series)}"
            )
        out[key] = Thing(key, level, dataset, group, series, title)

    for item in datasets_of():
        add("dataset", item["dataset_id"], None, None, item["dataset_id"])

    for item in groups_of():
        add("group", item["dataset_id"], item["group_id"], None, item["title"])

    for item in series_of():
        add("series", item["dataset_id"], item["group_id"], item["series"], item["series"])

    return out


@lru_cache(maxsize=1)
def _id_map(replica_stamp: float) -> dict[str, Thing]:
    """Cached against the replica's build time.

    Building it scans a million rows. `lake serve build` swaps a new file into
    place, so its mtime changing is exactly the signal that the map is stale —
    which means it never has to be invalidated by hand.
    """
    return _build_id_map()


def id_map() -> dict[str, Thing]:
    path = replica_path()
    return _id_map(path.stat().st_mtime if path.exists() else 0.0)


def resolve(thing_id: str) -> Thing:
    """The thing an id points at.

    Raises KeyError when the id names nothing we serve — the caller turns that into
    a 404 rather than an empty page dressed up as real data.
    """
    try:
        return id_map()[thing_id.strip().lower()]
    except KeyError as exc:
        raise KeyError(f"no dataset {thing_id!r}") from exc


def id_for(dataset: str, group: str | None = None, series: str | None = None) -> str:
    """The id for a thing, from its keys. The inverse of `resolve`."""
    return ids.make_id(dataset, group, series)


def parent_of(thing: Thing) -> Thing | None:
    """The thing one rung up, or None at the top.

    Every rung is always there — dataset, then group, then series — so this is a walk
    up a fixed ladder, not a search for whichever rungs a source happens to have.
    """
    if thing.level == "dataset":
        return None
    if thing.level == "group":
        return resolve(id_for(thing.dataset_id))
    return resolve(id_for(thing.dataset_id, thing.group_id))


def predicate(thing: Thing) -> tuple[str, list[Any]]:
    """The WHERE clause that isolates one thing, and its bound parameters.

    This is what the merge buys. Every rung — a dataset, a group inside it, a single
    series inside that — is the same table with a different filter, so there is one
    place that knows how a thing becomes a query and everything else calls it.

    Nothing from the URL reaches this. The column names are module constants and the
    values come from a `Thing`, which only exists because an id resolved against the
    real catalog — so an injected id raises at `resolve` and never gets here.
    """
    clauses = [f"{DATASET_COLUMN} = ?"]
    params: list[Any] = [thing.dataset_id]

    if thing.group_id is not None:
        clauses.append(f"{GROUP_COLUMN} = ?")
        params.append(thing.group_id)
    if thing.series is not None:
        clauses.append(f"{SERIES_COLUMN} = ?")
        params.append(thing.series)

    return " AND ".join(clauses), params


def _query_for(thing: Thing) -> dict[str, Any]:
    """The API request that returns this thing's rows, as data rather than a string.

    The id carries the keys. `/api/data/i5demefo/rows` says everything that
    `?dataset_id=seki_indicators&group_id=I.1.&series=Uang+Beredar+Luas%28M2%29` says,
    and a reader can hold it in their head, paste it into a paper, and read it back off
    a slide. That is the whole argument for an opaque id: not that the keys are secret,
    but that they are long, punctuated, and nobody wants to escape them.

    So what crosses the wire is the id plus what to do with it — a projection, an
    order, a page. The page needs this three ways (a link to the browser, a download
    URL, four copy-paste snippets), and each wants a different encoding of the same
    request, so it travels as data rather than as a formatted string.
    """
    # A series is one line of numbers, so `series` in the projection would repeat the
    # page title on every row. Above that rung it is what tells the rows apart.
    select = ["period", "value", "unit"] if thing.series else ["period", "series", "value", "unit"]

    return {
        "id": thing.id,
        "select": select,
        # Extra narrowing on top of the id. Empty on a thing's own page — the id already
        # says which rows — but the browser fills it in as the reader adds filters.
        "filters": {},
        "sort": "period",
        "descending": True,
        "limit": 100,
    }


def crumbs(thing: Thing) -> list[dict[str, str]]:
    """The trail back up, so a reader on an opaque id knows where they are.

    This is what an id costs: `wm72qlsa` says nothing on its own, so the page has to
    say it instead. Titles for the reader, ids for the links.
    """
    trail: list[dict[str, str]] = []
    node: Thing | None = thing
    while node is not None:
        trail.append({"id": node.id, "title": node.title, "level": node.level})
        node = parent_of(node)
    return list(reversed(trail))


def describe_dataset(thing_id: str) -> dict[str, Any]:
    """Everything a detail page needs, for any rung of the hierarchy.

    One query, whatever the id names: the merged table means a dataset, a group, and
    a series differ only by how much of the WHERE clause is filled in.

    Raises KeyError when the id names nothing we serve — the caller turns that into a
    404 rather than rendering an empty page that looks like real data.
    """
    thing = resolve(thing_id)  # raises KeyError on an unknown id
    observations = describe_table(OBSERVATIONS)  # raises KeyError with no replica
    where, params = predicate(thing)

    with read_cursor(timeout=15) as cur:
        row = cur.execute(
            f"""
            SELECT
                count(*),
                {SERIES_COUNT},
                count(DISTINCT {GROUP_COLUMN}),
                min(period), max(period),
                any_value(source_id),
                count(DISTINCT section),
                any_value(section),
                any_value(series_code),
                count(DISTINCT unit),
                any_value(unit),
                count(DISTINCT freq),
                any_value(freq),
                count(*) FILTER (WHERE value IS NULL)
            FROM {SCHEMA}."{OBSERVATIONS}"
            WHERE {where}
            """,
            params,
        ).fetchone()

    if not row or not row[0]:
        raise KeyError(f"no dataset {thing_id!r}")

    rows, series_count, group_count = row[0], row[1], row[2]
    first, last = row[3], row[4]
    source_id, sections, section, code = row[5], row[6], row[7], row[8]
    units, unit, freqs, freq, missing = row[9], row[10], row[11], row[12], row[13]

    # A section describes one group. `seki_indicators` spans eleven of them, so
    # claiming one would put "VIII. Harga - Harga" on a page that also holds the
    # money supply.
    if sections != 1:
        section = None

    parent = parent_of(thing)

    return {
        "id": thing.id,
        "level": thing.level,
        "title": thing.title,
        # The trail back up. An id says nothing, so the page has to.
        "crumbs": crumbs(thing),
        "parent_id": parent.id if parent else None,
        "parent_title": parent.title if parent else None,
        # The keys behind the id, for a reader who wants to write their own SQL.
        "table": OBSERVATIONS,
        "dataset_id": thing.dataset_id,
        # The publisher's own key for the group: `I.1.`, `NY.GDP.MKTP.CD`. It is the
        # closest thing to a citation the lake can give — quote it at Bank Indonesia
        # and they will know which table you mean.
        "group_id": thing.group_id,
        "series": thing.series,
        "series_code": code if thing.series else None,
        "section": section,
        "source_id": source_id,
        # What is inside it. A series is the bottom rung, so it counts neither.
        "row_count": rows,
        "series_count": None if thing.level == "series" else series_count,
        "group_count": group_count if thing.level == "dataset" else None,
        # How complete it is: the World Bank reports 2,681 missing years, and a
        # missing observation is not a zero.
        "missing_count": missing,
        "first_period": first,
        "last_period": last,
        # A rung spanning several units or frequencies (SEKI has 19 units) cannot
        # claim one of them, and saying it does would be a lie the page then repeats.
        "unit": unit if units == 1 else None,
        "unit_count": units,
        "freq": freq if freqs == 1 else None,
        "columns": [
            {"name": c.name, "type": c.type, "nullable": c.nullable} for c in observations.columns
        ],
        # The API request that returns these rows — what the page turns into a link,
        # a download, and the copy-paste snippets. There is no SQL endpoint to hand
        # anyone a query string for.
        "query": _query_for(thing),
    }


def children_of(thing_id: str, limit: int = 300) -> dict[str, Any]:
    """What is inside a thing: one rung down, never two.

    A dataset's children are its groups; a group's are its series; a series is the
    bottom rung and has none. That is the whole rule, because every rung is always
    there — a source that publishes one flat table publishes it as one group, so
    there is no "dataset whose children happen to be series" to special-case.

    `total` is the real count and `items` may be shorter, so a page listing 300 of a
    source's children can say it is showing 300 of them rather than implying that is
    all there are.
    """
    thing = resolve(thing_id)
    if thing.level == "series":
        return {"level": None, "items": [], "total": 0}

    where, params = predicate(thing)

    is_group = thing.level == "group"
    child = SERIES_COLUMN if is_group else GROUP_COLUMN
    level = "series" if is_group else "group"
    # A group has a name of its own; a series IS its name.
    title = child if is_group else "any_value(group_title)"

    with read_cursor(timeout=15) as cur:
        total = scalar(
            cur.execute(
                f'SELECT count(DISTINCT {child}) FROM {SCHEMA}."{OBSERVATIONS}" WHERE {where}',
                params,
            )
        )

        rows = cur.execute(
            f"""
            SELECT
                {child}                     AS key,
                {title}                     AS title,
                count(*)                    AS row_count,
                min(period)                 AS first_period,
                max(period)                 AS last_period,
                any_value(unit)             AS unit
            FROM {SCHEMA}."{OBSERVATIONS}"
            WHERE {where}
            GROUP BY {child}
            -- The publisher's own row order, so SEKI reads the way Bank Indonesia
            -- prints it. A source that gives none falls back to the key.
            ORDER BY min(row_no) NULLS LAST, {child}
            LIMIT {int(limit)}
            """,
            params,
        ).fetchall()

    items = [
        {
            "id": id_for(
                thing.dataset_id,
                thing.group_id if is_group else r[0],
                r[0] if is_group else None,
            ),
            "title": r[1] or r[0],
            "level": level,
            "row_count": r[2],
            "first_period": r[3],
            "last_period": r[4],
            "unit": r[5],
        }
        for r in rows
    ]
    return {"level": level, "items": items, "total": total}


def siblings_of(thing_id: str, limit: int = 300) -> dict[str, Any]:
    """The other things on the same rung, alongside this one.

    What a series page needs where `children_of` gives it nothing: a reader who opened
    the fourteenth series of a table is very likely to want the fifteenth, and making
    them navigate back up to a list to get it is the whole cost of a detail page.

    Siblings ARE the parent's children — the same query, asked from one rung down — so
    this is a lookup, not a second implementation that could disagree with the first.
    The current thing is included rather than filtered out: the list is a place, and a
    place a reader is standing in should show where they are standing.
    """
    thing = resolve(thing_id)
    parent = parent_of(thing)
    if parent is None:
        return {"level": None, "items": [], "total": 0}  # a dataset has no siblings
    return children_of(parent.id, limit=limit)


def dataset_sample(thing_id: str, limit: int = 20) -> dict[str, Any]:
    """A few real rows of this dataset, for the detail page.

    A series is one line of numbers over time, so its sample drops the `series`
    column — every row would carry the same value, which is noise the reader
    already knows from the page title.
    """
    thing = resolve(thing_id)
    series = thing.series
    where, params = predicate(thing)

    columns = "period, value, unit" if series else f"period, {SERIES_COLUMN}, value, unit"
    order = "period DESC" if series else f"period DESC, {SERIES_COLUMN}"

    with read_cursor(timeout=15) as cur:
        cur.execute(
            f'SELECT {columns} FROM {SCHEMA}."{OBSERVATIONS}" '
            f"WHERE {where} ORDER BY {order} LIMIT {int(limit)}",
            params,
        )
        headers = [d[0] for d in cur.description]
        rows = [[jsonable(v) for v in r] for r in cur.fetchall()]
    return {"columns": headers, "rows": rows, "row_count": len(rows)}


def dataset_series(thing_id: str, limit_points: int = 240) -> list[dict[str, Any]]:
    """The line to plot on a dataset page, newest points last.

    For a *series* that is the series itself — there is nothing to choose. For a
    rung above it, the honest line is the one the publisher lists first: row 1 of
    "Uang Beredar…" is M2, which is what the table is named for. A source that
    gives no row order (the World Bank) has no such headline, so nothing is drawn
    rather than an arbitrary country's line pretending to be the dataset's.
    """
    thing = resolve(thing_id)
    series = thing.series
    where, params = predicate(thing)

    with read_cursor(timeout=15) as cur:
        if series is not None:
            rows = cur.execute(
                f'SELECT period, value FROM {SCHEMA}."{OBSERVATIONS}" '
                f"WHERE {where} ORDER BY period",
                params,
            ).fetchall()
        else:
            rows = cur.execute(
                f"""
                WITH lead AS (
                    SELECT {SERIES_COLUMN} FROM {SCHEMA}."{OBSERVATIONS}"
                    WHERE {where} AND row_no IS NOT NULL
                    ORDER BY row_no LIMIT 1
                )
                SELECT period, value FROM {SCHEMA}."{OBSERVATIONS}"
                WHERE {where} AND {SERIES_COLUMN} = (SELECT {SERIES_COLUMN} FROM lead)
                ORDER BY period
                """,
                [*params, *params],
            ).fetchall()

    points = rows[-limit_points:]
    return [{"period": p, "value": v} for p, v in points]


#: What a search query is matched against — everything a reader can see on the card.
_SEARCHABLE = (
    "title",
    # A series is searchable by the group it belongs to as well as by its own name:
    # someone looking for "uang beredar" wants that group AND the 59 series inside
    # it, and someone looking for "Lainnya" needs the parent to tell the 23 of them
    # apart.
    "parent_title",
    "source_name",
    "source_id",
    "description",
    "section",
    # The publisher's own key — `I.1.`, `NY.GDP.MKTP.CD`. Someone who has the
    # publication in front of them searches by the number printed in it.
    "group_id",
)


def _haystack(card: dict[str, Any]) -> str:
    return " ".join(str(card.get(f)) for f in _SEARCHABLE if card.get(f)).lower()


#: Viewbox the sparkline path is drawn into. The SVG scales; the numbers don't.
_SPARK_W, _SPARK_H = 640, 160


def headline_series() -> dict[str, Any] | None:
    """World GDP over time, as a ready-to-draw SVG path.

    Decorative *and* true: the hero chart is the real `gdp_annual` series, not a
    hand-drawn squiggle. Returns None whenever the shape isn't there — an empty
    lake, or a replica built from some other dataset — so the page degrades to
    no chart rather than to a lie.

    `WLD` is the World Bank's own world aggregate, and it is a *series* in the
    merged table like any other. Summing the country rows instead would
    double-count: regional aggregates ("Arab World", "Euro area") are series too.
    """
    if OBSERVATIONS not in list_tables():
        return None

    with read_cursor(timeout=5) as cur:
        rows = cur.execute(
            f"""
            SELECT year, value
            FROM {SCHEMA}."{OBSERVATIONS}"
            WHERE {DATASET_COLUMN} = 'gdp_annual'
              AND series_code = 'WLD'
              AND value IS NOT NULL
            ORDER BY year
            """
        ).fetchall()

    if len(rows) < 2:
        return None

    years = [int(r[0]) for r in rows]
    values = [float(r[1]) for r in rows]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = _SPARK_W / (len(rows) - 1)

    points = [
        (round(i * step, 2), round(_SPARK_H - (v - lo) / span * _SPARK_H, 2))
        for i, v in enumerate(values)
    ]
    line = "M" + " L".join(f"{x},{y}" for x, y in points)

    return {
        "line": line,
        # close the path down to the baseline so it can be filled
        "area": f"{line} L{_SPARK_W},{_SPARK_H} L0,{_SPARK_H} Z",
        "width": _SPARK_W,
        "height": _SPARK_H,
        "first_year": years[0],
        "last_year": years[-1],
        "last_value": values[-1],
        "points": len(points),
    }


def schema_digest() -> str:
    """The whole catalog as compact text, for an AI system prompt.

    Deliberately terse. A model writing SQL needs shapes, not prose — but a single
    long table is not self-explanatory the way a wide one is: nothing in
    `series VARCHAR` tells a model that it holds both 'Indonesia' and 'Uang Beredar
    Luas(M2)', or that a `value` is meaningless without its `unit`. So the shape is
    spelled out, and the real values of the low-cardinality columns are listed,
    because a model that can see `dataset_id IN ('gdp_annual','seki_indicators')`
    writes a correct filter and one that cannot guesses at it.
    """
    lines = [
        "Read-only DuckDB. Schema `lake`. SELECT and EXPLAIN only.",
        "Writes, file functions (read_csv/read_parquet), ATTACH, PRAGMA, and",
        "multiple statements are rejected before execution.",
        "",
    ]

    for name in list_tables():
        table = describe_table(name)
        cols = ", ".join(f"{c.name} {c.type}" for c in table.columns)
        lines.append(f"{SCHEMA}.{table.name} ({table.row_count:,} rows)")
        lines.append(f"  {cols}")

    if OBSERVATIONS not in list_tables():
        return "\n".join(lines)

    lines += [
        "",
        "Every source lands in this one long table. One row is one observation:",
        "at `period`, the series named `series` had `value`, measured in `unit`.",
        "",
        "  dataset_id  what was published",
        "  group_id    a group inside it, keyed the way its publisher keys it:",
        "              'I.1.' is a Bank Indonesia table, 'NY.GDP.MKTP.CD' is a",
        "              World Bank indicator. Never NULL.",
        "  group_title what that group is called. A name, not a key — four SEKI",
        "              titles are shared by more than one group.",
        "  series      what the row is a time series OF — an indicator like",
        "              'Uang Beredar Luas(M2)', or a country like 'Indonesia'",
        "  series_code the publisher's own id for it ('IDN'), NULL if none",
        "",
        "ALWAYS filter on dataset_id: series names are not unique across datasets,",
        "and `value` mixes units, so SUM or AVG across datasets is meaningless.",
        "",
    ]

    with read_cursor(timeout=10) as cur:
        for column in (DATASET_COLUMN, "source_id", "freq", "unit"):
            values = cur.execute(
                f'SELECT DISTINCT "{column}" FROM {SCHEMA}."{OBSERVATIONS}" '
                f'WHERE "{column}" IS NOT NULL ORDER BY 1 LIMIT {MAX_DISTINCT}'
            ).fetchall()
            listed = ", ".join(repr(v[0]) for v in values)
            lines.append(f"  {column}: {listed}")

    return "\n".join(lines)
