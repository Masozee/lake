/**
 * A read of the API, as data — and the one place that turns it into a URL.
 *
 * The API takes no SQL. A request is a *thing* — a dataset, a statistical table inside
 * one, or a single series — addressed by the same short id its page is:
 *
 *     /api/data/i5demefo/rows?period=gte:2000&limit=100
 *
 * The id carries the keys, which is the whole point of it. The alternative is
 * `?dataset_id=seki_indicators&group_id=I.1.&series=Uang+Beredar+Luas%28M2%29`, and a
 * reader cannot hold that in their head, paste it into a paper, or read it off a slide.
 * `observations` — the raw table — is the one thing addressed by name, because it is
 * not a dataset; it is what all of them are views of.
 *
 * Filters compose on top: the id fixes the slice, a filter narrows within it.
 *
 * The server sends this shape (`Dataset.query`) rather than a formatted URL, because
 * the page needs it four ways — a link to the browser, a JSON fetch, a CSV download,
 * and four copy-paste snippets — and each wants a different encoding of it. Building
 * the string in four places is how they drift apart, so they all come through here.
 */

export type Operator =
  | "eq"
  | "ne"
  | "contains"
  | "starts"
  | "gt"
  | "lt"
  | "gte"
  | "lte"
  | "in"
  | "null"
  | "notnull"

/** What the API calls a read. Mirrors `lake.api.rows` and `catalog._query_for`. */
export type DataQuery = {
  /** The thing to read: an 8-char id, or `observations` for the raw table. */
  id: string
  /** The columns to return. Empty or absent means all of them. */
  select?: Array<string> | null
  /** Extra narrowing on top of the id. Column -> value, where the value may carry an
      operator prefix (`gte:2000`); a bare value means equality, which is what
      `?freq=annual` should obviously do. */
  filters: Record<string, string>
  sort?: string | null
  descending?: boolean
  limit?: number | null
  offset?: number | null
}

/** The raw merged table, addressed by name rather than by id — it is not a dataset,
    it is what all of them are views of. Mirrors `catalog.OBSERVATIONS`. */
export const OBSERVATIONS = "observations"

/** The query string shared by the rows, aggregate, and export endpoints. */
function params(query: DataQuery): URLSearchParams {
  const search = new URLSearchParams()

  // Filters first, so a reader eyeballing the URL sees what was asked for before
  // they see how much of it was wanted.
  for (const [column, value] of Object.entries(query.filters)) {
    search.set(column, value)
  }
  if (query.select?.length) search.set("select", query.select.join(","))
  if (query.sort) search.set("sort", query.sort)
  // `?desc` is a flag: present means descending. Sending `desc=false` would work too,
  // but a URL that says what it means is one a reader can edit by hand.
  if (query.descending) search.set("desc", "")
  if (query.limit != null) search.set("limit", String(query.limit))
  if (query.offset) search.set("offset", String(query.offset))

  return search
}

/** `?desc=` renders as `desc=`; the API reads a bare `?desc` the same way, and that is
    the one a person would type. */
function stringify(search: URLSearchParams): string {
  return search.toString().replace(/=(?=&|$)/g, "")
}

/** The rows endpoint for this read. `base` is "" in the browser (same-origin proxy)
    and the public API's URL in a snippet the reader will run somewhere else. */
export function rowsUrl(query: DataQuery, base = ""): string {
  const search = stringify(params(query))
  return `${base}/api/data/${query.id}/rows${search ? `?${search}` : ""}`
}

/**
 * The same read, as a downloadable file.
 *
 * The same URL as `rowsUrl` — the rows are one resource, and CSV is one of three ways
 * of writing them down. `?format=` rather than `Accept:` because this URL is handed to
 * an `<a href>` and pasted into `pd.read_csv`, and neither of those can set a header.
 */
export function exportUrl(
  query: DataQuery,
  format: "csv" | "xlsx",
  base = "",
  filename?: string
): string {
  const search = params(query)
  // An export is the reader's filtered view *without* the page. Carrying the limit
  // over would hand them 100 rows of a 3,000-row series and call it the data — the
  // server defaults a file to everything, so the way to ask for that is to say nothing.
  search.delete("limit")
  search.delete("offset")
  search.set("format", format)
  if (filename) search.set("filename", filename)
  return `${base}/api/data/${query.id}/rows?${stringify(search)}`
}

/** How a filter reads to a person: `period gte 2000`, `freq is annual`. */
export function describeFilter(column: string, raw: string): string {
  const [head, ...rest] = raw.split(":")
  const tail = rest.join(":")
  const words: Partial<Record<Operator, string>> = {
    eq: "is",
    ne: "is not",
    contains: "contains",
    starts: "starts with",
    gt: ">",
    lt: "<",
    gte: "≥",
    lte: "≤",
    in: "is one of",
    null: "is empty",
    notnull: "is not empty",
  }
  const word = words[head as Operator]
  if (!word || !rest.length) return `${column} is ${raw}`
  if (head === "null" || head === "notnull") return `${column} ${word}`
  return `${column} ${word} ${tail}`
}
