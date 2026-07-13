/**
 * The admin shell: a login gate, the sidebar, and the working area beside it.
 *
 * The gate here is a convenience, not the security boundary. The real one is the
 * API: every /api/admin/* route requires a session cookie and 401s without it, so
 * a reader who bypasses this component reaches a panel with no data in it. This
 * exists so they see a login form instead of a wall of failed requests.
 */

import { Outlet, createFileRoute, useRouterState } from "@tanstack/react-router"
import { useCallback, useEffect, useState } from "react"
import { AdminSidebar } from "@/components/admin-sidebar"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { TooltipProvider } from "@/components/ui/tooltip"
import { NotSignedIn, admin, errorList } from "@/lib/admin"
import type { Me } from "@/lib/admin"

export const Route = createFileRoute("/admin")({
  head: () => ({
    meta: [
      { title: "Admin · lake" },
      // Never index the admin panel, and never follow a link out of it.
      { name: "robots", content: "noindex, nofollow" },
    ],
  }),
  component: AdminShell,
})

/** What each page calls itself, for the bar above the working area. */
const TITLES: Array<[string, string]> = [
  ["/admin/runs", "Runs & errors"],
  ["/admin/storage", "Storage"],
  ["/admin/data", "Data"],
  ["/admin/sources", "Sources"],
  ["/admin/audit", "Audit log"],
  ["/admin/users", "Admins"],
  ["/admin/settings", "Settings"],
  ["/admin", "Overview"], // last: every other path starts with it
]

function AdminShell() {
  const [me, setMe] = useState<Me | null>(null)
  const [checking, setChecking] = useState(true)
  const path = useRouterState({ select: (s) => s.location.pathname })

  const check = useCallback(async () => {
    try {
      setMe(await admin.get<Me>("/me"))
    } catch {
      setMe(null) // any failure means "show the login form"
    } finally {
      setChecking(false)
    }
  }, [])

  useEffect(() => {
    void check()
  }, [check])

  // Don't flash the login form at someone who is already signed in.
  if (checking) {
    return (
      <main className="wrap page-pad">
        <p className="muted">Checking your session…</p>
      </main>
    )
  }

  if (!me) return <LoginForm onSignedIn={check} />

  const title = TITLES.find(([to]) => path.startsWith(to))?.[1] ?? "Admin"

  return (
    // TooltipProvider, because the rail collapses to icons and the labels then
    // live only in tooltips. SidebarProvider does not bring its own.
    <TooltipProvider>
      <SidebarProvider>
        <AdminSidebar
          me={me}
          onSignOut={async () => {
            await admin.post("/logout")
            setMe(null)
          }}
        />
        <SidebarInset>
          <header className="admin-bar">
            <SidebarTrigger />
            <h1 className="admin-bar-title">{title}</h1>
          </header>
          <div className="admin-body">
            <Outlet />
          </div>
        </SidebarInset>
      </SidebarProvider>
    </TooltipProvider>
  )
}

function LoginForm({ onSignedIn }: { onSignedIn: () => void }) {
  const [busy, setBusy] = useState(false)
  const [errors, setErrors] = useState<Array<string>>([])

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setErrors([])

    const form = new FormData(event.currentTarget)
    try {
      await admin.post<Me>("/login", {
        email: String(form.get("email") ?? ""),
        password: String(form.get("password") ?? ""),
      })
      onSignedIn()
    } catch (err) {
      // The API says the same thing for a wrong password and an unknown account,
      // on purpose. Don't dress it up into something more specific.
      setErrors(
        err instanceof NotSignedIn
          ? ["Email or password is wrong."]
          : errorList(err)
      )
      setBusy(false)
    }
  }

  return (
    <main className="wrap page-pad" style={{ maxWidth: "26rem" }}>
      <p className="eyebrow">Admin</p>
      <h1 className="page-title">Sign in</h1>
      <p className="page-sub">
        This panel is for the people who run the lake. Accounts are created from
        the server with <code className="mono">lake admin create-user</code> —
        there is no sign-up.
      </p>

      <form className="form" onSubmit={submit}>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            name="email"
            type="email"
            required
            autoComplete="username"
            autoFocus
          />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            name="password"
            type="password"
            required
            autoComplete="current-password"
          />
        </div>

        {errors.length > 0 && (
          <div className="notice notice-error" role="alert">
            {errors.map((e) => (
              <p key={e} style={{ margin: 0 }}>
                {e}
              </p>
            ))}
          </div>
        )}

        <button
          type="submit"
          className="btn btn-primary"
          style={{ minHeight: "48px" }}
          disabled={busy}
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  )
}
