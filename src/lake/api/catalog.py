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
from typing import Any

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


#: Which source each published dataset is built from.
#:
#: The authority is `lake.transform.runner.TRANSFORMS`, but importing that here
#: would pull `MetadataRepo` — and therefore Postgres — into a serving path that
#: is meant to keep answering when the catalog database is down. A dataset whose
#: source is not listed here simply shows no provenance rather than a wrong one.
DATASET_SOURCE = {
    "gdp_annual": "worldbank_gdp",
    "seki_indicators": "seki",
}


@dataclass(frozen=True, slots=True)
class Partitioned:
    """A DuckDB table that holds many logical datasets, one per key value.

    SEKI is one source publishing 108 statistical tables. They share a schema, so
    the transform writes them into a single `seki_indicators` table rather than
    108 near-identical ones — but "Uang Beredar dan Faktor-Faktor yang
    Mempengaruhinya" is a dataset a reader looks for by name, not a `table_id`
    they have to know to filter on. This declares how to fan one table back out.
    """

    key: str  # the column that separates one logical dataset from the next
    title: str  # human name of the dataset
    label: str  # a further grouping, e.g. SEKI's section headings
    number: str | None = None  # the publisher's own numbering, if any


#: Tables that are really many datasets. Everything else is one dataset per table.
PARTITIONED: dict[str, Partitioned] = {
    "seki_indicators": Partitioned(
        key="table_id", title="table_title", label="section", number="table_number"
    ),
}

#: Separates a table from the partition inside it: `seki_indicators:TABEL1_1`.
SLUG_SEP = ":"


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


def partitions_of(table: str) -> list[dict[str, Any]]:
    """The logical datasets inside a partitioned table, with their shape.

    One grouped scan rather than 108 queries. `any_value` is safe here because
    the transform writes the title, section, and number once per `table_id`.
    """
    spec = PARTITIONED.get(table)
    if spec is None:
        return []

    number = f'any_value("{spec.number}")' if spec.number else "NULL"
    with read_cursor(timeout=15) as cur:
        rows = cur.execute(
            f"""
            SELECT
                "{spec.key}"                    AS key,
                any_value("{spec.title}")       AS title,
                any_value("{spec.label}")       AS label,
                {number}                        AS number,
                count(*)                        AS row_count,
                count(DISTINCT indicator)       AS indicators,
                min(period)                     AS first_period,
                max(period)                     AS last_period,
                any_value(unit)                 AS unit,
                any_value(freq)                 AS freq
            FROM {SCHEMA}."{table}"
            GROUP BY "{spec.key}"
            ORDER BY "{spec.key}"
            """
        ).fetchall()

    return [
        {
            "key": r[0],
            "title": r[1] or r[0],
            "label": r[2],
            "number": r[3],
            "row_count": r[4],
            "indicators": r[5],
            "first_period": r[6],
            "last_period": r[7],
            "unit": r[8],
            "freq": r[9],
        }
        for r in rows
    ]


def _card(**kw: Any) -> dict[str, Any]:
    base = {
        "slug": None,
        "title": None,
        "dataset": None,
        "partition": None,
        "queryable": False,
        "source_id": None,
        "source_name": None,
        "description": None,
        "kind": None,
        "schedule": None,
        "enabled": True,
        "section": None,
        "number": None,
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
        tables = {t.name: t for t in (describe_table(n) for n in list_tables())}
    except FileNotFoundError:
        tables = {}

    by_id = {s["source_id"]: s for s in (sources or [])}
    cards: list[dict[str, Any]] = []
    published_sources: set[str] = set()

    for name, table in tables.items():
        source_id = DATASET_SOURCE.get(name)
        source = by_id.get(source_id) if source_id else None
        if source_id:
            published_sources.add(source_id)

        common = {
            "dataset": name,
            "queryable": True,
            "source_id": source_id,
            "source_name": source.get("display_name") if source else None,
            "kind": source.get("kind") if source else None,
            "schedule": source.get("schedule") if source else None,
            "enabled": bool(source.get("enabled", True)) if source else True,
            "last_collected": last_collected(source_id) if source_id else None,
        }

        if name not in PARTITIONED:
            cards.append(
                _card(
                    **common,
                    slug=name,
                    title=name,
                    description=source.get("description") if source else None,
                    row_count=table.row_count,
                    column_count=len(table.columns),
                    labels=_labels(common, queryable=True),
                )
            )
            continue

        # One card per logical dataset inside the table.
        for part in partitions_of(name):
            cards.append(
                _card(
                    **common,
                    slug=f"{name}{SLUG_SEP}{part['key']}",
                    title=part["title"],
                    partition=part["key"],
                    section=part["label"],
                    number=part["number"],
                    row_count=part["row_count"],
                    column_count=len(table.columns),
                    indicators=part["indicators"],
                    first_period=part["first_period"],
                    last_period=part["last_period"],
                    unit=part["unit"],
                    freq=part["freq"],
                    labels=_labels(common, queryable=True, extra=[part["freq"]]),
                )
            )

    # A source that has collected nothing yet is still worth naming: the page
    # should say what is being gathered, not only what is already queryable.
    for source in sources or []:
        if source["source_id"] in published_sources:
            continue
        cards.append(
            _card(
                slug=None,
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

    # queryable first, then active, then by section, then by the publisher's own
    # numbering where it exists — so SEKI reads in the order Bank Indonesia prints it
    cards.sort(
        key=lambda c: (
            not c["queryable"],
            not c["enabled"],
            c["section"] or "",
            _number_key(c["number"]),
            c["title"].lower(),
        )
    )
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
    """Sort `I.10.` after `I.9.`, not between `I.1.` and `I.2.`."""
    if not number:
        return ()
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"[.\s]+", number) if p)


def filter_cards(
    cards: list[dict[str, Any]],
    *,
    q: str = "",
    kind: str = "",
    status: str = "",
    section: str = "",
) -> list[dict[str, Any]]:
    """Narrow the card list. Pure function of its inputs, so it is trivial to test.

    Search matches the fields a reader can actually see on the card: its title,
    the source it came from, the description, the section it sits under, and the
    publisher's own numbering — so both "uang beredar" and "I.1." find the table.
    Matching hidden fields would make results look arbitrary.
    """
    out = cards
    needle = q.strip().lower()
    if needle:
        out = [c for c in out if needle in _haystack(c)]
    if kind:
        out = [c for c in out if c.get("kind") == kind]
    if section:
        out = [c for c in out if c.get("section") == section]
    if status == "queryable":
        out = [c for c in out if c.get("queryable")]
    elif status == "raw":
        out = [c for c in out if not c.get("queryable")]
    elif status == "paused":
        out = [c for c in out if not c.get("enabled", True)]
    return out


def split_slug(slug: str) -> tuple[str, str | None]:
    """`seki_indicators:TABEL1_1` -> ("seki_indicators", "TABEL1_1")."""
    table, _, partition = slug.partition(SLUG_SEP)
    return table, partition or None


def describe_dataset(slug: str) -> dict[str, Any]:
    """Everything a detail page needs, for a whole table or one partition of one.

    Raises KeyError when the slug names nothing we serve — the caller turns that
    into a 404 rather than rendering an empty page that looks like real data.
    """
    table_name, partition = split_slug(slug)
    table = describe_table(table_name)  # raises KeyError on an unknown table
    spec = PARTITIONED.get(table_name)

    if partition is None:
        if spec is not None:
            # the umbrella table itself is not a dataset a reader browses
            raise KeyError(f"{table_name} is a collection; name one of its datasets")
        return {
            "slug": slug,
            "table": table_name,
            "partition": None,
            "title": table_name,
            "columns": [
                {"name": c.name, "type": c.type, "nullable": c.nullable} for c in table.columns
            ],
            "row_count": table.row_count,
            "source_id": DATASET_SOURCE.get(table_name),
            "sql": f'SELECT * FROM {SCHEMA}."{table_name}" LIMIT 100',
        }

    if spec is None:
        raise KeyError(f"{table_name} has no partitions")

    with read_cursor(timeout=15) as cur:
        row = cur.execute(
            f"""
            SELECT
                any_value("{spec.title}"), any_value("{spec.label}"),
                {f'any_value("{spec.number}")' if spec.number else "NULL"},
                count(*), count(DISTINCT indicator),
                min(period), max(period), any_value(unit), any_value(freq)
            FROM {SCHEMA}."{table_name}" WHERE "{spec.key}" = ?
            """,
            [partition],
        ).fetchone()

    if not row or not row[3]:
        raise KeyError(f"no dataset {partition!r} in {table_name}")

    quoted = partition.replace("'", "''")
    return {
        "slug": slug,
        "table": table_name,
        "partition": partition,
        "partition_key": spec.key,
        "title": row[0] or partition,
        "section": row[1],
        "number": row[2],
        "row_count": row[3],
        "indicators": row[4],
        "first_period": row[5],
        "last_period": row[6],
        "unit": row[7],
        "freq": row[8],
        "columns": [
            {"name": c.name, "type": c.type, "nullable": c.nullable} for c in table.columns
        ],
        "source_id": DATASET_SOURCE.get(table_name),
        "sql": (
            f'SELECT period, indicator, value\nFROM {SCHEMA}."{table_name}"\n'
            f"WHERE {spec.key} = '{quoted}'\nORDER BY period DESC, indicator\nLIMIT 100"
        ),
    }


def dataset_sample(slug: str, limit: int = 20) -> dict[str, Any]:
    """A few real rows of this dataset, for the detail page."""
    table_name, partition = split_slug(slug)
    name = _assert_known_table(table_name)
    spec = PARTITIONED.get(name)

    if partition is None or spec is None:
        sql = f'SELECT * FROM {SCHEMA}."{name}"'
        params: list[Any] = []
    else:
        sql = (
            f'SELECT period, indicator, value, unit FROM {SCHEMA}."{name}" '
            f'WHERE "{spec.key}" = ? ORDER BY period DESC, indicator'
        )
        params = [partition]

    with read_cursor(timeout=15) as cur:
        cur.execute(f"{sql} LIMIT {int(limit)}", params)
        columns = [d[0] for d in cur.description]
        rows = [[jsonable(v) for v in r] for r in cur.fetchall()]
    return {"columns": columns, "rows": rows, "row_count": len(rows)}


def dataset_series(slug: str, limit_points: int = 240) -> list[dict[str, Any]]:
    """The headline series of a partitioned dataset, newest points last.

    The first indicator Bank Indonesia lists is the one the table is named for
    (row 1 of "Uang Beredar…" is M2), so it is the honest thing to plot.
    """
    table_name, partition = split_slug(slug)
    name = _assert_known_table(table_name)
    spec = PARTITIONED.get(name)
    if partition is None or spec is None:
        return []

    with read_cursor(timeout=15) as cur:
        rows = cur.execute(
            f"""
            WITH lead AS (
                SELECT indicator FROM {SCHEMA}."{name}"
                WHERE "{spec.key}" = ? AND row_no IS NOT NULL
                ORDER BY row_no LIMIT 1
            )
            SELECT period, value FROM {SCHEMA}."{name}"
            WHERE "{spec.key}" = ? AND indicator = (SELECT indicator FROM lead)
            ORDER BY period
            """,
            [partition, partition],
        ).fetchall()

    points = rows[-limit_points:]
    return [{"period": p, "value": v} for p, v in points]


#: What a search query is matched against — everything a reader can see on the card.
_SEARCHABLE = ("title", "source_name", "source_id", "description", "section", "number")


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

    `WLD` is the World Bank's own world aggregate. Summing the country rows
    instead would double-count: regional aggregates ("Arab World", "Euro area")
    are rows in the same table.
    """
    if "gdp_annual" not in list_tables():
        return None

    with read_cursor(timeout=5) as cur:
        rows = cur.execute(
            f"""
            SELECT year, gdp_usd
            FROM {SCHEMA}.gdp_annual
            WHERE country_iso3 = 'WLD' AND gdp_usd IS NOT NULL
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

    Deliberately terse. A model writing SQL needs shapes, not prose.
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
    return "\n".join(lines)
