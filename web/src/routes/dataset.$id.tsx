import { Link, createFileRoute } from "@tanstack/react-router"
import { ChevronRight, Layers, LineChart } from "lucide-react"
import { Chart, toPath } from "@/components/chart"
import { Cta, ResultTable, StatBand } from "@/components/blocks"
import { Reveal } from "@/components/reveal"
import { num, titleCase, year } from "@/lib/format"
import { exportUrl } from "@/lib/query"
import { fetchDataset } from "@/lib/server"
import type { Stat } from "@/components/blocks"
import type { Dataset, Level } from "@/lib/types"

/** What to call a rung, to a reader. An id says nothing; "group" says a little. */
const NOUN: Partial<Record<Level, string>> = {
  dataset: "dataset",
  group: "table",
  series: "series",
}

/**
 * The browse page's URL for this thing, as search params.
 *
 * Just the id. It already says which rows — the whole point of having one — so the
 * browser opens on this thing and the reader filters within it.
 */
function browse(dataset: Dataset) {
  return {
    id: dataset.query.id,
    sort: dataset.query.sort ?? undefined,
    desc: dataset.query.descending || undefined,
  } as never
}

export const Route = createFileRoute("/dataset/$id")({
  loader: ({ params }) => fetchDataset({ data: params.id }),
  head: ({ loaderData }) => ({
    meta: [{ title: `${loaderData?.dataset.title ?? "Dataset"} · lake` }],
  }),
  component: DatasetDetail,
})

function DatasetDetail() {
  const { dataset, sample, series, children, source } = Route.useLoaderData()
  const path = toPath(series)

  const from = year(dataset.first_period)
  const to = year(dataset.last_period)
  const seriesFrom = year(series[0]?.period)
  const seriesTo = year(series[series.length - 1]?.period)
  // The last point that HAS a value. The publisher can report a period with no
  // number in it, and "latest ∅" is not a caption.
  const last = series.filter((p) => p.value !== null).at(-1)?.value ?? undefined

  const isSeries = dataset.level === "series"

  const band: Array<Stat> = [
    // For a series every row IS an observation, so "rows" and "observations" are
    // the same number — say the one that means something.
    { count: dataset.row_count, label: isSeries ? "Observations" : "Rows" },
    isSeries
      ? { count: series.length, label: "Points charted" }
      : {
          count:
            dataset.group_count ??
            dataset.series_count ??
            dataset.columns.length,
          label: dataset.group_count
            ? "Groups"
            : dataset.series_count
              ? "Series"
              : "Columns",
        },
    {
      text: from && to ? `${from}–${to}` : "—",
      label: dataset.freq ? titleCase(dataset.freq) : "Coverage",
    },
    // A rung spanning several units cannot claim one of them, so it says how many.
    {
      text:
        dataset.unit ??
        (dataset.unit_count > 1 ? `${dataset.unit_count} units` : "—"),
      label: "Unit",
    },
  ]

  const noun = children.level === "group" ? "groups" : "series"
  const ChildIcon = children.level === "group" ? Layers : LineChart

  return (
    <main>
      <section className="hero dotgrid" style={{ paddingBottom: "2.5rem" }}>
        <div className="wrap" style={{ padding: 0 }}>
          {/* The trail back up. The URL is an id — `wm72qlsa` — so it says nothing
              about where the reader is; these crumbs are the only thing that does.
              The last one is this page, so it is dropped rather than linked to
              itself. */}
          <p className="eyebrow">
            <Link to="/datasets" search={{}} className="reset">
              ← all datasets
            </Link>
            {/* The section is a filter, not a rung — so it goes before the trail,
                and the trail runs unbroken from the source down to this page. */}
            {dataset.section && (
              <>
                <span className="dot"> · </span>
                <Link to="/datasets" search={{ section: dataset.section }}>
                  {titleCase(dataset.section)}
                </Link>
              </>
            )}
            {dataset.crumbs.slice(0, -1).map((crumb) => (
              <span key={crumb.id}>
                <span className="dot"> · </span>
                <Link to="/dataset/$id" params={{ id: crumb.id }}>
                  {crumb.title}
                </Link>
              </span>
            ))}
          </p>

          <div className="hero-grid">
            <div>
              {/* The publisher's own key for this group — `I.1.` is what Bank
                  Indonesia prints beside the table in SEKI itself, and quoting it
                  back at them names the table exactly. */}
              {dataset.group_id && !dataset.series && (
                <p className="detail-number mono">{dataset.group_id}</p>
              )}
              <h1 className="detail-title">{dataset.title}</h1>
              <p className="hero-lead">
                {source &&
                  `Collected from ${source.display_name} on a ${source.schedule} schedule. `}
                Read-only — this page cannot change the data.
              </p>
              <div className="hero-actions">
                <Link
                  to="/query"
                  search={browse(dataset)}
                  className="btn btn-primary"
                  style={{ minHeight: "48px" }}
                >
                  Browse this {NOUN[dataset.level] ?? "dataset"}
                </Link>
                {/* Every rung can be downloaded, not just a whole table: the id says
                    which rows, so a series gives you a series rather than the million
                    it sits in — and the server names the file after it. */}
                <a
                  href={exportUrl(dataset.query, "csv")}
                  className="btn btn-ghost"
                  style={{ minHeight: "48px" }}
                >
                  Download CSV
                </a>
              </div>
            </div>

            {/* The first indicator Bank Indonesia lists is the one the table is
                named for, so it is the honest thing to plot. */}
            {path && seriesFrom && seriesTo && last !== undefined && (
              <Chart
                path={path}
                height="11rem"
                label={`${dataset.title}, ${seriesFrom} to ${seriesTo}`}
                caption={
                  // The chart shows the tail of the series, not all of it. Say so,
                  // rather than implying the data begins where the line does.
                  `${from && seriesFrom > from ? "Last " : ""}${seriesFrom}–${seriesTo} · latest ${num(
                    Math.round(last)
                  )}${dataset.unit ? ` ${dataset.unit}` : ""}`
                }
              />
            )}
          </div>
        </div>
      </section>

      <StatBand stats={band} />

      {/* What is inside it. Without this the page is a dead end: a reader can see
          that SEKI publishes 108 tables and has no way to open one of them. */}
      {children.items.length > 0 && (
        <section className="section">
          <div className="wrap" style={{ padding: 0 }}>
            <Reveal>
              <h2 className="section-head">Inside this dataset</h2>
            </Reveal>
            <p className="section-lead">
              {num(children.total)} {noun}
              {children.items.length < children.total &&
                `, showing the first ${num(children.items.length)}`}
              . Each opens on its own page, with its own chart and its own
              query.
            </p>

            <ul className="drill-list">
              {children.items.map((child) => (
                <li key={child.id}>
                  <Link
                    to="/dataset/$id"
                    params={{ id: child.id }}
                    className="drill-row reset"
                  >
                    <ChildIcon
                      size={15}
                      className="drill-icon"
                      aria-hidden="true"
                    />
                    <span className="drill-main">
                      <span>{child.title}</span>
                      <span className="drill-sub">
                        {year(child.first_period) &&
                          year(child.last_period) && (
                            <>
                              {year(child.first_period)}–
                              {year(child.last_period)}
                            </>
                          )}
                        {child.unit && ` · ${child.unit}`}
                      </span>
                    </span>
                    <span className="drill-rows data-mono">
                      {num(child.row_count)}
                    </span>
                    <ChevronRight
                      size={15}
                      className="drill-caret"
                      aria-hidden="true"
                    />
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        </section>
      )}

      <section className="section">
        <div className="wrap" style={{ padding: 0 }}>
          <Reveal>
            <h2 className="section-head">Latest observations</h2>
          </Reveal>
          <p className="section-lead">
            The most recent rows, newest first. Use{" "}
            <Link to="/query" search={browse(dataset)}>
              the browser
            </Link>{" "}
            for the rest, or <Link to="/ask">ask the AI</Link> about them.
            {dataset.missing_count > 0 && (
              <>
                {" "}
                {num(dataset.missing_count)} observations here carry a period
                but no value — the publisher reports the gap, and a gap is not a
                zero.
              </>
            )}
          </p>

          <Reveal>
            <ResultTable result={sample} />
          </Reveal>

          <h3 className="section-title">Columns</h3>
          <Reveal className="schema-list">
            {dataset.columns.map((column) => (
              <div className="schema-col" key={column.name}>
                <span className="mono schema-name">{column.name}</span>
                <span className="schema-type">{column.type.toLowerCase()}</span>
              </div>
            ))}
          </Reveal>

          {/* The keys behind the id. The URL is opaque on purpose; the query is not,
              and someone writing their own needs the real column values. */}
          {dataset.level !== "dataset" && (
            <p className="muted mt-3" style={{ fontSize: "0.875rem" }}>
              This dataset is one of many in the{" "}
              <code className="mono">{dataset.table}</code> table, selected with{" "}
              <code className="mono">
                dataset_id = &apos;{dataset.dataset_id}&apos;
                {dataset.group_id && (
                  <> AND group_id = &apos;{dataset.group_id}&apos;</>
                )}
                {dataset.series && (
                  <> AND series = &apos;{dataset.series}&apos;</>
                )}
              </code>
              .
            </p>
          )}
        </div>
      </section>

      <Cta
        title="Something look wrong?"
        lead="If a number here disagrees with the publisher, we want to know. Send the query you ran — it is the fastest possible bug report."
        action="Report it"
      />
    </main>
  )
}
