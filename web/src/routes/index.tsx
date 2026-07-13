import { Link, createFileRoute } from "@tanstack/react-router"
import { Chart } from "@/components/chart"
import { Cta, Pipeline, StatBand } from "@/components/blocks"
import { Reveal } from "@/components/reveal"
import { dayShort, num, timeUtc } from "@/lib/format"
import { fetchOverview } from "@/lib/server"
import type { Stat } from "@/components/blocks"

export const Route = createFileRoute("/")({
  loader: () => fetchOverview(),
  head: () => ({ meta: [{ title: "Overview · lake" }] }),
  component: Overview,
})

function Overview() {
  const { stats, sources, series } = Route.useLoaderData()
  const active = sources.filter((s) => s.enabled).length

  const band: Array<Stat> = [
    { count: stats.total_rows, label: "Rows served" },
    {
      count: stats.table_count,
      label: stats.table_count === 1 ? "Dataset" : "Datasets",
    },
    { count: active, label: "Active sources" },
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
          <div className="hero-grid">
            <div>
              <p className="eyebrow">Open data lake</p>
              <h1 className="hero-title">
                Public data, collected on a schedule and queryable in the
                browser.
              </h1>
              <p className="hero-lead">
                Scrapers land immutable raw bytes on a NAS, every run is
                recorded in a Postgres catalog, and typed Parquet is rebuilt
                from it. What you query here is a read-only replica of that
                Parquet — it cannot be changed from this page.
              </p>
              <div className="hero-actions">
                <Link
                  to="/datasets"
                  search={{}}
                  className="btn btn-primary"
                  style={{ minHeight: "48px" }}
                >
                  Browse datasets
                </Link>
                <Link
                  to="/query"
                  search={{}}
                  className="btn btn-ghost"
                  style={{ minHeight: "48px" }}
                >
                  Query the data
                </Link>
              </div>
            </div>

            {/* Not decoration: the real gdp_annual series for country_iso3='WLD'. */}
            {series && (
              <Chart
                path={series}
                label={`World GDP, ${series.first_year} to ${series.last_year}`}
                caption={`World GDP · ${series.first_year}–${series.last_year} · $${(
                  series.last_value / 1e12
                ).toFixed(1)}T in ${series.last_year}`}
              />
            )}
          </div>
        </div>
      </section>

      <StatBand stats={band} />

      <section className="section" id="how">
        <div className="wrap" style={{ padding: 0 }}>
          <Reveal>
            <h2 className="section-head">How it works</h2>
          </Reveal>
          <p className="section-lead">
            Four stages, each one idempotent. A run that fails halfway leaves
            nothing half-written: raw bytes are committed atomically, and
            Parquet is rebuilt rather than appended to.
          </p>

          <Pipeline />

          <p className="muted mt-3" style={{ fontSize: "0.875rem" }}>
            <Link to="/about">Read more about the design →</Link>
          </p>
        </div>
      </section>

      <section className="section section-alt" id="data">
        <div className="wrap" style={{ padding: 0 }}>
          <Reveal>
            <h2 className="section-head">Latest data</h2>
          </Reveal>
          <p className="section-lead">
            {stats.built_at
              ? `The replica currently being served was published ${new Date(
                  stats.built_at
                ).toLocaleDateString("en-GB", {
                  day: "numeric",
                  month: "long",
                  year: "numeric",
                })} at ${timeUtc(stats.built_at)}. Pick a dataset to browse its columns, profile its values, or export it.`
              : "No serving replica has been built yet. Once it is, published datasets appear here."}
          </p>

          {stats.tables.length ? (
            <Reveal className="grid-cards mb-4">
              {stats.tables.map((table) => (
                <Link
                  key={table.name}
                  to="/table/$name"
                  params={{ name: table.name }}
                  className="tile"
                >
                  <h3 className="tile-title mono">{table.name}</h3>
                  <p className="tile-meta">
                    {num(table.row_count)} rows · {table.columns} columns
                  </p>
                </Link>
              ))}
            </Reveal>
          ) : (
            <div className="tile" style={{ border: "1px solid var(--border)" }}>
              <h3 className="tile-title">No datasets yet</h3>
              <p className="tile-meta mb-4">
                Build the serving replica, then refresh this page.
              </p>
              <pre className="code">
                <code>uv run lake serve build</code>
              </pre>
            </div>
          )}

          {sources.length > 0 && (
            <>
              <h3 className="section-title">Where it comes from</h3>
              <Reveal className="grid-cards">
                {sources.map((source) => (
                  <div className="tile" key={source.source_id}>
                    <h4 className="tile-title" style={{ fontSize: "1rem" }}>
                      {source.display_name}
                    </h4>
                    <p
                      className="tile-meta mono"
                      style={{ fontSize: "0.75rem" }}
                    >
                      {source.source_id}
                    </p>
                    <p className="tile-meta mt-3">
                      <span
                        className={`pill ${source.enabled ? "pill-live" : "pill-off"}`}
                      >
                        {source.enabled
                          ? `Collecting ${source.schedule}`
                          : "Paused"}
                      </span>
                    </p>
                  </div>
                ))}
              </Reveal>
            </>
          )}

          <p className="muted mt-3" style={{ fontSize: "0.875rem" }}>
            <Link to="/datasets" search={{}}>
              See every dataset and its schema →
            </Link>
          </p>
        </div>
      </section>

      <Cta
        title="Need a dataset, or found something wrong?"
        lead="Requests for new sources, corrections to existing data, and questions about how a number was derived are all welcome."
        action="Get in touch"
      />
    </main>
  )
}
