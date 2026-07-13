/**
 * The admin panel's client.
 *
 * Unlike the public pages, this does NOT go through server functions. The panel
 * is a private, authenticated tool — there is nothing to server-render for a
 * crawler, and the session lives in an httpOnly cookie the browser holds. So it
 * calls `/api/admin/*` directly and the proxy forwards the cookie along.
 *
 * `credentials: "same-origin"` is the whole reason this works: the browser only
 * attaches the cookie if we ask it to.
 */

export class AdminError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown
  ) {
    super(typeof detail === "string" ? detail : `request failed (${status})`)
    this.name = "AdminError"
  }
}

/** Raised on a 401 so callers can send the reader back to the login form. */
export class NotSignedIn extends AdminError {
  constructor() {
    super(401, "not signed in")
    this.name = "NotSignedIn"
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/admin${path}`, {
    ...init,
    credentials: "same-origin",
    headers: { "content-type": "application/json", ...init?.headers },
  })

  if (res.status === 401) throw new NotSignedIn()

  if (!res.ok) {
    const detail = await res
      .json()
      .then((b: { detail?: unknown }) => b.detail ?? b)
      .catch(() => res.statusText)
    throw new AdminError(res.status, detail)
  }

  return res.status === 204 ? (undefined as T) : (res.json() as Promise<T>)
}

export const admin = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
}

/** The errors an admin API call comes back with, as a list the form can show. */
export function errorList(err: unknown): Array<string> {
  if (err instanceof AdminError) {
    const d = err.detail
    if (typeof d === "string") return [d]
    if (
      d &&
      typeof d === "object" &&
      Array.isArray((d as { errors?: unknown }).errors)
    ) {
      return (d as { errors: Array<string> }).errors
    }
  }
  return [err instanceof Error ? err.message : String(err)]
}

// --- what the endpoints return -----------------------------------------------

export type Me = { email: string; display_name: string }

export type Health = {
  sources: number
  stale: number
  runs_24h: number
  failures_24h: number
  stale_ids: Array<string>
}

export type Freshness = {
  source_id: string
  display_name: string
  schedule: string
  enabled: boolean
  freshness_sla_hours: number | null
  last_success_at: string | null
  last_status: string | null
  hours_since_success: number | null
  is_stale: boolean
}

export type Run = {
  run_id: string
  source_id: string
  logical_date: string
  status: string
  attempt: number
  trigger: string | null
  file_count: number | null
  bytes_written: number | null
  duration_ms: number | null
  started_at: string
  finished_at: string | null
}

export type RunError = {
  source_id: string
  logical_date: string
  attempt: number
  error_class: string
  error_message: string
  occurred_at: string
}

export type Quiet = {
  source_id: string
  last_observed: string
  new_files: number
  observations: number
}

export type Overview = {
  health: Health
  freshness: Array<Freshness>
  runs: Array<Run>
  errors: Array<RunError>
  quiet: Array<Quiet>
}

export type StorageRow = {
  source_id: string
  files: number
  bytes: number
  deleted: number
  archived: number
  newest: string | null
}

export type DatasetRow = {
  dataset_id: string
  source_id: string | null
  nas_path: string
  format: string
  row_count: number | null
  partition_keys: Array<string> | null
  built_at: string
}

export type AuditEntry = {
  entry_id: string
  actor_email: string
  action: string
  target: string | null
  detail: Record<string, unknown>
  occurred_at: string
}

export type AdminUser = {
  user_id: string
  email: string
  display_name: string
  is_active: boolean
  created_at: string
  last_login_at: string | null
  is_you: boolean
}

export type SourcesFile = {
  path: string
  content: string
  backups: Array<{ name: string; size: number; written_at: string }>
}

export type Settings = Record<string, string | number | boolean>
