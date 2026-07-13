import { createFileRoute } from "@tanstack/react-router"
import { useState } from "react"
import { AdminTable, Cell, useAdmin } from "@/components/admin-bits"
import { day, num } from "@/lib/format"
import type { Run, RunError } from "@/lib/admin"

export const Route = createFileRoute("/admin/runs")({ component: AdminRuns })

function AdminRuns() {
  const [days, setDays] = useState(7)
  const runs = useAdmin<Array<Run>>("/runs?limit=200")
  const errors = useAdmin<Array<RunError>>(`/errors?days=${days}&limit=200`)

  return (
    <>
      <h2 className="section-title">Errors</h2>
      <div
        className="field"
        style={{ maxWidth: "12rem", marginBottom: "1rem" }}
      >
        <label htmlFor="days">Window</label>
        <select
          id="days"
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          style={{
            background: "var(--muted)",
            border: 0,
            borderBottom: "1px solid var(--muted-foreground)",
            padding: "11px 16px",
            minHeight: "48px",
            font: "inherit",
            color: "var(--foreground)",
          }}
        >
          <option value={1}>Last 24 hours</option>
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
        </select>
      </div>

      {errors.error && <p className="notice notice-error">{errors.error}</p>}
      {errors.data && (
        <AdminTable
          columns={["Source", "Date", "Try", "Error", "Message", "When"]}
          rows={errors.data}
          empty={`No errors in the last ${days} day${days === 1 ? "" : "s"}.`}
          render={(e) => [
            <Cell key="s" mono>
              {e.source_id}
            </Cell>,
            <Cell key="d">{e.logical_date}</Cell>,
            <Cell key="a" numeric>
              {e.attempt}
            </Cell>,
            <Cell key="c" mono>
              {e.error_class}
            </Cell>,
            <Cell key="m" wrap>
              {e.error_message}
            </Cell>,
            <Cell key="w">{day(e.occurred_at)}</Cell>,
          ]}
        />
      )}

      <h2 className="section-title">Runs</h2>
      {runs.error && <p className="notice notice-error">{runs.error}</p>}
      {runs.data && (
        <AdminTable
          columns={[
            "Source",
            "Date",
            "Status",
            "Try",
            "Trigger",
            "Files",
            "Bytes",
            "Duration",
            "Started",
          ]}
          rows={runs.data}
          empty="Nothing has run yet."
          render={(r) => [
            <Cell key="s" mono>
              {r.source_id}
            </Cell>,
            <Cell key="d">{r.logical_date}</Cell>,
            <Cell key="st">
              <span className={`label label-${r.status}`}>{r.status}</span>
            </Cell>,
            <Cell key="a" numeric>
              {r.attempt}
            </Cell>,
            <Cell key="t">{r.trigger ?? "—"}</Cell>,
            <Cell key="f" numeric>
              {r.file_count ?? "—"}
            </Cell>,
            <Cell key="b" numeric>
              {r.bytes_written === null ? "—" : num(r.bytes_written)}
            </Cell>,
            <Cell key="ms" numeric>
              {r.duration_ms === null
                ? "—"
                : `${num(Math.round(r.duration_ms))} ms`}
            </Cell>,
            <Cell key="w">{day(r.started_at)}</Cell>,
          ]}
        />
      )}
    </>
  )
}
