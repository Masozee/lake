import { Link, createFileRoute } from "@tanstack/react-router"
import {
  Boxes,
  ChevronLeft,
  ChevronRight,
  ChevronRight as Caret,
  Database,
  Layers,
  LineChart,
  PauseCircle,
  Rss,
  Search,
} from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { admin, errorList } from "@/lib/admin"
import { day, num } from "@/lib/format"
import type { Level } from "@/lib/types"

export const Route = createFileRoute("/admin/data/")({
  validateSearch: (search: Record<string, unknown>): { parent?: string } =>
    typeof search.parent === "string" && search.parent
      ? { parent: search.parent }
      : {},
  component: AdminData,
})

/** One thing in the list.

    `raw` is the whole merged DuckDB table; `dataset` is what one source published;
    `group` is a group inside that — a statistical table for Bank Indonesia, an
    indicator for the World Bank; `series` is one line of numbers inside the group.

    `source` is the odd one: a source that has collected NOTHING. It has no id and
    nothing inside it, so it cannot be opened — but it gets a row anyway, because a
    source that is not producing is exactly what an admin opens this page to find.

    Everything from `description` down is set only on the top two rungs. Below them the
    source is inherited, and repeating it on 4,178 series rows would be noise. */
type Item = {
  id: string | null
  title: string
  level: Level
  parent_title: string | null
  row_count: number | null
  unit: string | null
  freq: string | null
  first_period: string | null
  last_period: string | null
  openable: boolean

  description?: string | null
  /** Our internal key — `gdp_annual`. Not the title: nobody searches for it, but
      whoever is writing a query needs it. */
  dataset_id?: string | null
  source_id?: string | null
  kind?: string | null
  schedule?: string | null
  enabled?: boolean

  /** Freshness, from the catalog database. ABSENT when Postgres is down — not false,
      because "we could not check" and "it is fine" are not the same statement. */
  is_stale?: boolean
  last_success_at?: string | null
  hours_since_success?: number | null
  sla_hours?: number | null
}

type Crumb = { id: string; title: string }

type Children = {
  items: Array<Item>
  total: number
  page: number
  pages: number
  crumbs: Array<Crumb>
}

const ICON: Record<Level, typeof Database> = {
  raw: Database,
  source: Rss,
  dataset: Boxes,
  group: Layers,
  series: LineChart,
}

const NOUN: Record<Level, string> = {
  raw: "the whole table",
  source: "collected, nothing published yet",
  dataset: "dataset",
  group: "group",
  series: "series",
}

function AdminData() {
  // Where the list is pointed, in the URL rather than in state — so a level is
  // linkable, Back walks up it, and a reader coming out of a detail page lands
  // beside the thing they were just looking at rather than at the root.
  const { parent = "" } = Route.useSearch()
  const navigate = Route.useNavigate()

  const [page, setPage] = useState(0)
  const [query, setQuery] = useState("")

  const [data, setData] = useState<Children | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  // A new level is a new result set: page 3 of it, and a filter typed for the old
  // one, are both meaningless here.
  useEffect(() => {
    setPage(0)
    setQuery("")
  }, [parent])

  // A filter is typed into faster than the server answers, so only the settled
  // value is sent.
  const [needle, setNeedle] = useState("")
  useEffect(() => {
    const timer = setTimeout(() => {
      setNeedle(query.trim())
      setPage(0) // a new filter means a new result set; page 3 of it may not exist
    }, 250)
    return () => clearTimeout(timer)
  }, [query])

  // Sequence numbers, so a slow response for an old query cannot overwrite a fast
  // one for the current query. Without this, typing a filter can leave the list
  // showing the unfiltered results that were still in flight when you started.
  const latest = useRef(0)

  useEffect(() => {
    const seq = ++latest.current
    setLoading(true)

    const params = new URLSearchParams({ parent, page: String(page) })
    if (needle) params.set("q", needle)

    admin
      .get<Children>(`/data?${params}`)
      .then((body) => {
        if (seq !== latest.current) return // a newer request is in flight; discard
        setData(body)
        setError(null)
      })
      .catch((err) => {
        if (seq !== latest.current) return
        setError(errorList(err).join(" "))
      })
      .finally(() => {
        if (seq === latest.current) setLoading(false)
      })
  }, [parent, page, needle])

  const goTo = (next: string) =>
    void navigate({ search: next ? { parent: next } : {} })

  const atRoot = parent === ""

  return (
    <>
      <p className="section-lead" style={{ marginBottom: "1rem" }}>
        {atRoot ? (
          <>
            Every source the lake collects, and what it has published. A dataset
            is a filtered view of the one{" "}
            <code className="mono">observations</code> table, a group is a view
            of that, and a series is one line of numbers inside the group — so
            this drills down rather than listing four thousand things at once.
          </>
        ) : (
          <>
            Every source lands in one table,{" "}
            <code className="mono">observations</code>. A dataset is a filtered
            view of it, a group is a view of that, and a series is one line of
            numbers inside the group.
          </>
        )}
      </p>

      <nav className="drill-crumbs" aria-label="Breadcrumb">
        <button
          type="button"
          className={`crumb ${atRoot ? "crumb-on" : ""}`}
          onClick={() => goTo("")}
        >
          All data
        </button>
        {data?.crumbs.map((crumb, i) => (
          <span key={crumb.id} className="contents">
            <Caret size={13} className="crumb-sep" aria-hidden="true" />
            <button
              type="button"
              // The last crumb is the level you are on, not a link back to it.
              className={`crumb ${i === data.crumbs.length - 1 ? "crumb-on" : ""}`}
              onClick={() => goTo(crumb.id)}
            >
              {crumb.title}
            </button>
          </span>
        ))}
      </nav>

      <div
        className="field"
        style={{ maxWidth: "26rem", marginBottom: "1rem" }}
      >
        <label htmlFor="find">
          <Search size={12} style={{ display: "inline", marginRight: 4 }} />
          Filter this level
        </label>
        <input
          id="find"
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoComplete="off"
          placeholder={
            atRoot ? "Filter by name or description" : "Filter by name"
          }
        />
      </div>

      {error && <p className="notice notice-error">{error}</p>}
      {loading && !data && <p className="muted">Loading…</p>}

      {data && (
        <>
          <p className="result-count muted">
            {data.total === 0
              ? "Nothing here."
              : `${num(data.total)} ${data.total === 1 ? "item" : "items"}`}
          </p>

          <ul className="drill-list">
            {data.items.map((item) => (
              <li key={item.id ?? item.source_id ?? item.title}>
                <Row item={item} onDrill={goTo} />
              </li>
            ))}
          </ul>

          {data.pages > 1 && (
            <nav className="pager" aria-label="Pagination">
              <span className="muted" style={{ fontSize: "0.8125rem" }}>
                Page {num(data.page + 1)} of {num(data.pages)}
              </span>
              <span className="spacer hstack" style={{ gap: "0.25rem" }}>
                <button
                  type="button"
                  className="btn btn-outline"
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={data.page === 0}
                  aria-label="Previous page"
                  style={{ minHeight: "32px", padding: "0 8px" }}
                >
                  <ChevronLeft size={16} />
                </button>
                <button
                  type="button"
                  className="btn btn-outline"
                  onClick={() => setPage((p) => p + 1)}
                  disabled={data.page >= data.pages - 1}
                  aria-label="Next page"
                  style={{ minHeight: "32px", padding: "0 8px" }}
                >
                  <ChevronRight size={16} />
                </button>
              </span>
            </nav>
          )}
        </>
      )}
    </>
  )
}

/**
 * One row.
 *
 * A source that has published nothing has no id and no page, so it is not a link — it
 * is a statement that we are collecting something and have nothing to show for it yet.
 * Everything else opens.
 */
function Row({ item, onDrill }: { item: Item; onDrill: (id: string) => void }) {
  const Icon = ICON[item.level]

  const body = (
    <>
      <Icon size={15} className="drill-icon" aria-hidden="true" />

      <span className="drill-main">
        <span className="drill-title">
          <span className={item.level === "raw" ? "mono" : undefined}>
            {item.title}
          </span>
          <Freshness item={item} />
        </span>

        <span className="drill-sub">
          {/* The keys and the shape: what this is, where it came from, how often it is
              collected, and what it covers. `gdp_annual` lives here rather than in the
              title — it is our internal key, not a name anyone is searching for. */}
          {item.dataset_id ? (
            <code className="mono">{item.dataset_id}</code>
          ) : (
            NOUN[item.level]
          )}
          {item.kind && ` · ${item.kind}`}
          {item.schedule && ` · ${item.schedule}`}
          {item.first_period && item.last_period && (
            <>
              {" · "}
              {day(item.first_period).slice(-4)}–
              {day(item.last_period).slice(-4)}
            </>
          )}
          {item.unit && ` · ${item.unit}`}
        </span>

        {/* What the source says it publishes. The one line on this page that tells a
            reader who does not already know what any of this IS. */}
        {item.description && (
          <span className="drill-desc">{item.description}</span>
        )}
      </span>

      <span className="drill-rows data-mono">
        {item.row_count === null ? "—" : num(item.row_count)}
      </span>
    </>
  )

  // Nothing published: no id, so no page to open. The row still says what is being
  // collected, which is the whole reason it is here.
  if (!item.id) {
    return <span className="drill-row drill-row-flat">{body}</span>
  }

  return (
    <span className="drill-pair">
      {/* Every rung with an id opens its own page. The caret is a second target: it
          drills into what is inside without leaving the list. */}
      <Link
        to="/admin/data/$id"
        params={{ id: item.id }}
        className="drill-row reset"
      >
        {body}
      </Link>

      {item.openable && (
        <button
          type="button"
          className="drill-into"
          onClick={() => onDrill(item.id as string)}
          aria-label={`Open what is inside ${item.title}`}
        >
          <Caret size={15} aria-hidden="true" />
        </button>
      )}
    </span>
  )
}

/**
 * Whether a source is keeping to its collection schedule.
 *
 * Three states, and the third is the one that matters: `undefined` means the catalog
 * database did not answer, so we do not know — and a page that showed "fresh" there
 * would be lying about the one thing an admin came to check.
 *
 * A *paused* source is not stale. It is off because someone turned it off, and calling
 * that a fault would page a person at 3am about a decision they made themselves.
 */
function Freshness({ item }: { item: Item }) {
  if (item.enabled === false) {
    return (
      <span className="badge badge-paused">
        <PauseCircle size={11} aria-hidden="true" />
        paused
      </span>
    )
  }

  if (item.is_stale === undefined) return null

  if (item.is_stale) {
    const late =
      item.hours_since_success != null && item.sla_hours != null
        ? `${Math.round(item.hours_since_success)}h since last run, SLA ${item.sla_hours}h`
        : "has never landed a successful run"
    return (
      <span className="badge badge-stale" title={late}>
        stale
      </span>
    )
  }

  return (
    <span
      className="badge badge-fresh"
      title={
        item.hours_since_success != null
          ? `${Math.round(item.hours_since_success)}h since last run, SLA ${item.sla_hours}h`
          : undefined
      }
    >
      fresh
    </span>
  )
}
