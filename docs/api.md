# Serving API & AI exploration

A read-only HTTP API over the lake, plus an AI agent that can explore the data
but cannot change it. A TanStack Start frontend sits on top.

```
processed/*.parquet ──lake serve build──> serving.duckdb (read-only replica)
                                                   │
                                    read_only=True, enable_external_access=False
                                                   │
        ┌──────────────────────────────────────────┼─────────────────────┐
        │                                           │                     │
   GET /api/tables                           POST /api/query        POST /api/ai/ask
   GET /api/tables/{t}                        (single SELECT)        (Claude + read-only tools)
   POST /api/query/stream (NDJSON)                  │                     │
        └──────────────────── TanStack Start frontend ───────────────────┘
```

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

### Layer 2 — the SQL guard rejects non-reads before they run

`src/lake/api/sql_guard.py` parses every query with DuckDB's own parser
(`extract_statements`) and rejects anything that is not a single SELECT or
EXPLAIN. It exists to give clear errors and to survive someone later loosening
layer 1 — not because layer 1 needs help.

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
| POST | `/api/query` | run one SELECT, return JSON (capped) |
| POST | `/api/query/stream` | run one SELECT, stream NDJSON (never buffered) |
| POST | `/api/ai/ask` | ask the AI; streams SSE events |

Rejected queries return **422** with a readable reason. A query that runs too
long returns **408**. Nothing returns a 200 with data it should not have.

### Streaming

`/api/query/stream` returns newline-delimited JSON: a header line
`{"columns":[...]}` then one JSON array per row. The server uses DuckDB's Arrow
reader and never materialises the whole result — a client can pull a large table
without the server buffering it. Row and time ceilings still apply.

## Rate limiting

In-process token buckets, per client IP, in three tiers matched to cost. No Redis
— a single NUC runs one process, so the state lives in memory and idle clients are
evicted so it cannot grow without bound.

| Tier | Endpoints | Default |
|---|---|---|
| catalog | `/api/tables*` | 120 / min |
| query | `/api/query`, `/api/query/stream` | 30 / min |
| ai | `/api/ai/ask` | 6 / min — each call spawns model requests |

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
uv run lake transform gdp_annual     # produce processed/*.parquet
uv run lake serve build              # materialise the read-only replica
uv run lake serve run                # start the API on 127.0.0.1:8000

uv run lake serve query "SELECT country_iso3, sum(gdp_usd) FROM lake.gdp_annual GROUP BY 1"
```

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

## Frontend (htmx, served by FastAPI)

The UI is server-rendered HTML with htmx and the [Basecoat](https://basecoatui.com)
component library (shadcn-style components as plain CSS) — no Node, no build step,
no separate server. It lives in `src/lake/api/templates/` and
`src/lake/api/static/`, and the same FastAPI process that serves the API serves
the pages. Start it and open the browser:

```bash
uv run lake serve run          # API + UI on http://127.0.0.1:8000
```

htmx, its SSE extension, and Basecoat's CSS + JS are all **vendored** in `static/`
— no CDN, so the whole UI works on an air-gapped NUC and behind a firewall.
Basecoat brings the design system (buttons, cards, tables, inputs, badges, alerts,
light/dark theming via a `.dark` class); a small `app.css` adds only the bar chart,
the theme toggle, and the AI-chat bubbles, all styled from Basecoat's own theme
tokens so they track the theme automatically. Three pages:

* **Datasets** (`/`) — every dataset as a big click target with row/column counts.
* **Dataset detail** (`/table/{name}`) — columns, distinct-value profiles, a
  sample, and **Download CSV / Download Excel** for the whole dataset.
* **Query** (`/query`) — a SQL box. On submit, htmx posts to `/query/run` and
  swaps in a fragment: a bar chart (when the shape fits), a table, and CSV/Excel
  download links for the exact result. A rejected query shows the guard's reason
  inline, never a 500.
* **Ask AI** (`/ask`) — plain-English questions. A small fetch reader streams the
  agent's exploration (tool calls, results, then the answer) as it happens; every
  other interaction on the site is plain server-rendered HTML.

Chart colours come from the validated categorical palette in the dataviz skill —
CVD-safe in light and dark, every bar directly value-labelled. The base font is
larger than a typical dashboard: the audience is researchers reading real numbers.

### Exports — the researcher's real ask

A researcher usually wants the spreadsheet, not a SQL prompt. Four endpoints:

| Endpoint | Gives |
|---|---|
| `GET /api/tables/{t}/export.csv` | whole table as CSV |
| `GET /api/tables/{t}/export.xlsx` | whole table as Excel |
| `GET /api/query/export.csv?sql=…` | a query result as CSV |
| `GET /api/query/export.xlsx?sql=…` | a query result as Excel |

CSV streams (never buffered) and carries a UTF-8 BOM so Excel opens accented text
correctly. Excel uses openpyxl's streaming writer, bounded to a sliding window of
rows. Both go through the same read-only guard and query-tier rate limit, and
`COPY ... TO` stays blocked at the engine — the files are built in Python, the
database never writes to disk.
