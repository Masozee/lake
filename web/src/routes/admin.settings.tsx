import { createFileRoute } from "@tanstack/react-router"
import { useAdmin } from "@/components/admin-bits"
import type { Settings } from "@/lib/admin"

export const Route = createFileRoute("/admin/settings")({
  component: AdminSettings,
})

/** Keys the server reports as a boolean because the value is a secret. Rendered
    as "set"/"not set" so the panel can never become a way to read a key. */
const SECRET_FLAGS = new Set([
  "anthropic_api_key_set",
  "alert_ntfy_url_set",
  "db_configured",
])

const LABELS: Record<string, string> = {
  env: "Environment",
  nas_root: "NAS root",
  raw_root: "Raw archive",
  processed_root: "Processed",
  staging_root: "Staging",
  log_dir: "Log directory",
  log_level: "Log level",
  sources_config: "Source registry",
  alert_enabled: "Alerting",
  api_rate_limit_enabled: "Rate limiting",
  api_rate_catalog_per_min: "Rate: catalog / min",
  api_rate_query_per_min: "Rate: query / min",
  api_rate_ai_per_min: "Rate: AI / min",
  anthropic_api_key_set: "Anthropic API key",
  alert_ntfy_url_set: "Alert webhook",
  db_configured: "Catalog database",
}

function AdminSettings() {
  const { data, error, loading } = useAdmin<Settings>("/settings")

  if (loading) return <p className="muted">Loading…</p>
  if (error) return <p className="notice notice-error">{error}</p>
  if (!data) return null

  const note = String(data.note ?? "")
  const rows = Object.entries(data).filter(([k]) => k !== "note")

  return (
    <>
      <h2 className="section-title">Settings</h2>
      <p className="section-lead">{note}</p>

      <div className="notice mb-4">
        <strong>Secrets are never shown here.</strong>
        <p style={{ margin: "0.5rem 0 0" }}>
          Anything sensitive is reported only as set or not set. A panel that
          can display an API key is a panel that can leak one.
        </p>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Setting</th>
              <th>Value</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([key, value]) => (
              <tr key={key}>
                <td>{LABELS[key] ?? key}</td>
                <td className="mono">
                  {SECRET_FLAGS.has(key) ? (
                    value ? (
                      <span className="pill pill-live">set</span>
                    ) : (
                      <span className="pill pill-off">not set</span>
                    )
                  ) : typeof value === "boolean" ? (
                    <span
                      className={`pill ${value ? "pill-live" : "pill-off"}`}
                    >
                      {value ? "on" : "off"}
                    </span>
                  ) : (
                    String(value)
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
