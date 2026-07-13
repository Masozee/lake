import { createFileRoute } from "@tanstack/react-router"
import { useState } from "react"
import { AdminTable, Cell, useAdmin } from "@/components/admin-bits"
import { day } from "@/lib/format"
import type { AuditEntry } from "@/lib/admin"

export const Route = createFileRoute("/admin/audit")({ component: AdminAudit })

function AdminAudit() {
  const { data, error, loading } =
    useAdmin<Array<AuditEntry>>("/audit?limit=200")
  const [open, setOpen] = useState<string | null>(null)

  if (loading) return <p className="muted">Loading…</p>
  if (error) return <p className="notice notice-error">{error}</p>

  return (
    <>
      <h2 className="section-title">Audit log</h2>
      <p className="section-lead" style={{ marginBottom: "1rem" }}>
        Every write this panel has made, and who made it. A config edit records
        the full previous content — this is what stands in for the git commit
        that a browser edit does not produce.
      </p>

      <AdminTable
        columns={["When", "Who", "Action", "Target", ""]}
        rows={data ?? []}
        empty="Nothing has been changed from the panel yet."
        render={(a) => [
          <Cell key="w">{day(a.occurred_at)}</Cell>,
          <Cell key="who" mono>
            {a.actor_email}
          </Cell>,
          <Cell key="a">
            <span className="label">{a.action}</span>
          </Cell>,
          <Cell key="t" mono wrap>
            {a.target ?? "—"}
          </Cell>,
          <Cell key="d">
            {"previous" in a.detail ? (
              <button
                type="button"
                className="btn btn-outline"
                onClick={() => setOpen(open === a.entry_id ? null : a.entry_id)}
              >
                {open === a.entry_id ? "Hide" : "What changed"}
              </button>
            ) : (
              "—"
            )}
          </Cell>,
        ]}
      />

      {/* The previous content, in full. Not a diff: reconstructing one in the
          browser would be a second implementation of the truth, and the whole
          point of storing the file is that it IS the truth. */}
      {open && data && (
        <>
          <h3 className="section-title">Previous content</h3>
          {(() => {
            const entry = data.find((a) => a.entry_id === open)
            const previous = entry?.detail.previous
            const backup = entry?.detail.backup
            return (
              <>
                {typeof backup === "string" && (
                  <p className="muted mb-4" style={{ fontSize: "0.875rem" }}>
                    Also on disk as{" "}
                    <code className="mono">configs/backups/{backup}</code>.
                  </p>
                )}
                <pre
                  className="code"
                  style={{ maxHeight: "30rem", overflow: "auto" }}
                >
                  <code>{typeof previous === "string" ? previous : "—"}</code>
                </pre>
              </>
            )
          })()}
        </>
      )}
    </>
  )
}
