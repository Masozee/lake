/** The shapes `/api/ui/*` returns. Mirrors lake/api/routes/ui_json.py. */

import type { DataQuery } from "./query"

/**
 * Anything a DuckDB cell can be, once it has crossed JSON.
 *
 * Not `unknown`: a server function's return value has to be provably
 * serializable, and `unknown` could be a Date or a function. The API already
 * ran every value through `jsonable()`, so this union is what actually arrives —
 * stating it is honest, not a workaround.
 */
export type Cell = string | number | boolean | null

export type Source = {
  source_id: string
  display_name: string
  description: string | null
  kind: string
  schedule: string
  enabled: boolean
}

export type TableSummary = {
  name: string
  row_count: number
  columns: number
}

export type LakeStats = {
  table_count: number
  total_rows: number
  total_columns: number
  /** ISO 8601, or null when no serving replica has been built. */
  built_at: string | null
  tables: Array<TableSummary>
}

/** A ready-to-draw sparkline: the API sends the path, the page just renders it. */
export type HeadlineSeries = {
  line: string
  area: string
  width: number
  height: number
  first_year: number
  last_year: number
  last_value: number
  points: number
}

/** One dataset a reader can open. Not one source — SEKI alone publishes 108
    statistical tables, and each is a group of series. */
export type DatasetCard = {
  /** The short id it is addressed by: `/dataset/wm72qlsa`. Null for a source that
      has collected nothing yet — there is nothing to open. */
  id: string | null
  title: string
  dataset_id: string | null
  /** The publisher's own key for the group this card is, or sits in: `I.1.` is a
      Bank Indonesia table number, `NY.GDP.MKTP.CD` a World Bank indicator code.
      Null only on a dataset card — the rung above the groups. */
  group_id: string | null
  /** Set on a series card — one line of numbers inside one group. Its title is not
      unique ("Lainnya" is the name of 23 of them), so a series also carries the
      group it was published in. */
  series: string | null
  parent_title: string | null
  parent_id: string | null
  queryable: boolean
  source_id: string | null
  source_name: string | null
  description: string | null
  kind: string | null
  schedule: string | null
  enabled: boolean
  section: string | null
  labels: Array<string>
  last_collected: string | null
  row_count: number | null
  column_count: number | null
  indicators: number | null
  first_period: string | null
  last_period: string | null
  unit: string | null
  freq: string | null
}

export type Column = {
  name: string
  type: string
  nullable: boolean
}

/** What `GET /api/tables/{name}` returns — the columns a reader can filter on. */
export type TableInfo = {
  name: string
  row_count: number
  columns: Array<Column>
}

/** A rung of the hierarchy, named. `raw` is the whole merged DuckDB table. */
export type Level = "raw" | "source" | "dataset" | "group" | "series"

/** One step of the trail back up. An id says nothing on its own, so every detail
    page carries the titles above it. */
export type Crumb = { id: string; title: string; level: Level }

export type Dataset = {
  id: string
  level: Level
  title: string
  crumbs: Array<Crumb>
  parent_id: string | null
  parent_title: string | null
  /** The keys behind the id, for a reader who wants to query it themselves. */
  table: string
  dataset_id: string
  /** The publisher's own key for this group: `I.1.`, `NY.GDP.MKTP.CD`. The closest
      thing to a citation the lake can give — quote it at Bank Indonesia and they
      will know which table you mean. Null only on a dataset. */
  group_id: string | null
  series: string | null
  series_code?: string | null
  section: string | null
  source_id: string | null
  row_count: number
  /** Null on a series — it IS one, and "1 series" is noise. */
  series_count: number | null
  /** How many groups are inside it. Set only on a dataset. */
  group_count: number | null
  /** Observations with no value. The World Bank has 2,681 of them, and a missing
      year is not a zero. */
  missing_count: number
  first_period: string | null
  last_period: string | null
  /** Null when the rung spans several — SEKI has 19 units, and claiming one of
      them would be a lie the page then repeats. */
  unit: string | null
  unit_count: number
  freq: string | null
  columns: Array<Column>
  /** The API request that returns these rows. The page turns it into a link to the
      browser, a download, and the copy-paste snippets — there is no SQL endpoint to
      hand anyone a query string for. */
  query: DataQuery
}

/** What is inside a thing: a dataset's groups, or a group's series. */
export type Children = {
  level: Level | null
  items: Array<ChildItem>
  /** The real count. `items` may be shorter — the page says so when it is. */
  total: number
}

export type ChildItem = {
  id: string
  title: string
  level: Level
  row_count: number
  first_period: string | null
  last_period: string | null
  unit: string | null
}

/** What the admin detail page reads: the same description the public page gets, plus
    the line to chart, what is inside it, and what sits beside it — in one request,
    because the page is useless without any of them. */
export type AdminDetail = Dataset & {
  points: Array<SeriesPoint>
  children: Children
  /** The other things on the same rung. A series has nothing inside it, and without
      these its page is a cul-de-sac. Includes the thing itself. */
  siblings: Children
  /** Where the API answers from, as the outside world reaches it. The snippets need a
      URL that works off this machine, and only the server knows what that is. */
  api_url: string
}

/** A query result, or a sample of one. Rows are positional, matching `columns`. */
export type QueryResult = {
  columns: Array<string>
  rows: Array<Array<Cell>>
  row_count: number
  truncated?: boolean
  elapsed_ms?: number
}

/** What `GET /api/data/{id}/rows` returns: one page, and enough to page it.
    `total` counts what the filters match, not what the thing holds — "page 3 of 41"
    is a lie if the 41 counts rows the filter removed. */
export type RowsResult = QueryResult & {
  /** The thing that was read — the id we asked for. */
  id: string
  /** The table it resolved to. Every thing is a filtered view of one. */
  table: string
  total: number
  limit: number
  offset: number
  has_more: boolean
}

/** What `GET /api/data/{id}/aggregate` returns: a GROUP BY, without a query language.
    The last column is the measure; the ones before it are the grouping. */
export type AggregateResult = QueryResult & {
  id: string
  table: string
  group_by: Array<string>
  /** The measure's column name — `count`, or `sum_value`. */
  measure: string
  limit: number
  /** Whether this is the whole ranking or a top-N of a longer one. A bar chart
      captioned "the ten biggest" is right; one captioned "all of them" is not. */
  truncated: boolean
}

export type SeriesPoint = {
  period: string
  /** Null where the publisher reports the period but not the number. A gap, not a
      zero — the chart drops it rather than drawing a collapse that never happened. */
  value: number | null
}

/** What SUMMARIZE gives back, plus the distinct values we add for low-cardinality
    columns — an AI that can see `region IN ('Java','Sumatra')` writes a correct
    filter, and one that cannot writes `region = 'java'` and gets zero rows. */
export type ColumnProfile = {
  column_name: string
  column_type?: string
  min?: Cell
  max?: Cell
  approx_unique?: number | null
  null_percentage?: Cell
  distinct_values?: Array<Cell>
}

// --- endpoint payloads -------------------------------------------------------

export type OverviewPayload = {
  stats: LakeStats
  sources: Array<Source>
  series: HeadlineSeries | null
}

export type StatsPayload = {
  stats: LakeStats
  sources: Array<Source>
}

export type DatasetsPayload = {
  /** One page of them. There are 4,030 in total — the whole set is ~1.8 MB. */
  cards: Array<DatasetCard>
  /** How many match the current filters, across every page. */
  matched: number
  /** How many exist, unfiltered. "60 of 4,030" needs both numbers. */
  total: number
  page: number
  size: number
  pages: number
  /** The three rungs below a source, with their real counts. Every card sits on
      exactly one: a dataset, a group inside it, or a series inside that. */
  levels: { dataset: number; group: number; series: number }
  kinds: Array<string>
  sections: Array<string>
  stats: LakeStats
}

export type DatasetPayload = {
  dataset: Dataset
  sample: QueryResult
  series: Array<SeriesPoint>
  children: Children
  source: Source | null
}

export type TablePayload = {
  table: { name: string; row_count: number; columns: Array<Column> }
  /** Keyed by column name — but SUMMARIZE does not always have a row for every
      column, so a lookup can miss. Saying so is what keeps the page from
      rendering `undefined` into a cell. */
  profile: Partial<Record<string, ColumnProfile>>
  sample: QueryResult
}

// --- the AI stream -----------------------------------------------------------

export type AskEvent =
  | { type: "text"; text: string }
  | { type: "tool_call"; tool: string; input?: { sql?: string } }
  | { type: "tool_result"; tool: string; result: ToolResult }
  | { type: "error"; error: string }
  | { type: "done" }
  | { type: "stream_end" }

export type ToolResult = {
  error?: string
  tables?: Array<string>
  columns?: Array<string>
  rows?: Array<Array<Cell>>
} | null
