/**
 * The FastAPI backend, as seen from this app's server.
 *
 * Calls here run inside server functions and server routes, never in the browser:
 * the API binds to localhost and is reached over Tailscale or behind an
 * authenticating proxy, so it is not something a public page may fetch directly.
 * The browser talks only to this app, and `/api/*` is proxied through to FastAPI
 * (see routes/api.$.ts) — one origin, so no CORS and no API address in the client
 * bundle.
 */

export const API_URL = process.env.LAKE_API_URL ?? "http://127.0.0.1:8000"

/** Raised when the API answers, but with a status the page has to handle. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown
  ) {
    super(typeof detail === "string" ? detail : `API error ${status}`)
    this.name = "ApiError"
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  })

  if (!res.ok) {
    // The API answers errors as JSON; a proxy in front of it might not.
    const detail = await res
      .json()
      .then((body: { detail?: unknown }) => body.detail ?? body)
      .catch(() => res.statusText)
    throw new ApiError(res.status, detail)
  }

  return res.json() as Promise<T>
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body) }),
}
