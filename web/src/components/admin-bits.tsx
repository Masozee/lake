/** The pieces every admin page reuses: a fetch hook and a table. */

import { useCallback, useEffect, useState } from "react"
import type { ReactNode } from "react"
import { admin, errorList } from "@/lib/admin"

/**
 * Fetch an admin endpoint.
 *
 * No SSR and no loader: the panel is private, so there is nothing to
 * server-render, and the session lives in a cookie the browser holds. `reload` is
 * returned so a page that writes can refresh itself without a full navigation.
 */
export function useAdmin<T>(path: string) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      setData(await admin.get<T>(path))
      setError(null)
    } catch (err) {
      setError(errorList(err).join(" "))
    } finally {
      setLoading(false)
    }
  }, [path])

  useEffect(() => {
    void reload()
  }, [reload])

  return { data, error, loading, reload }
}

/** One cell. Numbers right-align in monospace so digits line up down the column. */
export function Cell({
  children,
  mono = false,
  numeric = false,
  wrap = false,
}: {
  children: ReactNode
  mono?: boolean
  numeric?: boolean
  wrap?: boolean
}) {
  return (
    <td
      className={numeric ? "data-mono" : mono ? "mono" : undefined}
      style={{
        textAlign: numeric ? "right" : undefined,
        // Long error messages must wrap rather than push the table sideways.
        whiteSpace: wrap ? "normal" : undefined,
        maxWidth: wrap ? "32rem" : undefined,
      }}
    >
      {children}
    </td>
  )
}

/** A table that scrolls inside its own box, never the page. */
export function AdminTable<T>({
  columns,
  rows,
  render,
  empty = "Nothing here.",
}: {
  columns: Array<string>
  rows: Array<T>
  render: (row: T) => Array<ReactNode>
  empty?: string
}) {
  if (!rows.length) return <p className="muted">{empty}</p>

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {columns.map((c, i) => (
              <th key={`${c}-${i}`}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>{render(row)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
