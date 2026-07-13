import { createFileRoute } from "@tanstack/react-router"
import { AdminTable, Cell, useAdmin } from "@/components/admin-bits"
import { day, num } from "@/lib/format"
import type { Overview } from "@/lib/admin"

export const Route = createFileRoute("/admin/")({ component: AdminOverview })

function AdminOverview() {
  const { data, error, loading } = useAdmin<Overview>("/overview")

  if (loading) return <p className="muted">Loading…</p>
  if (error) return <p className="notice notice-error">{error}</p>
  if (!data) return null

  const { health, freshness, runs, errors, quiet } = data

  return (
    <>
      <section className="stat-band admin-stats">
        <Stat value={health.sources} label="Sources" />
        <Stat value={health.stale} label="Stale" bad={health.stale > 0} />
        <Stat value={health.runs_24h} label="Runs (24h)" />
        <Stat
          value={health.failures_24h}
          label="Failures (24h)"
          bad={health.failures_24h > 0}
        />
      </section>

      {health.stale > 0 && (
        <div className="notice notice-error mt-3" role="alert">
          <strong>
            {health.stale} source{health.stale === 1 ? "" : "s"} past the
            freshness SLA.
          </strong>
          <p style={{ margin: "0.5rem 0 0" }}>
            A stale source has not succeeded within its SLA. This is the check
            that catches a scraper which silently stopped being scheduled — it
            never fails, because it never runs.
          </p>
        </div>
      )}

      <h2 className="section-title">Freshness</h2>
      <AdminTable
        columns={[
          "Source",
          "Schedule",
          "Last status",
          "Hours since success",
          "SLA",
          "",
        ]}
        rows={freshness}
        empty="No sources registered. Run `lake sync-sources`."
        render={(f) => [
          <Cell key="s" mono>
            {f.source_id}
          </Cell>,
          <Cell key="sc">{f.schedule}</Cell>,
          <Cell key="ls">
            {f.last_status ?? <span className="cell-null">never run</span>}
          </Cell>,
          <Cell key="h" numeric>
            {f.hours_since_success === null
              ? "—"
              : f.hours_since_success.toFixed(1)}
          </Cell>,
          <Cell key="sla" numeric>
            {f.freshness_sla_hours ?? "—"}
          </Cell>,
          <Cell key="st">
            {f.is_stale ? (
              <span className="label label-stale">stale</span>
            ) : (
              <span className="pill pill-live">ok</span>
            )}
          </Cell>,
        ]}
      />

      <h2 className="section-title">Quiet sources</h2>
      <p className="section-lead" style={{ marginBottom: "1rem" }}>
        Succeeding, but every file fetched is byte-identical to one already
        held. The source stopped publishing — that is not a scraper bug, and it
        is a different fix. Telling the two apart is the entire reason{" "}
        <code className="mono">file_observations.was_new</code> exists.
      </p>
      {quiet.length === 0 ? (
        <p className="muted">
          Every source has published something new in the last 30 days.
        </p>
      ) : (
        <AdminTable
          columns={["Source", "Last observed", "New files", "Observations"]}
          rows={quiet}
          render={(q) => [
            <Cell key="s" mono>
              {q.source_id}
            </Cell>,
            <Cell key="l">{day(q.last_observed)}</Cell>,
            <Cell key="n" numeric>
              {q.new_files}
            </Cell>,
            <Cell key="o" numeric>
              {q.observations}
            </Cell>,
          ]}
        />
      )}

      <h2 className="section-title">Recent errors</h2>
      {errors.length === 0 ? (
        <p className="muted">No errors in the last 7 days.</p>
      ) : (
        <AdminTable
          columns={["Source", "Date", "Try", "Error", "Message", "When"]}
          rows={errors}
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

      <h2 className="section-title">Recent runs</h2>
      <AdminTable
        columns={[
          "Source",
          "Date",
          "Status",
          "Try",
          "Files",
          "Bytes",
          "Duration",
          "Started",
        ]}
        rows={runs}
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
    </>
  )
}

function Stat({
  value,
  label,
  bad = false,
}: {
  value: number
  label: string
  bad?: boolean
}) {
  return (
    <div className="stat">
      {/* `--danger-ink`, not `--carbon-error`: this is red *text* on the page
          background, and Red 60 does not clear AA on charcoal. The token flips
          to Red 50 in the dark theme; the raw colour would not. */}
      <div
        className="stat-value"
        style={bad ? { color: "var(--danger-ink)" } : undefined}
      >
        {num(value)}
      </div>
      <div className="stat-label">{label}</div>
    </div>
  )
}
