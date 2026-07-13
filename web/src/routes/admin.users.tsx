import { createFileRoute } from "@tanstack/react-router"
import { useState } from "react"
import { AdminTable, Cell, useAdmin } from "@/components/admin-bits"
import { admin, errorList } from "@/lib/admin"
import { day } from "@/lib/format"
import type { AdminUser } from "@/lib/admin"

export const Route = createFileRoute("/admin/users")({ component: AdminUsers })

/** Matches the server. Stated here only so the form can say it up front rather
    than bouncing the reader after they have typed something too short. */
const MIN_PASSWORD = 12

function AdminUsers() {
  const { data, error, loading, reload } = useAdmin<Array<AdminUser>>("/users")
  const [errors, setErrors] = useState<Array<string>>([])
  const [busy, setBusy] = useState(false)

  async function act<T>(fn: () => Promise<T>) {
    setBusy(true)
    setErrors([])
    try {
      await fn()
      await reload()
    } catch (err) {
      setErrors(errorList(err))
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <p className="muted">Loading…</p>
  if (error) return <p className="notice notice-error">{error}</p>

  return (
    <>
      <h2 className="section-title">Admins</h2>
      <p className="section-lead" style={{ marginBottom: "1rem" }}>
        Everyone who can sign in here. Disabling someone revokes every session
        they hold — they are signed out everywhere, immediately. A disabled
        account keeps its row, so the audit log still resolves who did what.
      </p>

      {errors.length > 0 && (
        <div className="notice notice-error mb-4" role="alert">
          {errors.map((e) => (
            <p key={e} style={{ margin: 0 }}>
              {e}
            </p>
          ))}
        </div>
      )}

      <AdminTable
        columns={["Email", "Name", "Status", "Created", "Last login", ""]}
        rows={data ?? []}
        render={(u) => [
          <Cell key="e" mono>
            {u.email}
            {u.is_you && <span className="muted"> (you)</span>}
          </Cell>,
          <Cell key="n">{u.display_name}</Cell>,
          <Cell key="s">
            <span className={`pill ${u.is_active ? "pill-live" : "pill-off"}`}>
              {u.is_active ? "Active" : "Disabled"}
            </span>
          </Cell>,
          <Cell key="c">{day(u.created_at)}</Cell>,
          <Cell key="l">
            {u.last_login_at ? day(u.last_login_at) : "never"}
          </Cell>,
          <Cell key="a">
            {/* You cannot disable yourself — the server refuses too. Locking the
                only admin out of the panel that unlocks them is a bad afternoon. */}
            {u.is_you ? (
              <span className="muted" style={{ fontSize: "0.75rem" }}>
                —
              </span>
            ) : (
              <button
                type="button"
                className="btn btn-outline"
                disabled={busy}
                onClick={() =>
                  void act(() =>
                    admin.patch(`/users/${u.user_id}`, {
                      is_active: !u.is_active,
                    })
                  )
                }
              >
                {u.is_active ? "Disable" : "Enable"}
              </button>
            )}
          </Cell>,
        ]}
      />

      <h2 className="section-title">Add an admin</h2>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault()
          const form = new FormData(event.currentTarget)
          const el = event.currentTarget
          void act(async () => {
            await admin.post("/users", {
              email: String(form.get("email") ?? ""),
              display_name: String(form.get("display_name") ?? ""),
              password: String(form.get("password") ?? ""),
            })
            el.reset()
          })
        }}
      >
        <div className="field">
          <label htmlFor="new-email">Email</label>
          <input
            id="new-email"
            name="email"
            type="email"
            required
            autoComplete="off"
          />
        </div>
        <div className="field">
          <label htmlFor="new-name">Display name</label>
          <input
            id="new-name"
            name="display_name"
            type="text"
            autoComplete="off"
          />
        </div>
        <div className="field">
          <label htmlFor="new-password">
            Password — at least {MIN_PASSWORD} characters
          </label>
          <input
            id="new-password"
            name="password"
            type="password"
            required
            minLength={MIN_PASSWORD}
            autoComplete="new-password"
          />
        </div>
        <button
          type="submit"
          className="btn btn-primary"
          style={{ minHeight: "48px" }}
          disabled={busy}
        >
          Create admin
        </button>
      </form>

      <h2 className="section-title">Change your password</h2>
      <p className="section-lead" style={{ marginBottom: "1rem" }}>
        Changing it signs out every other browser holding your account — which
        is what you want if you are changing it because someone else might have
        it.
      </p>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault()
          const form = new FormData(event.currentTarget)
          const el = event.currentTarget
          void act(async () => {
            await admin.post("/password", {
              current_password: String(form.get("current_password") ?? ""),
              new_password: String(form.get("new_password") ?? ""),
            })
            el.reset()
          })
        }}
      >
        <div className="field">
          <label htmlFor="cur-pw">Current password</label>
          <input
            id="cur-pw"
            name="current_password"
            type="password"
            required
            autoComplete="current-password"
          />
        </div>
        <div className="field">
          <label htmlFor="new-pw">New password</label>
          <input
            id="new-pw"
            name="new_password"
            type="password"
            required
            minLength={MIN_PASSWORD}
            autoComplete="new-password"
          />
        </div>
        <button
          type="submit"
          className="btn btn-primary"
          style={{ minHeight: "48px" }}
          disabled={busy}
        >
          Change password
        </button>
      </form>
    </>
  )
}
