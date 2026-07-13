import { createFileRoute } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"
import { useAdmin } from "@/components/admin-bits"
import { admin, errorList } from "@/lib/admin"
import { day } from "@/lib/format"
import type { SourcesFile } from "@/lib/admin"

export const Route = createFileRoute("/admin/sources")({
  component: AdminSources,
})

type Validation = {
  ok: boolean
  errors: Array<string>
  sources?: Array<string>
}

function AdminSources() {
  const { data, error, loading, reload } = useAdmin<SourcesFile>("/sources")

  const [draft, setDraft] = useState("")
  const [check, setCheck] = useState<Validation | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState<string | null>(null)
  const [failed, setFailed] = useState<Array<string>>([])
  const loaded = useRef(false)

  // Seed the editor once. Re-seeding on every reload would throw away an edit in
  // progress the moment anything else refetched.
  useEffect(() => {
    if (data && !loaded.current) {
      setDraft(data.content)
      loaded.current = true
    }
  }, [data])

  // Validate as you type, debounced. The server owns the rules — re-implementing
  // them here would give two answers to the same question, and the browser's would
  // be the one that is wrong.
  useEffect(() => {
    if (!draft) return
    const timer = setTimeout(() => {
      admin
        .post<Validation>("/sources/validate", { content: draft })
        .then(setCheck)
        .catch((err) => setCheck({ ok: false, errors: errorList(err) }))
    }, 400)
    return () => clearTimeout(timer)
  }, [draft])

  if (loading) return <p className="muted">Loading…</p>
  if (error) return <p className="notice notice-error">{error}</p>
  if (!data) return null

  const dirty = draft !== data.content

  async function save() {
    if (saving || !dirty) return
    setSaving(true)
    setFailed([])
    setSaved(null)
    try {
      const res = await admin.put<{ backup: string | null; note?: string }>(
        "/sources",
        {
          content: draft,
        }
      )
      setSaved(res.backup)
      loaded.current = false // let the reload re-seed from what is now on disk
      await reload()
    } catch (err) {
      setFailed(errorList(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <h2 className="section-title">The source registry</h2>
      <p className="section-lead">
        This is <code className="mono">{data.path}</code>, verbatim. It is a
        git-tracked file that normally changes through review — editing it here
        bypasses that, so every save is validated first, backed up before it
        overwrites, and recorded in the audit log with the full previous
        content.
      </p>

      <div className="notice mb-4">
        <strong>Two things this will not do.</strong>
        <p style={{ margin: "0.5rem 0 0" }}>
          It will not accept a literal secret — put those in{" "}
          <code className="mono">/etc/lake/lake.env</code> and reference them as{" "}
          <code className="mono">{"${env:VAR_NAME}"}</code>. And saving does not
          push the change into the catalog: run{" "}
          <code className="mono">lake sync-sources</code> for that.
        </p>
      </div>

      <div className="field">
        <label htmlFor="yaml">configs/sources.yaml</label>
        <textarea
          id="yaml"
          className="mono"
          spellCheck={false}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value)
            // A "Saved." banner sitting above unsaved edits is a lie. Clear it the
            // moment the file diverges from what is on disk again.
            setSaved(null)
          }}
          style={{ minHeight: "28rem", fontSize: "0.8125rem", lineHeight: 1.6 }}
        />
      </div>

      {check && !check.ok && (
        <div className="notice notice-error" role="alert">
          <strong>This would break something.</strong>
          <ul>
            {check.errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </div>
      )}
      {/* Only while there is something unsaved to say it about. After a save the
          confirmation below is the thing to read — two green boxes stacked, one
          of them stale, is how a reader misses the one that matters. */}
      {check?.ok && dirty && !saved && (
        <div className="notice" role="status">
          <strong>Valid.</strong>
          <p style={{ margin: "0.5rem 0 0" }}>
            {check.sources?.length} source
            {check.sources?.length === 1 ? "" : "s"}:{" "}
            <span className="mono">{check.sources?.join(", ")}</span>
          </p>
        </div>
      )}

      {failed.length > 0 && (
        <div className="notice notice-error" role="alert">
          <strong>Not saved.</strong>
          <ul>
            {failed.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </div>
      )}
      {saved && (
        <div className="notice" role="status">
          <strong>Saved.</strong>
          <p style={{ margin: "0.5rem 0 0" }}>
            The previous version is backed up as{" "}
            <code className="mono">{saved}</code>. Run{" "}
            <code className="mono">lake sync-sources</code> to push this into
            the catalog.
          </p>
        </div>
      )}

      <div className="hstack mt-3">
        <button
          type="button"
          className="btn btn-primary"
          style={{ minHeight: "48px" }}
          onClick={() => void save()}
          disabled={saving || !dirty || check?.ok === false}
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          className="btn btn-outline"
          style={{ minHeight: "48px" }}
          onClick={() => {
            setDraft(data.content)
            setFailed([])
            setSaved(null)
          }}
          disabled={!dirty}
        >
          Discard changes
        </button>
        {dirty && <span className="muted">Unsaved changes.</span>}
      </div>

      <h2 className="section-title">Backups</h2>
      <p className="section-lead" style={{ marginBottom: "1rem" }}>
        Taken automatically before every save — this is the undo. To restore
        one, open it, copy it into the editor above, and save.
      </p>
      {data.backups.length === 0 ? (
        <p className="muted">No backups yet. The first save will make one.</p>
      ) : (
        <ul className="backup-list">
          {data.backups.map((b) => (
            <li key={b.name}>
              <button
                type="button"
                className="btn btn-ghost mono"
                onClick={async () => {
                  const one = await admin.get<{ content: string }>(
                    `/sources/backups/${encodeURIComponent(b.name)}`
                  )
                  setDraft(one.content)
                }}
              >
                {b.name}
              </button>
              <span className="muted" style={{ fontSize: "0.75rem" }}>
                {day(b.written_at)} · {b.size} bytes
              </span>
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
