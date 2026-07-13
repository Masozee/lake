# web

The public frontend: TanStack Start, React, shadcn/ui. It replaced the htmx UI
that used to be served out of `src/lake/api/routes/ui.py`.

It holds no data and talks to nothing but the lake API.

## Running it

Two processes. The API must be up first — every page is empty without it.

```bash
make serve   # terminal 1: the API on :8000
make web     # terminal 2: this app on :3000
```

`LAKE_API_URL` says where the API is; it defaults to `http://127.0.0.1:8000`.
See `.env.example`.

```bash
make web-check   # typecheck + lint
make web-build   # production build
```

## How it reaches the data

Every page loads its data in a **server function** (`src/lib/server.ts`), which
runs on this app's server and calls the API's `/api/ui/*` endpoints. The browser
never talks to the API directly: it binds to localhost and carries no
authentication of its own, so it is not something a public page may fetch.

Two things do need the browser to reach the API, and neither can go through a
server function:

- **exports** (CSV, Excel) — a link the browser follows itself; a server function
  would have to buffer the whole file to hand it back
- **the AI answer** — a live SSE stream the page renders as it arrives

Both go through `src/routes/api.$.ts`, which proxies `/api/*` to FastAPI and pipes
the body rather than buffering it. That also means one origin: no CORS, and the
API's address never ships to a client.

```
browser ──▶ web (:3000) ──▶ API (:8000) ──▶ DuckDB replica
            server fns          /api/ui/*
            /api/* proxy        /api/data/{id}/rows, /api/ai/ask, exports
```

## The design

IBM Carbon: IBM Plex, Blue 60, and a zero radius — Carbon does not round. The
tokens are in `src/styles.css` and are the same ones the htmx UI used, so the two
look identical; only the renderer changed. shadcn's rounding and shadows are
turned off globally, on purpose.

Motion is additive: the hidden state is applied by script, so with JS off — or
with `prefers-reduced-motion` — every element renders in its final visible state.
Nothing important is gated behind an animation.

## Filters are URLs

`/datasets` keeps its search and filters in the query string, so a filtered view
is a link you can send someone. Empty filters are dropped rather than written as
`?q=&kind=`.

## The admin panel

`/admin` is the one part of this app that is private and that writes. It does
**not** use server functions: there is nothing to server-render for a crawler, and
the session lives in an `httpOnly` cookie the browser holds — so it calls
`/api/admin/*` directly (with `credentials: "same-origin"`) and the proxy forwards
the cookie along.

The login gate in `routes/admin.tsx` is a convenience, not the security boundary.
The real one is the API: every `/api/admin/*` route requires a session and 401s
without it, so a reader who bypasses the component reaches a panel with no data in
it. The gate exists so they see a login form instead of a wall of failed requests.

Create the first account from a shell — there is no sign-up:

```bash
lake admin create-user you@example.org
```

See [../docs/admin.md](../docs/admin.md).
