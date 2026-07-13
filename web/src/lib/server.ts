/**
 * Server functions the route loaders call.
 *
 * These run on the server during SSR and on navigation, so the page arrives with
 * its data already in it — a reader with a slow connection sees the datasets, not
 * a spinner, and a crawler sees them too. The API is only ever reached from here.
 */

import { createServerFn } from "@tanstack/react-start"
import { api } from "@/lib/api"
import type {
  DatasetPayload,
  DatasetsPayload,
  OverviewPayload,
  StatsPayload,
  TablePayload,
} from "@/lib/types"

export const fetchOverview = createServerFn({ method: "GET" }).handler(() =>
  api.get<OverviewPayload>("/api/ui/overview")
)

export const fetchStats = createServerFn({ method: "GET" }).handler(() =>
  api.get<StatsPayload>("/api/ui/stats")
)

export type DatasetFilters = {
  q?: string
  kind?: string
  status?: string
  section?: string
  /** "table" or "series". Empty means both rungs. */
  level?: string
  page?: number
}

export const fetchDatasets = createServerFn({ method: "GET" })
  .validator((filters: DatasetFilters) => filters)
  .handler(({ data }) => {
    const query = new URLSearchParams()
    // The text filters and the page number are set separately, because they are
    // empty in different ways: a filter is absent when it is "", and a page is
    // absent when it is undefined — `page: 0` is a real page, and a truthiness
    // test would silently drop it.
    for (const key of ["q", "kind", "status", "section", "level"] as const) {
      const value = data[key]
      if (value) query.set(key, value)
    }
    if (data.page !== undefined) query.set("page", String(data.page))

    const suffix = query.size ? `?${query}` : ""
    return api.get<DatasetsPayload>(`/api/ui/datasets${suffix}`)
  })

export const fetchDataset = createServerFn({ method: "GET" })
  .validator((id: string) => id)
  .handler(({ data }) =>
    api.get<DatasetPayload>(`/api/ui/dataset/${encodeURIComponent(data)}`)
  )

export const fetchTable = createServerFn({ method: "GET" })
  .validator((name: string) => name)
  .handler(({ data }) =>
    api.get<TablePayload>(`/api/ui/table/${encodeURIComponent(data)}`)
  )
