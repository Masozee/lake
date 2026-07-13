/**
 * The card grid.
 *
 * One card per dataset, not per source: SEKI is one source publishing 108
 * statistical tables, and "Uang Beredar dan Faktor-Faktor yang Mempengaruhinya"
 * is what a reader looks for — listing the source alone would hide all 108 behind
 * an id nobody searches for.
 */

import { Link } from "@tanstack/react-router"
import { day, num, titleCase, year } from "@/lib/format"
import type { DatasetCard } from "@/lib/types"

type Filters = {
  q?: string
  kind?: string
  status?: string
  section?: string
  level?: string
}

export function DatasetCards({
  cards,
  matched,
  total,
  filters,
}: {
  /** One page of them. */
  cards: Array<DatasetCard>
  /** How many match the filters across every page — not how many are on screen. */
  matched: number
  /** How many exist unfiltered. */
  total: number
  filters: Filters
}) {
  const word = matched === 1 ? "dataset" : "datasets"

  return (
    <div>
      <p className="result-count muted" aria-live="polite">
        {/* The count that matters is how many MATCH, not how many fit on a page.
            "60 datasets" when 3,918 matched would be a lie the pager then contradicts. */}
        {matched === total
          ? `${total.toLocaleString()} ${word}`
          : `${matched.toLocaleString()} of ${total.toLocaleString()} ${word}`}
      </p>

      {cards.length ? (
        <div className="card-grid">
          {cards.map((card) => (
            <Card card={card} key={card.id ?? card.source_id ?? card.title} />
          ))}
        </div>
      ) : (
        <Empty filters={filters} />
      )}
    </div>
  )
}

function Card({ card }: { card: DatasetCard }) {
  const from = year(card.first_period)
  const to = year(card.last_period)

  return (
    <article className="dcard">
      <header>
        {/* The whole card is a target, but only the title is the link — a
            card-wide anchor would swallow the View button inside it. */}
        <h3 className={`dcard-title${card.section ? "" : "mono"}`}>
          {card.id ? (
            <Link to="/dataset/$id" params={{ id: card.id }}>
              {card.title}
            </Link>
          ) : (
            card.title
          )}
        </h3>
        <p className="dcard-source">
          {/* The publisher's own key: `I.1.` is what Bank Indonesia prints beside
              this table in SEKI itself. */}
          {card.group_id && !card.series && (
            <span className="dcard-number">{card.group_id}</span>
          )}
          {card.source_name ?? "Source not in registry"}
        </p>
      </header>

      {/* A series shows the table it came from, because its own name does not
          identify it: twenty-three series are called "Lainnya", and the parent is
          the only thing telling them apart. */}
      {card.series && card.parent_title && card.parent_id && (
        <p className="dcard-parent">
          in{" "}
          <Link to="/dataset/$id" params={{ id: card.parent_id }}>
            {card.parent_title}
          </Link>
        </p>
      )}

      <p className="dcard-desc">
        {card.section ? (
          titleCase(card.section)
        ) : card.description ? (
          card.description
        ) : (
          <span className="muted">No description yet.</span>
        )}
      </p>

      <div className="labels">
        {card.labels.map((label) => (
          <span
            className={`label label-${label.replace(/ /g, "-")}`}
            key={label}
          >
            {label}
          </span>
        ))}
      </div>

      <footer className="dcard-foot">
        <div className="dcard-meta">
          {from && to ? (
            <>
              <time dateTime={card.first_period!}>{from}</time>
              <span className="dot">–</span>
              <time dateTime={card.last_period!}>{to}</time>
            </>
          ) : card.last_collected ? (
            <>
              <span className="muted">Updated</span>
              <time dateTime={card.last_collected}>
                {day(card.last_collected)}
              </time>
            </>
          ) : (
            <span className="muted">Never collected</span>
          )}
          {card.row_count !== null && (
            <>
              <span className="dot">·</span>
              <span>{num(card.row_count)} rows</span>
            </>
          )}
          {card.indicators ? (
            <>
              <span className="dot">·</span>
              <span>{card.indicators} series</span>
            </>
          ) : null}
        </div>

        {card.id ? (
          <Link
            to="/dataset/$id"
            params={{ id: card.id }}
            className="btn btn-ghost"
            style={{ minHeight: "36px" }}
          >
            View →
          </Link>
        ) : (
          <span className="muted dcard-note">Collected, not yet queryable</span>
        )}
      </footer>
    </article>
  )
}

function Empty({ filters }: { filters: Filters }) {
  const said = [
    filters.q && `“${filters.q}”`,
    filters.level === "group" && "among the groups",
    filters.level === "series" && "among the series",
    filters.section && `in ${titleCase(filters.section)}`,
    filters.kind && `in ${filters.kind} sources`,
    filters.status && `with status ${filters.status}`,
  ].filter(Boolean)

  return (
    <div className="tile" style={{ border: "1px solid var(--border)" }}>
      <h3 className="tile-title">No datasets match</h3>
      <p className="tile-meta">
        Nothing here matches {said.join(" ")}. Try a broader search, or{" "}
        <Link to="/datasets" search={{}}>
          clear the filters
        </Link>
        .
      </p>
    </div>
  )
}
