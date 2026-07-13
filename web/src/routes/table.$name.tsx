import { Link, createFileRoute } from "@tanstack/react-router"
import { ResultTable } from "@/components/blocks"
import { num } from "@/lib/format"
import { exportUrl } from "@/lib/query"
import { fetchTable } from "@/lib/server"

export const Route = createFileRoute("/table/$name")({
  loader: ({ params }) => fetchTable({ data: params.name }),
  head: ({ loaderData }) => ({
    meta: [{ title: `${loaderData?.table.name ?? "Table"} · lake` }],
  }),
  component: TableDetail,
})

function TableDetail() {
  const { table, profile, sample } = Route.useLoaderData()

  return (
    <main className="wrap page-pad">
      <p className="muted mb-4">
        <Link to="/datasets" search={{}} className="reset">
          ← all datasets
        </Link>
      </p>
      <h1 className="page-title mono">{table.name}</h1>
      <p className="page-sub">
        {num(table.row_count)} rows · {table.columns.length} columns
      </p>

      <div className="hstack mb-4">
        <span className="muted">Download the whole dataset:</span>
        {/* The raw table is addressed by name where everything else is addressed by an
            id — it is not a dataset, it is what all of them are views of. Followed by
            the browser, not fetched: the proxy streams it straight through, so a large
            table never buffers in this app's memory. */}
        <a
          className="btn btn-outline"
          href={exportUrl({ id: table.name, filters: {} }, "csv")}
        >
          CSV
        </a>
        <a
          className="btn btn-outline"
          href={exportUrl({ id: table.name, filters: {} }, "xlsx")}
        >
          Excel
        </a>
        <Link
          to="/query"
          search={{ id: table.name }}
          className="btn btn-outline"
        >
          Browse it →
        </Link>
      </div>

      <h2 className="section-title">Columns</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>column</th>
              <th>type</th>
              <th>nullable</th>
              <th>distinct</th>
              <th>values / range</th>
            </tr>
          </thead>
          <tbody>
            {table.columns.map((column) => {
              const p = profile[column.name]
              return (
                <tr key={column.name}>
                  <td className="mono">{column.name}</td>
                  <td className="muted">{column.type}</td>
                  <td>
                    {column.nullable ? (
                      <span className="badge">nullable</span>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="mono">{p?.approx_unique ?? "—"}</td>
                  <td className="muted">
                    {p?.distinct_values?.length
                      ? p.distinct_values.slice(0, 6).map(String).join(", ")
                      : p
                        ? `${String(p.min)} … ${String(p.max)}`
                        : ""}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <h2 className="section-title">Sample (first 20 rows)</h2>
      <ResultTable result={sample} />
    </main>
  )
}
