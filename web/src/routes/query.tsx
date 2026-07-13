import { Link, createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState } from "react"
import { ResultTable } from "@/components/blocks"
import { num } from "@/lib/format"
import { OBSERVATIONS, exportUrl, rowsUrl } from "@/lib/query"
import type { DataQuery, Operator } from "@/lib/query"
import type { Column, RowsResult, TableInfo } from "@/lib/types"

/**
 * Browse the lake without writing SQL.
 *
 * This page used to be a SQL editor. It is now a filter over the REST API, and the
 * reason is not that SQL was dangerous — the engine is read-only with the filesystem
 * off, and it never was — but that a query language is a *contract*, and almost nobody
 * arriving here wanted to author one. They wanted the rows for one series since 2010,
 * and the SQL was a toll on the way.
 *
 * What it reads is a *thing*: `?id=i5demefo` is one series, and `?id=observations` is
 * the whole lake. A dataset page links here with its own id already set, so a reader
 * arrives at that thing's rows and filters within them — the id fixes the slice, and
 * the filters narrow it.
 *
 * Every control is one query param, and the URL is the whole state. So a view a reader
 * builds here is a link they can send, and a link a dataset page hands them lands on
 * the rows rather than on a form they still have to submit.
 */

/** Rows per page. The API will serve far more; this is what a screen can hold. */
const PAGE = 100

/** The comparisons a reader can pick, in the order they are likely to want them.
    Mirrors `lake.api.rows.OPERATORS` — the API rejects anything else. */
const OPERATORS: Array<{ id: Operator; label: string; valueless?: boolean }> = [
  { id: "eq", label: "is" },
  { id: "contains", label: "contains" },
  { id: "starts", label: "starts with" },
  { id: "ne", label: "is not" },
  { id: "gte", label: "≥" },
  { id: "lte", label: "≤" },
  { id: "gt", label: ">" },
  { id: "lt", label: "<" },
  { id: "in", label: "is one of" },
  { id: "null", label: "is empty", valueless: true },
  { id: "notnull", label: "is not empty", valueless: true },
]

const VALUELESS = new Set(
  OPERATORS.filter((o) => o.valueless).map((o) => o.id as string)
)

type Search = {
  /** The thing to read: an 8-char id, or `observations` for the whole lake. */
  id?: string
  sort?: string
  desc?: boolean
  offset?: number
  /** Every other param is a filter: `period=gte:2000`. They cannot be named in
      advance — they are the table's own columns — so they ride as a bag. */
  filters?: Record<string, string>
}

/** Everything that is not one of ours is a filter on a column. */
const RESERVED = new Set(["id", "sort", "desc", "offset"])

export const Route = createFileRoute("/query")({
  validateSearch: (search: Record<string, unknown>): Search => {
    const filters: Record<string, string> = {}
    for (const [key, value] of Object.entries(search)) {
      if (RESERVED.has(key)) continue
      if (typeof value === "string" && value) filters[key] = value
    }
    return {
      id: typeof search.id === "string" ? search.id : undefined,
      sort: typeof search.sort === "string" ? search.sort : undefined,
      desc: search.desc === true || search.desc === "true" || search.desc === "",
      offset: Number(search.offset) || undefined,
      filters: Object.keys(filters).length ? filters : undefined,
    }
  },
  head: () => ({ meta: [{ title: "Browse · lake" }] }),
  component: BrowsePage,
})

/** A filter as the form holds it — split, because the reader edits the parts. */
type Draft = { column: string; op: Operator; value: string }

function toDrafts(filters: Record<string, string>): Array<Draft> {
  return Object.entries(filters).map(([column, raw]) => {
    const [head, ...rest] = raw.split(":")
    const known = OPERATORS.some((o) => o.id === head)
    // A value with no operator means equality — `?freq=annual` should do the obvious
    // thing — and a colon inside a value ("10:30") is not an operator.
    return known && rest.length
      ? { column, op: head as Operator, value: rest.join(":") }
      : { column, op: "eq" as Operator, value: raw }
  })
}

function toFilters(drafts: Array<Draft>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const d of drafts) {
    if (!d.column) continue
    if (VALUELESS.has(d.op)) out[d.column] = `${d.op}:`
    else if (d.value) out[d.column] = `${d.op}:${d.value}`
  }
  return out
}

function BrowsePage() {
  const search = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })

  const [columns, setColumns] = useState<Array<Column>>([])
  const [result, setResult] = useState<RowsResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  // No id means the whole lake. It is the one thing addressed by name rather than by
  // id, because it is not a dataset — it is what all of them are views of.
  const id = search.id ?? OBSERVATIONS
  const filters = search.filters ?? {}
  const offset = search.offset ?? 0

  const query: DataQuery = {
    id,
    filters,
    sort: search.sort ?? null,
    descending: search.desc ?? false,
    limit: PAGE,
    offset,
  }

  // The form is a draft of the URL, not the URL itself: a reader half-way through
  // typing a filter has not asked for anything yet, and re-running on every keystroke
  // would be a query per character.
  const [drafts, setDrafts] = useState<Array<Draft>>(() => toDrafts(filters))
  useEffect(() => setDrafts(toDrafts(filters)), [JSON.stringify(filters)])

  // The URL is the query. Anything that changes it runs the read — so the back button
  // works, and a link someone was sent lands on the rows.
  useEffect(() => {
    let live = true
    setLoading(true)
    setError(null)

    void fetch(rowsUrl(query))
      .then(async (r) => {
        const body = await r.json()
        if (!live) return
        if (!r.ok) {
          // 422 names the columns that do exist, 404 says the id resolves to nothing —
          // both are things the reader can act on, so show what the server said.
          setResult(null)
          setError(
            typeof body.detail === "string"
              ? body.detail
              : `Request failed (${r.status})`
          )
          return
        }
        setResult(body as RowsResult)
      })
      .catch((e) => live && setError(String(e)))
      .finally(() => live && setLoading(false))

    return () => {
      live = false
    }
  }, [rowsUrl(query)])

  // Which columns a reader may filter on. The id resolves to a table, and the read
  // says which — so this follows the result rather than being fetched in parallel with
  // it and racing.
  const table = result?.table
  useEffect(() => {
    if (!table) return
    void fetch(`/api/tables/${table}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((info: TableInfo | null) => setColumns(info?.columns ?? []))
      .catch(() => setColumns([]))
  }, [table])

  /** Apply the form. Paging resets — page 3 of the old filter is not page 3 of this
      one, and landing there would show a hole. */
  function apply(next: Partial<Search> = {}) {
    void navigate({
      search: {
        // Only when it is not the default: `/query` should stay `/query`, not become
        // `/query?id=observations`. A URL that states its own default is noise a reader
        // then has to decide whether to keep when they copy it.
        id: search.id,
        sort: search.sort,
        desc: search.desc,
        ...toFilters(drafts),
        ...next,
        offset: next.offset ?? 0,
      } as never,
      replace: false,
    })
  }

  function sortBy(column: string) {
    const same = search.sort === column
    apply({ sort: column, desc: same ? !search.desc : true } as Partial<Search>)
  }

  const shown = result ? offset + result.rows.length : 0
  const canPage = result ? result.total > PAGE : false

  const whole = id === OBSERVATIONS

  return (
    <main className="wrap page-pad">
      <h1 className="page-title">Browse</h1>
      <p className="page-sub">
        {whole ? (
          <>
            Every observation in the lake. Filter it, sort it, take it away.
          </>
        ) : (
          <>
            Reading <code className="mono">{id}</code> —{" "}
            <Link to="/dataset/$id" params={{ id }}>
              what is this?
            </Link>{" "}
            ·{" "}
            <Link to="/query" search={{} as never}>
              browse the whole lake instead
            </Link>
          </>
        )}{" "}
        Read-only — this page cannot change the data. Every control here is one
        parameter of the URL, so the view you build is a link you can send.
      </p>

      <form
        className="form mb-4"
        style={{ maxWidth: "none" }}
        onSubmit={(e) => {
          e.preventDefault()
          apply()
        }}
      >
        <fieldset className="field" style={{ border: 0, padding: 0 }}>
          <legend>Filters</legend>
          {drafts.map((draft, i) => (
            <div className="hstack mb-4" key={i}>
              <select
                aria-label="Column"
                className="mono"
                value={draft.column}
                onChange={(e) =>
                  setDrafts(
                    drafts.map((d, j) =>
                      j === i ? { ...d, column: e.target.value } : d
                    )
                  )
                }
              >
                <option value="">column…</option>
                {columns.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </select>

              <select
                aria-label="Comparison"
                value={draft.op}
                onChange={(e) =>
                  setDrafts(
                    drafts.map((d, j) =>
                      j === i ? { ...d, op: e.target.value as Operator } : d
                    )
                  )
                }
              >
                {OPERATORS.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.label}
                  </option>
                ))}
              </select>

              {!VALUELESS.has(draft.op) && (
                <input
                  aria-label="Value"
                  className="mono"
                  value={draft.value}
                  placeholder={draft.op === "in" ? "a,b,c" : "value"}
                  onChange={(e) =>
                    setDrafts(
                      drafts.map((d, j) =>
                        j === i ? { ...d, value: e.target.value } : d
                      )
                    )
                  }
                />
              )}

              <button
                type="button"
                className="btn btn-ghost"
                aria-label={`Remove filter on ${draft.column || "column"}`}
                onClick={() => setDrafts(drafts.filter((_, j) => j !== i))}
              >
                ✕
              </button>
            </div>
          ))}

          <div className="hstack">
            <button
              type="button"
              className="btn btn-outline"
              onClick={() =>
                setDrafts([...drafts, { column: "", op: "eq", value: "" }])
              }
            >
              Add filter
            </button>
            <button type="submit" className="btn btn-primary" disabled={loading}>
              {loading ? "Loading…" : "Apply"}
            </button>
          </div>
        </fieldset>
      </form>

      {error && (
        <div className="notice notice-error" role="alert">
          <strong>Request rejected.</strong>
          <p style={{ margin: "0.5rem 0 0" }}>{error}</p>
        </div>
      )}

      {result && !error && (
        <>
          <div className="hstack mb-4">
            <span className="muted">
              {num(result.total)} row{result.total === 1 ? "" : "s"} match
              {result.total === 1 ? "es" : ""}
              {shown < result.total && `, showing ${num(shown - offset)}`}. Take
              them:
            </span>
            {/* No filename: the server names the file after the thing itself, so a
                reader downloading one series gets its name rather than a fourth
                `observations.csv`. */}
            <a className="btn btn-outline" href={exportUrl(query, "csv")}>
              CSV
            </a>
            <a className="btn btn-outline" href={exportUrl(query, "xlsx")}>
              Excel
            </a>
          </div>

          {/* The header cells sort. It is the one thing a reader always wants and the
              only way to reach the other end of a million rows without paging there. */}
          <div className="hstack mb-4" role="group" aria-label="Sort">
            <span className="muted">Sort:</span>
            {result.columns.map((c) => (
              <button
                key={c}
                type="button"
                className={`btn ${search.sort === c ? "btn-primary" : "btn-ghost"}`}
                onClick={() => sortBy(c)}
              >
                {c}
                {search.sort === c && (search.desc ? " ↓" : " ↑")}
              </button>
            ))}
          </div>

          <ResultTable result={result} />

          {canPage && (
            <div className="hstack mb-4" style={{ marginTop: "1rem" }}>
              <button
                type="button"
                className="btn btn-outline"
                disabled={offset === 0}
                onClick={() => apply({ offset: Math.max(0, offset - PAGE) })}
              >
                ← Previous
              </button>
              <span className="muted">
                {num(offset + 1)}–{num(shown)} of {num(result.total)}
              </span>
              <button
                type="button"
                className="btn btn-outline"
                disabled={!result.has_more}
                onClick={() => apply({ offset: offset + PAGE })}
              >
                Next →
              </button>
            </div>
          )}
        </>
      )}
    </main>
  )
}
