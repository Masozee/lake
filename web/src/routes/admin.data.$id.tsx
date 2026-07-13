/**
 * One thing, on its own page: a dataset, a group inside it, or a single series.
 *
 * Every rung gets this — a series is not a lesser thing than the group it sits in, it
 * is what a reader actually came for. So it has a URL, a chart, the query behind it,
 * the code to fetch it in four languages, its rows, and a sidebar of everything true
 * about it.
 *
 * ## Why the layout is two columns
 *
 * The facts (unit, period, frequency, how much is missing) are what a reader checks
 * *while* they are reading the numbers, not before. Stacked above the grid they scroll
 * away exactly when they are needed — "is this in billions or millions?" is a question
 * you ask on row 300. So they sit in a sticky aside, beside the content, where they
 * stay.
 *
 * The main column is the work: what the series looks like, how to get it, and what is
 * in it.
 *
 * The page is one request. Split across five it would render in pieces, and a
 * half-drawn page is one a reader will believe.
 */

import { Link, createFileRoute } from "@tanstack/react-router"
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Layers,
  LineChart,
} from "lucide-react"
import { useEffect, useState } from "react"
import { Chart, toPath } from "@/components/chart"
import { DataTable } from "@/components/data-table"
import { admin, errorList } from "@/lib/admin"
import { day, num, titleCase, year } from "@/lib/format"
import { rowsUrl } from "@/lib/query"
import { LANGUAGES, snippet } from "@/lib/snippets"
import type { DataQuery } from "@/lib/query"
import type { Language } from "@/lib/snippets"
import type { AdminDetail, Children, Level } from "@/lib/types"

export const Route = createFileRoute("/admin/data/$id")({
  component: AdminDataDetail,
})

/** The raw DuckDB table. Not a dataset — the table every dataset is a view of — so it
    has no id, no period, and no unit. Browsing it IS its detail. */
const RAW = "observations"

const NOUN: Record<Level, string> = {
  raw: "the whole table",
  // A source that has published nothing has no id, so it has no page and never reaches
  // here. The map is total anyway: a partial one is a `undefined` waiting to render.
  source: "source",
  dataset: "dataset",
  group: "group",
  series: "series",
}

function AdminDataDetail() {
  const { id } = Route.useParams()

  if (id === RAW) return <RawTable />
  return <Thing id={id} />
}

function RawTable() {
  return (
    <>
      <Back to="" title="All data" />
      <h1 className="page-title mono">{RAW}</h1>
      <p className="section-lead" style={{ marginBottom: "1.5rem" }}>
        Every source lands here. A dataset is this table with a{" "}
        <code className="mono">dataset_id</code> filter on it, so there is
        nothing above this to describe — only the rows.
      </p>
      <DataTable datasetId={RAW} rowCount={0} />
    </>
  )
}

function Thing({ id }: { id: string }) {
  const [meta, setMeta] = useState<AdminDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let stale = false
    setMeta(null)
    setError(null)

    admin
      .get<AdminDetail>(`/data/${encodeURIComponent(id)}/detail`)
      .then((body) => !stale && setMeta(body))
      .catch((err) => !stale && setError(errorList(err).join(" ")))

    return () => {
      stale = true
    }
  }, [id])

  if (error) {
    return (
      <>
        <Back to="" title="All data" />
        <p className="notice notice-error">{error}</p>
      </>
    )
  }
  if (!meta) return <p className="muted">Loading…</p>

  const path = toPath(meta.points)
  const from = year(meta.first_period)
  const chartFrom = year(meta.points[0]?.period)
  const chartTo = year(meta.points[meta.points.length - 1]?.period)
  const parent = meta.crumbs[meta.crumbs.length - 2]

  return (
    <>
      {/* Back to the level this thing lives on, not to the root: a reader who opened
          the fourteenth series of a table wants the other fifty-eight, not the two
          datasets at the top. */}
      <Back to={parent?.id ?? ""} title={parent?.title ?? "All data"} />

      <p className="detail-eyebrow">
        {/* The publisher's own key: `I.1.` is what Bank Indonesia prints beside this
            table, `NY.GDP.MKTP.CD` is what the World Bank calls its indicator. */}
        {meta.group_id && !meta.series && (
          <span className="detail-number mono">{meta.group_id}</span>
        )}
        {NOUN[meta.level]}
        {meta.section && ` · ${titleCase(meta.section)}`}
        <span className="dot"> · </span>
        <span className="mono">{meta.id}</span>
      </p>
      <h1 className="page-title">{meta.title}</h1>

      {/* Its own name does not identify it: twenty-three SEKI series are called
          "Lainnya", and the group they came from is the only thing telling them
          apart. */}
      {meta.parent_id && meta.parent_title && (
        <p className="muted" style={{ fontSize: "0.875rem" }}>
          in{" "}
          <Link to="/admin/data/$id" params={{ id: meta.parent_id }}>
            {meta.parent_title}
          </Link>
        </p>
      )}

      <div className="detail-layout">
        <div className="detail-main">
          {/* The line itself. An admin scanning for a break in the data sees it here
              long before they would find it in the grid. */}
          {path && chartFrom && chartTo && (
            <Chart
              path={path}
              height="10rem"
              label={`${meta.title}, ${chartFrom} to ${chartTo}`}
              caption={
                // The chart shows the tail of the series, not all of it. Say so, rather
                // than implying the data begins where the line does.
                `${from && chartFrom > from ? "Last " : ""}${chartFrom}–${chartTo}${
                  meta.unit ? ` · ${meta.unit}` : ""
                }${meta.level === "series" ? "" : " · the first series the publisher lists"}`
              }
            />
          )}

          <Fetch query={meta.query} api={meta.api_url} />

          {/* What is inside it. Without this the page is a dead end: a reader can see
              that a group holds 59 series and has no way to open one. */}
          {meta.children.items.length > 0 && (
            <section>
              <h2 className="section-title">Inside this {NOUN[meta.level]}</h2>
              <p className="muted mb-4" style={{ fontSize: "0.8125rem" }}>
                {num(meta.children.total)}{" "}
                {meta.children.level === "group" ? "groups" : "series"}
                {meta.children.items.length < meta.children.total &&
                  `, showing the first ${num(meta.children.items.length)}`}
                . Each one has a page of its own.
              </p>
              <ThingList items={meta.children.items} />
            </section>
          )}

          <section>
            <h2 className="section-title">Rows</h2>
            <DataTable datasetId={meta.id} rowCount={meta.row_count} />
          </section>
        </div>

        <Metadata meta={meta} />
      </div>
    </>
  )
}

/**
 * How to get this data into whatever you were going to do with it.
 *
 * The URL alone is half an answer — the reader still has to work out how to unpack
 * what comes back. These snippets end where their own work begins: a DataFrame, an
 * array, a tibble.
 */
function Fetch({ query, api }: { query: DataQuery; api: string }) {
  const [language, setLanguage] = useState<Language>("curl")
  const code = snippet(language, query, api)

  return (
    <section>
      <h2 className="section-title">Get this data</h2>
      <p className="muted mb-4" style={{ fontSize: "0.8125rem" }}>
        The API is read-only and needs no key. There is no SQL to send — a
        request is a URL, and this one is below.
      </p>

      <div className="tabs" role="tablist" aria-label="Language">
        {LANGUAGES.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="tab"
            aria-selected={entry.id === language}
            className={`tab ${entry.id === language ? "tab-on" : ""}`}
            onClick={() => setLanguage(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <CodeBlock code={code} label={`${language} snippet`} />

      <h3 className="section-title" style={{ marginTop: "1.5rem" }}>
        The request behind it
      </h3>
      <p className="muted mb-4" style={{ fontSize: "0.8125rem" }}>
        The page's URL is an opaque id on purpose; the request is not. These are
        the real column values — edit a filter, drop one to widen it, raise the
        limit.
      </p>
      <CodeBlock code={rowsUrl(query, api)} label="request URL" />
    </section>
  )
}

/** Code, with the one button anyone actually wants next to it. */
function CodeBlock({ code, label }: { code: string; label: string }) {
  const [copied, setCopied] = useState(false)

  const copy = () => {
    void navigator.clipboard.writeText(code).then(() => {
      setCopied(true)
      // Long enough to read, short enough that the button is ready again before the
      // reader has finished pasting.
      setTimeout(() => setCopied(false), 1600)
    })
  }

  return (
    <div className="code-wrap">
      <button
        type="button"
        className="code-copy"
        onClick={copy}
        aria-label={copied ? `${label} copied` : `Copy ${label}`}
      >
        {copied ? <Check size={13} /> : <Copy size={13} />}
        {copied ? "Copied" : "Copy"}
      </button>
      <pre className="code-block mono">{code}</pre>
    </div>
  )
}

/**
 * Everything true about this thing, beside it rather than above it.
 *
 * Sticky, because "is this in billions or millions?" is a question you ask on row 300,
 * not on row 1 — and a fact that has scrolled off the screen by the time it is needed
 * may as well not be on the page.
 */
function Metadata({ meta }: { meta: AdminDetail }) {
  // A rung spanning several units cannot claim one of them, so it says how many.
  const unit =
    meta.unit ?? (meta.unit_count > 1 ? `${meta.unit_count} units` : "—")

  const facts: Array<[string, string]> = [
    // For a series every row IS an observation, so "rows" and "observations" are the
    // same number — say the one that means something.
    [meta.level === "series" ? "Observations" : "Rows", num(meta.row_count)],
    ...(meta.group_count
      ? [["Groups", num(meta.group_count)] as [string, string]]
      : []),
    ...(meta.series_count
      ? [["Series", num(meta.series_count)] as [string, string]]
      : []),
    [
      "Period",
      meta.first_period && meta.last_period
        ? `${day(meta.first_period)} – ${day(meta.last_period)}`
        : "—",
    ],
    ["Unit", unit],
    ["Frequency", meta.freq ? titleCase(meta.freq) : "—"],
    ["Source", meta.source_id ?? "—"],
    // A missing observation is not a zero, and 2,681 of the World Bank's are missing.
    // A page that does not say so is one a reader will trust too far.
    ["Missing values", meta.missing_count ? num(meta.missing_count) : "none"],
  ]

  return (
    <aside className="detail-aside">
      <div className="detail-sticky">
        <section className="meta-card">
          <h2 className="meta-head">Metadata</h2>
          <dl className="meta-list">
            {facts.map(([label, value]) => (
              <div className="meta-row" key={label}>
                <dt>{label}</dt>
                <dd className="data-mono">{value}</dd>
              </div>
            ))}
          </dl>
        </section>

        {/* The keys behind the id. An id is opaque by design; this is where a reader
            who wants to write their own query finds what to write. */}
        <section className="meta-card">
          <h2 className="meta-head">Keys</h2>
          <dl className="meta-list">
            <div className="meta-row">
              <dt>dataset_id</dt>
              <dd className="mono">{meta.dataset_id}</dd>
            </div>
            {meta.group_id && (
              <div className="meta-row">
                <dt>group_id</dt>
                <dd className="mono">{meta.group_id}</dd>
              </div>
            )}
            {meta.series && (
              <div className="meta-row">
                <dt>series</dt>
                <dd className="mono">{meta.series}</dd>
              </div>
            )}
            {meta.series_code && (
              <div className="meta-row">
                <dt>series_code</dt>
                <dd className="mono">{meta.series_code}</dd>
              </div>
            )}
          </dl>
        </section>

        <Siblings siblings={meta.siblings} current={meta.id} />
      </div>
    </aside>
  )
}

/**
 * The other series in this group, so the next one is one click away.
 *
 * Without this a series page is a cul-de-sac: a reader comparing M2 against the four
 * lines under it has to navigate back to a list, find their place, and come back in —
 * fifty-eight times.
 */
function Siblings({
  siblings,
  current,
}: {
  siblings: Children
  current: string
}) {
  if (siblings.items.length < 2) return null // itself is not a list

  const noun = siblings.level === "group" ? "groups" : "series"

  return (
    <section className="meta-card">
      <h2 className="meta-head">
        Beside it
        <span className="meta-count">{num(siblings.total)}</span>
      </h2>
      <p
        className="muted"
        style={{ fontSize: "0.6875rem", marginBottom: "0.5rem" }}
      >
        The other {noun} in this group
        {siblings.items.length < siblings.total &&
          `, showing ${num(siblings.items.length)}`}
        .
      </p>
      <ul className="sibling-list">
        {siblings.items.map((item) => (
          <li key={item.id}>
            <Link
              to="/admin/data/$id"
              params={{ id: item.id }}
              className={`sibling ${item.id === current ? "sibling-on" : "reset"}`}
              // The one you are on is a place, not a link back to itself.
              aria-current={item.id === current ? "page" : undefined}
            >
              {item.title}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** A list of things to open. Shared by "inside this" and nothing else — yet. */
function ThingList({ items }: { items: Children["items"] }) {
  return (
    <ul className="drill-list">
      {items.map((child) => {
        const Icon = child.level === "group" ? Layers : LineChart
        return (
          <li key={child.id}>
            <Link
              to="/admin/data/$id"
              params={{ id: child.id }}
              className="drill-row reset"
            >
              <Icon size={15} className="drill-icon" aria-hidden="true" />
              <span className="drill-main">
                <span>{child.title}</span>
                <span className="drill-sub">
                  {year(child.first_period) && year(child.last_period) && (
                    <>
                      {year(child.first_period)}–{year(child.last_period)}
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
        )
      })}
    </ul>
  )
}

/** Back to the level this thing lives on.

    A link, not `history.back()`. The label names a place — "Back to Uang Beredar…" —
    and history goes wherever the reader came from, which is often somewhere else
    entirely. A link that says where it goes has to go there. */
function Back({ to, title }: { to: string; title: string }) {
  return (
    <Link
      to="/admin/data"
      search={to ? { parent: to } : {}}
      className="crumb-back reset"
    >
      <ChevronLeft size={14} aria-hidden="true" />
      Back to {title}
    </Link>
  )
}
