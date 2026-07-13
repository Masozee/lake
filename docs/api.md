# Serving API & AI exploration

A read-only HTTP API over the lake, plus an AI agent that can explore the data
but cannot change it. A TanStack Start frontend sits on top.

```
processed/*.parquet ──lake serve build──> serving.duckdb (read-only replica)
                                                   │
                                    read_only=True, enable_external_access=False
                                                   │
        ┌──────────────────────────────────────────┼─────────────────────┐
        │                                          │                     │
   GET /api/tables                    GET /api/data/{id}/rows       POST /api/ai/ask
   GET /api/tables/{t}                GET /api/data/{id}/aggregate   (Claude + read-only tools)
   GET /api/tables/{t}/profile        GET /api/data/{id}/rows?format=csv  │
        └──────────────────── TanStack Start frontend ───────────────────┘
```

**The public API takes no SQL, and no keys either.** A read names a *thing* — a
dataset, a statistical table inside one, or a single series — by the same short id
its page is addressed by:

```
GET /api/data/i5demefo/rows?period=gte:2000
```

`lake.api.rows` resolves that id against the catalog and compiles the read. Nothing
a caller sends is ever written into the SQL text. The AI still writes SQL,
in-process, behind the guard; see below.

## The read-only guarantee is structural, not a policy

This is the part that matters, so it is worth being precise. "Read-only" here is
not a flag someone remembered to set or a list of banned keywords. It is two
independent layers, either of which alone would stop a write.

### Layer 1 — the engine cannot write or touch the filesystem

The serving connection is opened like this (`src/lake/api/engine.py`):

```python
duckdb.connect(path, read_only=True, config={"enable_external_access": False})
```

Verified against DuckDB 1.5:

| Attempt | Result |
|---|---|
| `INSERT` / `UPDATE` / `DELETE` / `CREATE` / `DROP` | `InvalidInputException` — read-only |
| `read_csv('/etc/passwd')` | `PermissionException` — external access off |
| `read_parquet(...)`, `COPY ... TO`, `ATTACH`, `INSTALL` | `PermissionException` |
| `SET enable_external_access=true` | `InvalidInputException` — **cannot be re-enabled while running** |

That last row is why this is a guarantee and not a hope: once the database is
open, the lockdown cannot be undone from inside a query. An AI agent cannot talk
its way out of a property of the process.

Because `enable_external_access=False` also blocks reading Parquet, the data has
to already be *inside* the database — hence the replica. Choosing to keep the
filesystem lock means a serving layer that cannot be tricked into reading
`/etc/passwd` or the raw NAS tree. That is the right trade.

### Layer 2 — a caller cannot write SQL at all

The public API has no SQL endpoint. A request names a table, some columns, an
operator from a fixed list, and a value, and `src/lake/api/rows.py` compiles that
into a query. Two rules make it safe, and they are the same two `admin/browse.py`
runs on:

* **Identifiers** — a table, column, sort key, or aggregate function named in a
  request is looked up in the real catalog (or in a frozen allowlist, for `agg`),
  and *the catalog's own copy of the name* is what reaches the query. An unknown
  one raises a 422 that names the columns that do exist. An injected one cannot
  survive the round trip.
* **Values** — every filter value is a bound parameter. Not quoted, not escaped:
  bound. There is no string of caller input anywhere in the SQL text.

So `?series=x'; DROP TABLE lake.observations; --` is a *value*. It matches no row
and returns an empty result, because it never becomes SQL. The attack list lives
in `tests/unit/api/test_routes.py`, each case run against a real engine.

### The SQL guard still exists — for the AI

`src/lake/api/sql_guard.py` parses a query with DuckDB's own parser
(`extract_statements`) and rejects anything that is not a single SELECT or
EXPLAIN. Nothing over HTTP reaches it any more; its one caller is the AI's
`run_sql` tool (and `lake serve query` on the CLI). The AI genuinely needs SQL —
it joins and aggregates to answer a question — so it keeps the guard, and the
guard keeps its teeth.

It closes two holes a naive statement-type check leaves open, **both verified**:

* `SELECT * FROM read_csv('/etc/passwd')` parses as a `SELECT`. A type allowlist
  alone is an arbitrary-file-read vulnerability. The guard also scans for
  filesystem/network table functions.
* `PRAGMA database_list` parses as a `SELECT` and returns the absolute path of the
  database file. The guard rejects leading `PRAGMA`/`CALL` and the `pragma_*` /
  `duckdb_*` table functions.

The full attack list lives in `tests/unit/api/test_sql_guard.py` — 60-odd cases,
each one run against a real engine.

### The AI has no write verb at all

The agent's tools are `list_tables`, `describe_table`, `profile_table`, and
`run_sql` (`src/lake/api/ai/tools.py`). There is no `insert`, `update`, `delete`,
or `write` tool, and adding one would not work — the connection underneath is
read-only. The model literally has no verb for mutation.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | replica status and table count |
| GET | `/api/tables` | list table names |
| GET | `/api/tables/{t}` | columns, types, row count |
| GET | `/api/tables/{t}/sample` | first N rows |
| GET | `/api/tables/{t}/profile` | per-column stats + distinct values |
| GET | `/api/data/{id}/rows` | a thing's rows — as JSON, CSV, or Excel |
| GET | `/api/data/{id}/aggregate` | a GROUP BY, without a query language |
| POST | `/api/ai/ask` | ask the AI; streams SSE events |

A request naming a column, operator, or format we do not have returns **422** with a
readable reason — it names the ones that *do* exist. An id that resolves to nothing
is a **404**, and a read that runs too long is a **408**. Nothing returns a 200 with
data it should not have.

### One resource, three representations

The rows of a thing are *one* resource. JSON, CSV and Excel are three ways of
writing them down, so the format is a property of the **request**, not of the path —
a `.csv` on the end of a URL is a filename pretending to be a resource.

```
GET /api/data/i5demefo/rows                      -> JSON, one page
GET /api/data/i5demefo/rows   Accept: text/csv   -> CSV, everything
GET /api/data/i5demefo/rows?format=csv           -> the same
```

**Why both mechanisms.** `Accept` is the REST way and it works. It cannot be the
*only* way, and the reason is not philosophical: `pandas.read_csv(url)` sends **no
Accept header at all**. Under Accept-only negotiation it would receive JSON and parse
it as CSV *without raising* — an empty DataFrame whose single column name is a blob of
JSON, no error, no reason why. A browser's `<a href>` download and R's `read.csv` fail
the same silent way.

So `?format=` exists for the clients that physically cannot set a header, and it
**beats Accept** when both are present: a caller who typed it was being explicit.

Every response carries `Vary: Accept`, so a cache in front of the API cannot hand a
CSV body to the next client that asked for JSON.

### The id

`{id}` is the same eight-character id the thing's page is addressed by
(`/dataset/i5demefo`), or the literal name `observations` for the whole lake — the
raw table is the one thing named rather than hashed, because it is not a dataset,
it is what all of them are views of.

The id is a **hash of the keys behind it**, not a row in a table. Two consequences:
a link anyone shared keeps resolving across a rebuild, with no migration and nothing
to keep in sync; and the id stands in for keys nobody wants to type. These are the
same request:

```bash
curl 'localhost:8000/api/data/i5demefo/rows'
curl 'localhost:8000/api/data/observations/rows?dataset_id=seki_indicators&group_id=I.1.&series=Uang+Beredar+Luas%28M2%29'
```

The second is correct, unreadable, and impossible to cite. That is the argument for
the id — not that the keys are secret, but that they are long and punctuated.

An id is *looked up*, never interpolated, so an unknown or injected one raises at
`catalog.resolve` and 404s rather than reaching the query.

### Reading rows

Any query param that names a real column is a filter, applied **on top of** the id:
the id fixes the slice, and a filter narrows within it. A filter can never widen
past the id. The value may carry an operator prefix; with no prefix it means
equality, because `?freq=annual` should do the obvious thing.

```bash
# one series since 2000, newest first
curl 'localhost:8000/api/data/i5demefo/rows?period=gte:2000&sort=period&desc'

# the same rows as a spreadsheet — same filters, no page
curl -sOJ 'localhost:8000/api/data/i5demefo/rows?format=csv'    # -> M2.csv

# and the whole lake is a thing too
curl 'localhost:8000/api/data/observations/rows?freq=in:annual,monthly&limit=50'
```

| Operator | Meaning | Example |
|---|---|---|
| *(none)* / `eq` | equals | `?freq=annual` |
| `ne` | not equal | `?freq=ne:annual` |
| `contains` | case-insensitive substring | `?series=contains:uang` |
| `starts` | case-insensitive prefix | `?series=starts:M` |
| `gt` `lt` `gte` `lte` | ordered comparison | `?year=gte:2020` |
| `in` | one of a comma-separated list | `?freq=in:annual,monthly` |
| `null` / `notnull` | is (not) empty | `?value=null:` |

A `%` or `_` in a search value is matched literally — someone searching for "50%"
means the characters, not "starts with 50".

Controls: `format`, `select` (comma-separated projection), `sort`, `desc`, `limit`,
`offset`, `filename`. The JSON response carries `total` — the count *after* filtering,
because "page 3 of 41" is a lie if the 41 counts rows the filter removed — and
`has_more`, so a client does not have to do the arithmetic.

**What `limit` defaults to depends on what you asked for**, because a page is a screen
and a file is the data:

| request | default `limit` |
|---|---|
| JSON | 1 000 — a page |
| CSV / Excel | everything that matched (up to 1 000 000) |
| either, with an explicit `?limit=` | exactly what was asked |

A CSV that silently stopped at the JSON page size would be the same class of bug as
the `read_csv` failure above: quiet, and wrong.

Dates compare against a partial period: `?period=gte:2024` works, even though
`period` is a DATE and DuckDB will not cast `"2024"` to one. An ISO-8601 date sorts
identically as text, which is the whole reason the format is written
biggest-unit-first.

### Aggregating

`group_by` (comma-separated), `agg` (one of `count`, `sum`, `avg`, `min`, `max`,
`median`), and `measure` (the column to aggregate; not needed for `count`). The
thing's own filters apply first, then the caller's, then the grouping.

```bash
# the yearly total of one series
curl 'localhost:8000/api/data/i5demefo/aggregate?group_by=year&agg=sum&measure=value'

# the ten biggest series in the lake since 2020
curl 'localhost:8000/api/data/observations/aggregate?group_by=series&agg=sum&measure=value&period=gte:2020&limit=10'
```

The result carries `truncated` — whether this is the whole ranking or a top-N of a
longer one. A bar chart captioned "the ten biggest" is right; one captioned "all of
them" is not.

### Exports stream

`?format=csv` never materialises the whole result: it goes through DuckDB's
Arrow reader batch by batch, so a client can pull a million rows without the server
buffering them. An export carries the same filters as the page it was downloaded
from, and drops the page — it is the reader's view *without* the limit.

## Rate limiting

In-process token buckets, per client IP, in three tiers matched to cost. No Redis
— a single NUC runs one process, so the state lives in memory and idle clients are
evicted so it cannot grow without bound.

| Tier | Endpoints | Default |
|---|---|---|
| catalog | `/api/tables*` — names, columns, profiles | 120 / min |
| query | `/api/data/*` — rows, aggregates, exports | 30 / min |
| ai | `/api/ai/ask` | 6 / min — each call spawns model requests |

The split is *scanning* versus *describing*: reading a schema is cheap, and reading
a million rows is not. That is exactly the `/api/tables` ÷ `/api/data` line, which is
why the two live under different prefixes.

`/api/health` is never limited. A throttled request gets **429** with a
`Retry-After` header and a readable reason. Tune the ceilings in `.env`:

```bash
LAKE_API_RATE_QUERY_PER_MIN=30
LAKE_API_RATE_AI_PER_MIN=6
LAKE_API_RATE_LIMIT_ENABLED=true
```

**Behind a proxy:** set `LAKE_API_TRUSTED_PROXIES` to your proxy's IP(s). Only
then is `X-Forwarded-For` trusted — otherwise a client could spoof the header and
mint a fresh bucket per fake IP, defeating the limit. With no proxy, the limiter
keys on the socket peer, which cannot be spoofed.

The token bucket gives burst tolerance: a client that has been quiet can spend up
to the full bucket at once, then is throttled to the steady rate. For a hard
per-second cap or a global (not per-client) limit, put nginx or Caddy in front —
this limiter is the application-level backstop, not a DDoS shield.

## Running it

```bash
uv sync --extra api
uv run lake transform gdp_annual       # produce processed/*.parquet
uv run lake transform seki_indicators  # both land in the same table
uv run lake serve build                # materialise the read-only replica
uv run lake serve run                  # start the API on 127.0.0.1:8000

uv run lake serve query "
  SELECT series, value, unit
  FROM lake.observations
  WHERE dataset_id = 'gdp_annual' AND period = DATE '2024-01-01'
  ORDER BY value DESC LIMIT 5"
```

## One table

Every source lands in `lake.observations`. One row is one observation: at `period`,
the series named `series` had `value`, measured in `unit`.

| | |
|---|---|
| `dataset_id` | what was published — `gdp_annual`, `seki_indicators` |
| `table_id` | a statistical table inside it — `TABEL1_1`. NULL when the source publishes only one, as the World Bank does |
| `series` | what the row is a time series *of* — an indicator like `Uang Beredar Luas(M2)`, or a country like `Indonesia` |
| `series_code` | the publisher's own id for it (`IDN`), NULL if none |
| `period` `freq` `value` `unit` | when, how often, how much, in what |

That the World Bank's *country* and Bank Indonesia's *indicator* are the same kind
of thing is the whole reason one table works. A country is exactly what a GDP series
is a series of.

**Always filter on `dataset_id`.** Series names are not unique across datasets — 27
World Bank country names collide with SEKI indicator names — and `value` mixes 19
units, so a `SUM` across datasets is meaningless.

For AI exploration, set `LAKE_ANTHROPIC_API_KEY` in `/etc/lake/lake.env`. Without
it, every other endpoint works and `/api/ai/ask` returns a clear "needs a key"
event rather than failing.

### Serving in production (on the NUC)

The backend is a normal ASGI app. Two ways to run it:

**Single process (default).** `lake serve run` launches uvicorn with one worker.
That is the right choice here — the rate limiter keeps its buckets in memory, and
one process means one shared view of them. On a NUC serving a small team this is
plenty; the DuckDB replica is memory-mapped and queries are milliseconds.

```bash
uv run lake serve run --host 127.0.0.1 --port 8000
```

**As a managed service.** `deploy/systemd/lake-api.service` runs it under systemd
with restart-on-failure, resource caps, and the same sandboxing as the scrapers.
`deploy/systemd/lake-serve-build.timer` rebuilds the replica hourly so new data
appears without a manual step.

```bash
sudo make deploy                              # installs the units
sudo systemctl enable --now lake-serve-build.timer lake-api.service
systemctl status lake-api
```

**Do not add `--workers N`.** Multiple uvicorn workers each get their own
in-memory rate-limit state, so the effective limit becomes N× what you configured,
and it drifts per worker. If you genuinely outgrow one process, move the hard
limit to the reverse proxy (below) and treat the app limiter as a per-worker
backstop — or move limiter state to Redis. Neither is needed at NUC scale.

### The reverse proxy (the real front door)

The app binds to `127.0.0.1` and has **no authentication of its own**. Put
something in front that terminates TLS, adds auth, and — if you want a hard,
global cap rather than the app's per-client one — enforces a connection/rate
limit. nginx:

```nginx
limit_req_zone $binary_remote_addr zone=lake:10m rate=10r/s;

server {
    listen 443 ssl;
    server_name lake.internal;

    location /api/ {
        limit_req zone=lake burst=20 nodelay;   # hard edge limit
        auth_basic "lake";
        auth_basic_user_file /etc/nginx/lake.htpasswd;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header Host $host;
    }
    location / {
        proxy_pass http://127.0.0.1:3000;       # the frontend
    }
}
```

Then set `LAKE_API_TRUSTED_PROXIES=127.0.0.1` so the app reads the real client IP
from the `X-Forwarded-For` nginx adds. For remote-only access with no public
exposure at all, skip nginx and reach the box over Tailscale.

For AI exploration, set `LAKE_ANTHROPIC_API_KEY` in `/etc/lake/lake.env`. Without
it, every other endpoint works and `/api/ai/ask` returns a clear "needs a key"
event rather than failing.

## Frontend

The UI is a separate [TanStack Start](https://tanstack.com/start) app in `web/`. It
server-renders, reaches these routes through a same-origin proxy (`web/src/routes/api.$.ts`),
and streams exports and the AI's SSE straight through rather than buffering them.
FastAPI serves no HTML, no templates, and no static assets — it is the API and
nothing else.

```bash
uv run lake serve run          # the API on http://127.0.0.1:8000
cd web && bun dev              # the site on http://127.0.0.1:3000
```

The pages that read data:

* **Datasets** (`/`) — one card per dataset, with row counts and periods.
* **Dataset detail** (`/dataset/{id}`) — what it is, where it came from, a chart, a
  sample, and copy-paste code in four languages. Its **Browse** button carries the
  filters that isolate it, so a series page opens on that series' rows.
* **Browse** (`/query?id=…`) — one thing's rows, or the whole lake with no id. Add
  filters, sort, page, download. Every control is one query param, so the view a
  reader builds is a link they can send — and a link a dataset page hands them lands
  on the rows, not on a form.
* **Ask AI** (`/ask`) — plain-English questions, streaming the agent's exploration
  (tool calls, results, then the answer) as it happens.

### Exports — the researcher's real ask

A researcher usually wants the spreadsheet, not a query language. An export is not a
separate endpoint — it is `/rows`, asked for as a file:

| Request | Gives |
|---|---|
| `GET /api/data/{id}/rows?format=csv&<filters>` | the thing's rows as CSV |
| `GET /api/data/{id}/rows?format=xlsx&<filters>` | the thing's rows as Excel |
| `GET /api/data/observations/rows?format=csv` | the whole lake as CSV |

Same query string as the JSON page, so a file cannot filter differently from the rows
it was downloaded from — that would be a quiet, undetectable lie. Every rung is
downloadable: a series gives you a series rather than the million rows it sits in. And
the file is **named after the thing**, so downloading M2 puts `M2.csv` in the folder
rather than a fourth `observations.csv` — the URL loses its extension, the file keeps
one, because a file on disk should say what it is.

Which is why the download links in the frontend are plain `<a href>`, and the Python
snippet is one line:

```python
df = pd.read_csv("http://localhost:8000/api/data/i5demefo/rows?format=csv")
```

CSV streams (never buffered) and carries a UTF-8 BOM so Excel opens accented text
correctly. Excel uses openpyxl's streaming writer, bounded to a sliding window of
rows. Both are on the query-tier rate limit, and `COPY ... TO` stays blocked at the
engine — the files are built in Python, and the database never writes to disk.
