import { Link, createFileRoute, useNavigate } from "@tanstack/react-router"
import { useEffect, useState } from "react"
import { Cta, StatBand } from "@/components/blocks"
import { DatasetCards } from "@/components/dataset-cards"
import { dayShort, timeUtc, titleCase } from "@/lib/format"
import { fetchDatasets } from "@/lib/server"
import type { Stat } from "@/components/blocks"

/** The filters live in the URL, so a filtered view is a link you can send someone.
    Every one is optional — a bare `/datasets` is the unfiltered list, and a link
    that sets only `section` should not have to spell out three empty strings. */
export type Filters = {
  q?: string
  kind?: string
  status?: string
  section?: string
  /** "group" or "series" — which rung of the hierarchy to show. */
  level?: string
  page?: number
}

const TEXT_KEYS = ["q", "kind", "status", "section", "level"] as const

/** Drop empty values rather than writing `?q=&kind=` into the address bar. */
const clean = (search: Record<string, unknown>): Filters => {
  const out: Filters = {}
  for (const key of TEXT_KEYS) {
    const value = search[key]
    if (typeof value === "string" && value) out[key] = value
  }
  const page = Number(search.page)
  if (Number.isInteger(page) && page > 0) out.page = page // page 0 is the default
  return out
}

/** Set one filter, keeping the others. Clearing a filter removes the key rather
    than setting it to "" — otherwise the URL accumulates `?section=&status=`.

    Changing any filter resets the page: "page 40" of a result set that now has
    three pages shows an empty screen and reads as a bug. */
function withFilter(prev: Filters, key: keyof Filters, value: string): Filters {
  const next = { ...prev }
  if (value) next[key] = value as never
  else delete next[key]
  delete next.page
  return next
}

export const Route = createFileRoute("/datasets")({
  validateSearch: clean,
  loaderDeps: ({ search }) => search,
  loader: ({ deps }) => fetchDatasets({ data: deps }),
  head: () => ({ meta: [{ title: "Datasets · lake" }] }),
  component: Datasets,
})

function Datasets() {
  const { cards, matched, total, page, pages, levels, kinds, sections, stats } =
    Route.useLoaderData()
  const search = Route.useSearch()
  const navigate = useNavigate({ from: Route.fullPath })

  // The search box is typed into far faster than the server can answer, so it
  // holds its own value and pushes to the URL on a debounce. Everything else
  // navigates immediately — a dropdown change is one decision, not thirty.
  const [q, setQ] = useState(search.q ?? "")
  useEffect(() => setQ(search.q ?? ""), [search.q])
  useEffect(() => {
    if (q === (search.q ?? "")) return
    const timer = setTimeout(() => {
      // `replace`, so a search does not push thirty history entries the reader
      // then has to press Back through.
      navigate({ search: (prev) => withFilter(prev, "q", q), replace: true })
    }, 200)
    return () => clearTimeout(timer)
  }, [q, search.q, navigate])

  const setFilter = (key: keyof Filters, value: string) =>
    navigate({ search: (prev) => withFilter(prev, key, value) })

  const goToPage = (next: number) =>
    navigate({
      search: (prev) => ({ ...prev, page: next > 0 ? next : undefined }),
    })

  // These count the whole catalogue, not the page. `cards` is 60 rows; saying
  // "60 published" because that is what happens to be on screen would be a lie.
  const band: Array<Stat> = [
    {
      count: levels.dataset,
      label: levels.dataset === 1 ? "Dataset" : "Datasets",
    },
    { count: levels.group, label: "Groups" },
    { count: levels.series, label: "Series" },
    {
      text: stats.built_at ? dayShort(stats.built_at) : "—",
      label: stats.built_at
        ? `Last built ${timeUtc(stats.built_at)}`
        : "Replica not built",
    },
  ]

  return (
    <main>
      <section className="hero dotgrid">
        <div className="wrap" style={{ padding: 0 }}>
          <p className="eyebrow">Datasets</p>
          <h1 className="hero-title">Everything the lake collects.</h1>
          <p className="hero-lead">
            A source publishes a dataset, a dataset is made of groups, and a
            group is made of series — every one of them is something you can
            open, chart, and export. {levels.dataset} datasets, {levels.group}{" "}
            groups, and {levels.series.toLocaleString()} series between them.
            Everything here is read-only.
          </p>
        </div>
      </section>

      <StatBand stats={band} />

      <section className="section" id="browse">
        <div className="wrap" style={{ padding: 0 }}>
          <div className="finder" role="search">
            <div className="field finder-search">
              <label htmlFor="q">Search datasets</label>
              <input
                id="q"
                type="search"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                autoComplete="off"
                placeholder="Try “uang beredar”, “inflation”, “GDP”, or “I.1”"
              />
            </div>

            {/* Sections carry more signal than `kind`: three source types across a
                hundred-odd datasets, but nine subject areas. `kind` stays a chip. */}
            <div className="field">
              <label htmlFor="section">Subject area</label>
              <select
                id="section"
                value={search.section}
                onChange={(e) => setFilter("section", e.target.value)}
              >
                <option value="">All subjects</option>
                {sections.map((s) => (
                  <option key={s} value={s}>
                    {titleCase(s)}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label htmlFor="status">Status</label>
              <select
                id="status"
                value={search.status}
                onChange={(e) => setFilter("status", e.target.value)}
              >
                <option value="">Any status</option>
                <option value="queryable">Queryable</option>
                <option value="raw">Raw only</option>
                <option value="paused">Paused</option>
              </select>
            </div>
          </div>

          <div className="chips">
            {/* The rung of the hierarchy. Series outnumber tables 35 to 1, so
                without this the index is almost entirely single series. */}
            <Chip
              label="Everything"
              param="level"
              value=""
              current={search.level}
            />
            <Chip
              label={`Datasets (${levels.dataset})`}
              param="level"
              value="dataset"
              current={search.level}
            />
            <Chip
              label={`Groups (${levels.group})`}
              param="level"
              value="group"
              current={search.level}
            />
            <Chip
              label={`Series (${levels.series.toLocaleString()})`}
              param="level"
              value="series"
              current={search.level}
            />
            <span className="chip-sep" aria-hidden="true" />
            <Chip label="All" param="status" value="" current={search.status} />
            <Chip
              label="Queryable"
              param="status"
              value="queryable"
              current={search.status}
            />
            <Chip
              label="Raw only"
              param="status"
              value="raw"
              current={search.status}
            />
            <Chip
              label="Paused"
              param="status"
              value="paused"
              current={search.status}
            />
            <span className="chip-sep" aria-hidden="true" />
            <Chip
              label="All types"
              param="kind"
              value=""
              current={search.kind}
            />
            {kinds.map((kind) => (
              <Chip
                key={kind}
                label={kind}
                param="kind"
                value={kind}
                current={search.kind}
              />
            ))}
          </div>

          <DatasetCards
            cards={cards}
            matched={matched}
            total={total}
            filters={search}
          />

          {pages > 1 && (
            <nav className="card-pager" aria-label="Pagination">
              <button
                type="button"
                className="btn btn-outline"
                onClick={() => goToPage(page - 1)}
                disabled={page === 0}
              >
                ← Previous
              </button>
              <span className="muted" style={{ fontSize: "0.875rem" }}>
                Page {(page + 1).toLocaleString()} of {pages.toLocaleString()}
              </span>
              <button
                type="button"
                className="btn btn-outline"
                onClick={() => goToPage(page + 1)}
                disabled={page >= pages - 1}
              >
                Next →
              </button>
            </nav>
          )}
        </div>
      </section>

      <Cta
        title="Want a dataset that isn't here?"
        lead="Tell us where the data lives and how often it changes. Adding a source is a YAML edit plus a small scraper."
        action="Request a source"
      />
    </main>
  )
}

/** A chip is a real link that carries every *other* active filter, so chips
    compose with the search box and with each other — and each is a shareable URL. */
function Chip({
  label,
  param,
  value,
  current,
}: {
  label: string
  param: keyof Filters
  value: string
  current: string | undefined
}) {
  const active = (current ?? "") === value
  return (
    <Link
      to="/datasets"
      search={(prev) => withFilter(prev, param, value)}
      className={`chip ${active ? "chip-on" : ""}`}
      aria-current={active ? "true" : undefined}
    >
      {label}
    </Link>
  )
}
