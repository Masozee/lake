/**
 * A TanStack Table over a DuckDB table, in *manual* mode.
 *
 * `manualPagination`, `manualSorting`, and `manualFiltering` are the whole point.
 * They tell TanStack Table: you render, you track state, but you do not touch the
 * rows — the database already did the sorting, the filtering, and the paging, and
 * what arrived is exactly the page to show.
 *
 * The alternative — fetch everything, sort in JavaScript — is not slower at this
 * scale, it is broken: `seki_indicators` is 970,700 rows and roughly 24 MB of
 * JSON. This way, 25 rows cross the wire whether the table has a thousand rows or
 * a billion.
 */

import {
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table"
import type { ColumnDef, SortingState } from "@tanstack/react-table"
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  X,
} from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import { admin, errorList } from "@/lib/admin"
import { cell, num } from "@/lib/format"
import type { Cell } from "@/lib/types"

type Column = { name: string; type: string }

type Page = {
  id: string
  table: string
  /** Columns the dataset itself pins. Filtering `group_id` inside a group that IS one
      `group_id` can only return everything or nothing, so the UI disables those
      filters rather than offering a control that cannot do anything. */
  pinned: Array<string>
  /** Columns that hold the same value on every row of this view, and what it is.
      Browsing one series, four of them repeat the same string 304 times and push the
      two columns anyone came for off the screen. So they are stated once, above the
      grid, and folded out of it. */
  constant: Record<string, Cell>
  columns: Array<Column>
  rows: Array<Array<Cell>>
  total: number
  page: number
  size: number
  pages: number
}

type Filter = { column: string; op: string; value: string }

/** What we hang off each column so the header knows which filter controls to show.
    Declared through TanStack's own augmentation point, so `columnDef.meta.type` is
    typed at every use rather than cast back into existence at each one. */
declare module "@tanstack/react-table" {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends unknown, TValue> {
    type?: string
  }
}

const SIZES = [25, 50, 100, 200]

/** Numeric columns get comparison operators; everything else gets text ones. The
    server enforces this too — this only keeps the UI from offering nonsense. */
const NUMERIC = [
  "INT",
  "DEC",
  "DOUBLE",
  "FLOAT",
  "REAL",
  "HUGEINT",
  "NUMERIC",
  "BIGINT",
]
const isNumeric = (type: string) =>
  NUMERIC.some((t) => type.toUpperCase().includes(t))

export function DataTable({
  datasetId,
  rowCount,
}: {
  datasetId: string
  rowCount: number
}) {
  const [page, setPage] = useState(0)
  const [size, setSize] = useState(25)
  const [sorting, setSorting] = useState<SortingState>([])
  const [filters, setFilters] = useState<Array<Filter>>([])

  const [data, setData] = useState<Page | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // A filter is typed into far faster than a million-row scan can answer, so the
  // inputs hold their own value and only the debounced copy is sent.
  const [draft, setDraft] = useState<Array<Filter>>([])
  useEffect(() => {
    const timer = setTimeout(() => {
      // Drop the empties: a filter with no value is a filter the reader has not
      // finished typing, not a request for rows where the column equals "".
      const live = draft.filter(
        (f) => f.value !== "" || f.op === "empty" || f.op === "not_empty"
      )
      setFilters(live)
      setPage(0) // a new filter means a new result set; page 9 of it may not exist
    }, 350)
    return () => clearTimeout(timer)
  }, [draft])

  // Sequence numbers, so a slow response for an old query cannot overwrite a fast
  // one for the current query. Without this, typing quickly can leave the table
  // showing the results of a filter the reader has already changed.
  const latest = useRef(0)

  useEffect(() => {
    const id = ++latest.current
    setLoading(true)

    admin
      .post<Page>(`/data/${encodeURIComponent(datasetId)}`, {
        page,
        size,
        sort: sorting[0]?.id ?? null,
        descending: sorting[0]?.desc ?? false,
        filters,
      })
      .then((body) => {
        if (id !== latest.current) return // a newer request is in flight; discard
        setData(body)
        setError(null)
      })
      .catch((err) => {
        if (id !== latest.current) return
        setError(errorList(err).join(" "))
      })
      .finally(() => {
        if (id === latest.current) setLoading(false)
      })
  }, [datasetId, page, size, sorting, filters])

  const columns = useMemo<Array<ColumnDef<Array<Cell>>>>(() => {
    if (!data) return []
    return (
      data.columns
        // A column with one value on every row is a fact about this view, not a column
        // of data. Repeating `seki_indicators` 304 times pushes `period` and `value`
        // — the two anyone came for — off the right edge. They are stated once above.
        .map((column, i) => ({ column, i }))
        .filter(({ column }) => !(column.name in data.constant))
        .map(({ column, i }) => ({
          id: column.name,
          accessorFn: (row: Array<Cell>) => row[i],
          header: column.name,
          meta: { type: column.type },
        }))
    )
  }, [data])

  const instance = useReactTable({
    data: data?.rows ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
    // The database did all three. Do not re-do them in the browser.
    manualPagination: true,
    manualSorting: true,
    manualFiltering: true,
    pageCount: data?.pages ?? 0,
    state: { sorting },
    onSortingChange: (updater) => {
      setSorting((old) =>
        typeof updater === "function" ? updater(old) : updater
      )
      setPage(0) // a new sort order means page 1 of it
    },
  })

  const setFilter = (column: string, patch: Partial<Filter>) =>
    setDraft((old) => {
      const rest = old.filter((f) => f.column !== column)
      const current = old.find((f) => f.column === column) ?? {
        column,
        op: "contains",
        value: "",
      }
      const next = { ...current, ...patch }
      // Drop it entirely when there is nothing left to say.
      if (!next.value && next.op !== "empty" && next.op !== "not_empty")
        return rest
      return [...rest, next]
    })

  const clearFilters = () => setDraft([])

  const total = data?.total ?? rowCount
  const filtered = data ? data.total !== rowCount : false

  return (
    <>
      <div className="table-toolbar">
        <span className="muted" style={{ fontSize: "0.8125rem" }}>
          {loading ? (
            "Loading…"
          ) : filtered ? (
            <>
              <strong>{num(total)}</strong> of {num(rowCount)} rows match
            </>
          ) : (
            <>
              <strong>{num(total)}</strong> rows
            </>
          )}
        </span>

        {draft.length > 0 && (
          <button
            type="button"
            className="btn btn-ghost"
            onClick={clearFilters}
          >
            <X size={14} /> Clear {draft.length} filter
            {draft.length === 1 ? "" : "s"}
          </button>
        )}

        <label
          className="spacer hstack"
          style={{ gap: "0.5rem", fontSize: "0.8125rem" }}
        >
          <span className="muted">Rows</span>
          <select
            value={size}
            onChange={(e) => {
              setSize(Number(e.target.value))
              setPage(0)
            }}
            className="page-size"
          >
            {SIZES.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
      </div>

      {/* What every row of this view has in common, said once instead of 304 times.
          Not hidden — folded: the columns are still in the table, and this is what
          they hold. Without saying so, a reader would wonder where `series` went. */}
      {data && Object.keys(data.constant).length > 0 && (
        <p className="grid-constants">
          {Object.entries(data.constant).map(([column, value]) => (
            <span className="grid-constant" key={column}>
              <span className="mono grid-constant-key">{column}</span>
              <span className="mono">
                {value === null ? "∅" : String(value)}
              </span>
            </span>
          ))}
          <span className="muted">on every row</span>
        </p>
      )}

      {error && (
        <div className="notice notice-error" role="alert">
          {error}
        </div>
      )}

      <div className="table-wrap data-grid">
        <table>
          <thead>
            <tr>
              {instance.getHeaderGroups()[0]?.headers.map((header) => {
                const type = header.column.columnDef.meta?.type ?? ""
                const sorted = header.column.getIsSorted()
                return (
                  <th key={header.id}>
                    <button
                      type="button"
                      className="col-sort"
                      onClick={header.column.getToggleSortingHandler()}
                      title={`Sort by ${header.id} (${type.toLowerCase()})`}
                    >
                      <span className="mono">
                        {flexRender(
                          header.column.columnDef.header,
                          header.getContext()
                        )}
                      </span>
                      {sorted === "asc" && <ArrowUp size={12} />}
                      {sorted === "desc" && <ArrowDown size={12} />}
                    </button>
                    <ColumnFilter
                      type={type}
                      pinned={data?.pinned.includes(header.id) ?? false}
                      value={draft.find((f) => f.column === header.id)}
                      onChange={(patch) => setFilter(header.id, patch)}
                    />
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {instance.getRowModel().rows.map((row) => (
              <tr key={row.id}>
                {row.getVisibleCells().map((c) => {
                  // The column decides whether a number is a quantity or a label:
                  // `year` holds 2001, and `2,001` is a count of something.
                  const { text, numeric, empty } = cell(
                    c.getValue() as Cell,
                    c.column.id
                  )
                  return (
                    <td
                      key={c.id}
                      className={
                        empty ? "cell-null" : numeric ? "data-mono" : undefined
                      }
                      style={{ textAlign: numeric ? "right" : undefined }}
                      title={empty ? "null" : text}
                    >
                      {text}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>

        {!loading && data?.rows.length === 0 && (
          <p className="muted" style={{ padding: "2rem", textAlign: "center" }}>
            No rows match these filters.
          </p>
        )}
      </div>

      <div className="pager">
        <span className="muted" style={{ fontSize: "0.8125rem" }}>
          Page {num((data?.page ?? 0) + 1)} of{" "}
          {num(Math.max(data?.pages ?? 1, 1))}
        </span>
        <div className="spacer hstack" style={{ gap: "0.25rem" }}>
          <PageButton
            onClick={() => setPage(0)}
            disabled={page === 0}
            label="First page"
          >
            <ChevronsLeft size={16} />
          </PageButton>
          <PageButton
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            label="Previous page"
          >
            <ChevronLeft size={16} />
          </PageButton>
          <PageButton
            onClick={() => setPage((p) => p + 1)}
            disabled={!data || page >= data.pages - 1}
            label="Next page"
          >
            <ChevronRight size={16} />
          </PageButton>
          <PageButton
            onClick={() => setPage(Math.max(0, (data?.pages ?? 1) - 1))}
            disabled={!data || page >= data.pages - 1}
            label="Last page"
          >
            <ChevronsRight size={16} />
          </PageButton>
        </div>
      </div>
    </>
  )
}

function PageButton({
  onClick,
  disabled,
  label,
  children,
}: {
  onClick: () => void
  disabled: boolean
  label: string
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      className="btn btn-outline"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      style={{ minHeight: "32px", padding: "0 8px" }}
    >
      {children}
    </button>
  )
}

/** The per-column filter under each header.

    Numeric columns get comparisons; text columns get substring and exact match.
    Offering `>` on a country name would only ever produce a 422 from the server. */
function ColumnFilter({
  type,
  pinned,
  value,
  onChange,
}: {
  type: string
  /** The dataset already filters on this column. Filtering it again could only
      return everything or nothing, so say so rather than offer a dead control. */
  pinned: boolean
  value: Filter | undefined
  onChange: (patch: Partial<Filter>) => void
}) {
  const numeric = isNumeric(type)
  const op = value?.op ?? (numeric ? "gt" : "contains")
  const needsValue = op !== "empty" && op !== "not_empty"

  if (pinned) {
    return (
      <div className="col-filter">
        <span
          className="col-pinned"
          title="This dataset is defined by this column"
        >
          fixed by this dataset
        </span>
      </div>
    )
  }

  return (
    <div className="col-filter">
      <select
        value={op}
        onChange={(e) => onChange({ op: e.target.value })}
        aria-label="Filter operator"
      >
        {numeric ? (
          <>
            <option value="gt">&gt;</option>
            <option value="gte">≥</option>
            <option value="lt">&lt;</option>
            <option value="lte">≤</option>
            <option value="equals">=</option>
          </>
        ) : (
          <>
            <option value="contains">has</option>
            <option value="equals">is</option>
          </>
        )}
        <option value="empty">is null</option>
        <option value="not_empty">not null</option>
      </select>
      <input
        type={numeric && needsValue ? "number" : "text"}
        value={value?.value ?? ""}
        disabled={!needsValue}
        onChange={(e) => onChange({ value: e.target.value })}
        placeholder={needsValue ? "filter" : "—"}
        aria-label="Filter value"
      />
    </div>
  )
}
