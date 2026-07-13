import { createFileRoute } from "@tanstack/react-router"
import { Cta, Pipeline } from "@/components/blocks"
import { Reveal } from "@/components/reveal"
import { fetchStats } from "@/lib/server"

export const Route = createFileRoute("/about")({
  loader: () => fetchStats(),
  head: () => ({ meta: [{ title: "About · lake" }] }),
  component: About,
})

const GUARANTEES = [
  {
    title: "Raw data is immutable",
    body: "Bytes land with an atomic rename and are then read-only. A later run supersedes an earlier one; it never edits it. You can always go back to what the source actually served.",
  },
  {
    title: "Every run is recorded",
    body: "Runs, files, and checksums go into a Postgres catalog. That is what lets the system tell you whether a source went quiet or a scraper broke — two very different problems that look identical from the outside.",
  },
  {
    title: "Transforms are idempotent",
    body: "Parquet partitions are rebuilt, never appended to. Run a transform twice and you get the same answer, so a half-finished job is safe to simply run again.",
  },
  {
    title: "Serving is read-only",
    body: "The DuckDB connection behind this website is opened read-only with external file access disabled. The guarantee is structural, not a promise in a comment.",
  },
]

/** Verbatim from docs/deployment.md and docs/api.md. If those change, change these. */
const LOCAL = `# install and set up the catalog
uv venv && uv pip install -e '.[dev]'
cp .env.example .env
createdb lake_meta
uv run alembic upgrade head
uv run lake sync-sources
uv run lake doctor

# build the replica, then serve it
uv run lake serve build
uv run lake serve run`

const SERVER = `# install the systemd units
sudo make deploy

# mount the NAS, migrate, verify
sudo systemctl enable --now mnt-nas.mount
sudo -u lake .venv/bin/alembic upgrade head
sudo -u lake .venv/bin/lake sync-sources
sudo -u lake .venv/bin/lake doctor

# start the timers and the API
sudo make enable
sudo systemctl enable --now lake-api.service`

function About() {
  const { sources } = Route.useLoaderData()

  return (
    <main>
      <section className="hero dotgrid">
        <div className="wrap" style={{ padding: 0 }}>
          <p className="eyebrow">About</p>
          <h1 className="hero-title">
            Built to still be debuggable at 3am, two years from now.
          </h1>
          <p className="hero-lead">
            lake is a small, durable data lake for one server and one NAS. It
            collects public data on a schedule, keeps every raw byte exactly as
            it arrived, and rebuilds a typed, queryable copy from it. Nothing
            here is clever. That is the point.
          </p>
        </div>
      </section>

      <section className="section">
        <div className="wrap" style={{ padding: 0 }}>
          <Reveal>
            <h2 className="section-head">What it guarantees</h2>
          </Reveal>
          <p className="section-lead">
            Four properties the system holds onto, because each one is what you
            actually want at 3am when something has gone wrong.
          </p>

          <Reveal className="grid-cards">
            {GUARANTEES.map((g) => (
              <div className="tile" key={g.title}>
                <h3 className="tile-title" style={{ fontSize: "1.125rem" }}>
                  {g.title}
                </h3>
                <p className="tile-meta">{g.body}</p>
              </div>
            ))}
          </Reveal>
        </div>
      </section>

      <section className="section section-alt" id="pipeline">
        <div className="wrap" style={{ padding: 0 }}>
          <Reveal>
            <h2 className="section-head">The pipeline</h2>
          </Reveal>
          <p className="section-lead">
            Four stages, each one idempotent. A run that fails halfway leaves
            nothing half-written.
          </p>
          <Pipeline />
        </div>
      </section>

      {sources.length > 0 && (
        <section className="section">
          <div className="wrap" style={{ padding: 0 }}>
            <Reveal>
              <h2 className="section-head">Sources</h2>
            </Reveal>
            <p className="section-lead">
              Adding a source is an edit to one YAML file plus a package that
              knows how to fetch and parse it. Nothing else in the system
              changes — no dispatch table, no scheduler edit.
            </p>

            <Reveal className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Identifier</th>
                    <th>Kind</th>
                    <th>Schedule</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {sources.map((source) => (
                    <tr key={source.source_id}>
                      <td>{source.display_name}</td>
                      <td className="mono" style={{ fontSize: "0.8125rem" }}>
                        {source.source_id}
                      </td>
                      <td>{source.kind}</td>
                      <td>{source.schedule}</td>
                      <td>
                        <span
                          className={`pill ${source.enabled ? "pill-live" : "pill-off"}`}
                        >
                          {source.enabled ? "Active" : "Paused"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Reveal>
          </div>
        </section>
      )}

      <section className="section section-alt" id="deploy">
        <div className="wrap" style={{ padding: 0 }}>
          <Reveal>
            <h2 className="section-head">Run your own</h2>
          </Reveal>
          <p className="section-lead">
            It fits on one small server and one NAS. There is no Docker image
            and no CI pipeline — deployment is a virtualenv and systemd timers,
            on purpose.
          </p>

          <Reveal className="code-group">
            <div>
              <p className="code-caption">Locally</p>
              <pre className="code">
                <code>{LOCAL}</code>
              </pre>
            </div>
            <div>
              <p className="code-caption">On a server</p>
              <pre className="code">
                <code>{SERVER}</code>
              </pre>
            </div>
          </Reveal>

          <p className="muted mt-3" style={{ fontSize: "0.875rem" }}>
            The API binds to localhost and carries no authentication of its own
            — put it behind a proxy or a private network.{" "}
            <code className="mono">lake-serve-build.timer</code> rebuilds the
            replica hourly.
          </p>
        </div>
      </section>

      <Cta
        title="Questions about the data?"
        lead="How a number was derived, why a source is paused, what a column means — all fair game."
        action="Contact us"
      />
    </main>
  )
}
