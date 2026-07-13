/**
 * Passes `/api/*` through to FastAPI.
 *
 * Two things need the browser to reach the API directly, and neither can go
 * through a server function: an export link the browser follows itself (CSV,
 * Excel — a server function would have to buffer the whole file to hand it back),
 * and the AI answer, which is a live SSE stream the page renders as it arrives.
 *
 * Proxying rather than exposing FastAPI means one origin: no CORS, no API address
 * in the client bundle, and in production one reverse proxy in front of one port.
 * The body is piped, never buffered, so a large export and a long stream both
 * flow straight through.
 */

import { createFileRoute } from "@tanstack/react-router"
import { API_URL } from "@/lib/api"

async function proxy({ request }: { request: Request }): Promise<Response> {
  const url = new URL(request.url)
  const target = `${API_URL}${url.pathname}${url.search}`

  // Buffer the request body rather than forwarding `request.body` as a stream.
  // The server runtime may already have consumed that stream by the time this
  // runs, and re-sending a spent one fails with "expected non-null body source" —
  // a TypeError, which surfaces as an opaque 500 and makes a rejected login look
  // like a broken server. Requests here are small JSON payloads; it is the
  // *responses* — a large CSV, a live SSE stream — that must not be buffered, and
  // those still are not.
  const hasBody = request.method !== "GET" && request.method !== "HEAD"
  const body = hasBody ? await request.arrayBuffer() : undefined

  const upstream = await fetch(target, {
    method: request.method,
    headers: stripHopByHop(request.headers),
    body: body && body.byteLength > 0 ? body : undefined,
  })

  // The response body IS streamed through, untouched. Content-Disposition rides
  // along with it, so an export still downloads with the filename the API chose,
  // and the AI answer still arrives token by token.
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: stripHopByHop(upstream.headers),
  })
}

/** Headers that describe *this* hop and must not be forwarded to the next one. */
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "transfer-encoding",
  "upgrade",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "host",
  "content-length",
])

function stripHopByHop(headers: Headers): Headers {
  const out = new Headers()
  headers.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) out.append(key, value)
  })
  return out
}

export const Route = createFileRoute("/api/$")({
  server: {
    handlers: {
      GET: proxy,
      POST: proxy,
    },
  },
})
