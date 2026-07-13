import { createFileRoute } from "@tanstack/react-router"
import { AdminTable, Cell, useAdmin } from "@/components/admin-bits"
import { day, num } from "@/lib/format"
import type { DatasetRow, StorageRow } from "@/lib/admin"

export const Route = createFileRoute("/admin/storage")({
  component: AdminStorage,
})

/** Bytes as a human reads them. Binary units, because that is what `du` reports
    and disagreeing with `du` about how full the NAS is helps nobody. */
function bytes(n: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"]
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`
}

function AdminStorage() {
  const { data, error, loading } = useAdmin<{
    files: Array<StorageRow>
    datasets: Array<DatasetRow>
  }>("/storage")

  if (loading) return <p className="muted">Loading…</p>
  if (error) return <p className="notice notice-error">{error}</p>
  if (!data) return null

  const total = data.files.reduce((sum, f) => sum + f.bytes, 0)

  return (
    <>
      <h2 className="section-title">Raw files, by source</h2>
      <p className="section-lead" style={{ marginBottom: "1rem" }}>
        What has actually landed on the NAS. {bytes(total)} across{" "}
        {num(data.files.reduce((s, f) => s + f.files, 0))} files.
      </p>
      <AdminTable
        columns={["Source", "Files", "Size", "Archived", "Deleted", "Newest"]}
        rows={data.files}
        empty="Nothing has been collected yet."
        render={(f) => [
          <Cell key="s" mono>
            {f.source_id}
          </Cell>,
          <Cell key="f" numeric>
            {num(f.files)}
          </Cell>,
          <Cell key="b" numeric>
            {bytes(f.bytes)}
          </Cell>,
          <Cell key="a" numeric>
            {f.archived || "—"}
          </Cell>,
          <Cell key="d" numeric>
            {f.deleted || "—"}
          </Cell>,
          <Cell key="n">{f.newest ? day(f.newest) : "—"}</Cell>,
        ]}
      />

      <h2 className="section-title">Datasets</h2>
      <p className="section-lead" style={{ marginBottom: "1rem" }}>
        The processed Parquet outputs, each with lineage back to the run that
        built it.
      </p>
      <AdminTable
        columns={[
          "Dataset",
          "Source",
          "Rows",
          "Partitioned by",
          "Format",
          "Built",
        ]}
        rows={data.datasets}
        empty="No datasets built yet. Run `lake transform <dataset_id>`."
        render={(d) => [
          <Cell key="d" mono>
            {d.dataset_id}
          </Cell>,
          <Cell key="s" mono>
            {d.source_id ?? "—"}
          </Cell>,
          <Cell key="r" numeric>
            {d.row_count === null ? "—" : num(d.row_count)}
          </Cell>,
          <Cell key="p" mono>
            {d.partition_keys?.join(", ") || "—"}
          </Cell>,
          <Cell key="f">{d.format}</Cell>,
          <Cell key="b">{day(d.built_at)}</Cell>,
        ]}
      />
    </>
  )
}
