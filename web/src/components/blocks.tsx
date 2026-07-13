/** The repeating bands: stats, pipeline, CTA, and a result table. */

import { Link } from "@tanstack/react-router"
import type { ReactNode } from "react"
import { CountUp } from "@/components/count-up"
import { Reveal } from "@/components/reveal"
import { cell } from "@/lib/format"
import type { QueryResult } from "@/lib/types"

/** One cell of the stat band. `count` animates; `text` is shown as written. */
export type Stat =
  { count: number; label: string } | { text: string; label: string }

export function StatBand({ stats }: { stats: Array<Stat> }) {
  return (
    <section className="stat-band">
      {stats.map((stat) => (
        <div className="stat" key={stat.label}>
          {"count" in stat ? (
            <CountUp value={stat.count} className="stat-value" />
          ) : (
            <div className="stat-value">{stat.text}</div>
          )}
          <div className="stat-label">{stat.label}</div>
        </div>
      ))}
    </section>
  )
}

/** The four ingest stages. One copy, shared by the landing page and /about, so
    the story cannot drift between the two. */
const STAGES = [
  {
    num: "01",
    title: "Collect",
    body: "A scraper fetches bytes to local staging on its own schedule — daily, weekly, monthly, or yearly. Anything that fails a structural check is quarantined, never promoted.",
  },
  {
    num: "02",
    title: "Land",
    body: "Bytes are checksummed and moved into the raw archive with a single atomic rename, read-only. Raw data is immutable: it is never edited, only superseded by a later run.",
  },
  {
    num: "03",
    title: "Catalog",
    body: "Every run, file, and checksum is recorded in Postgres. That catalog is what answers whether a source went quiet or a scraper broke — and it drives the freshness alerts.",
  },
  {
    num: "04",
    title: "Serve",
    body: "DuckDB validates and rebuilds each partition into typed Parquet, then publishes a read-only replica. That replica is what this site queries.",
  },
]

export function Pipeline() {
  return (
    <Reveal className="pipeline">
      {STAGES.map((stage) => (
        <div className="stage" key={stage.num}>
          <div className="stage-num">{stage.num}</div>
          <h3 className="stage-title">{stage.title}</h3>
          <p className="stage-body">{stage.body}</p>
        </div>
      ))}
    </Reveal>
  )
}

export function Cta({
  title,
  lead,
  action,
  to = "/contact",
}: {
  title: string
  lead: ReactNode
  action: string
  to?: string
}) {
  return (
    <section className="cta">
      <div className="cta-inner">
        <div>
          <h2 className="cta-title">{title}</h2>
          <p className="cta-lead">{lead}</p>
        </div>
        <Link to={to} className="btn-on-blue">
          {action}
        </Link>
      </div>
    </section>
  )
}

/** Columns, rows, and a stat line. Shared by the table sample and the query page.
    Numbers right-align in monospace so digits line up down the column. */
export function ResultTable({ result }: { result: QueryResult }) {
  if (!result.rows.length) return <p className="muted">No rows.</p>

  return (
    <>
      <div
        className="table-wrap"
        style={{ maxHeight: "65vh", overflow: "auto" }}
      >
        <table>
          <thead>
            <tr>
              {result.columns.map((column) => (
                <th className="mono" key={column}>
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, i) => (
              <tr key={i}>
                {row.map((value, j) => {
                  // The column decides whether a number is a quantity or a label:
                  // `year` holds 2001, and `2,001` is a count of something.
                  const { text, numeric, empty } = cell(
                    value,
                    result.columns[j]
                  )
                  return (
                    <td
                      key={j}
                      className={
                        empty ? "cell-null" : numeric ? "data-mono" : "mono"
                      }
                      style={numeric ? { textAlign: "right" } : undefined}
                    >
                      {text}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {result.elapsed_ms !== undefined && (
        <p className="muted mt-3" style={{ fontSize: "0.85rem" }}>
          {result.row_count.toLocaleString()} rows in {result.elapsed_ms}ms
          {result.truncated &&
            ` · truncated to the first ${result.row_count.toLocaleString()}`}
        </p>
      )}
    </>
  )
}
