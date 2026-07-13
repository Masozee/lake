import { Link, createFileRoute } from "@tanstack/react-router"
import { useState } from "react"
import { Reveal } from "@/components/reveal"

export const Route = createFileRoute("/contact")({
  head: () => ({ meta: [{ title: "Contact · lake" }] }),
  component: Contact,
})

const EMAIL = "nurojilukmansyah@gmail.com"

type Result = { ok: true; name: string } | { ok: false; errors: Array<string> }

function Contact() {
  const [sending, setSending] = useState(false)
  const [result, setResult] = useState<Result | null>(null)

  async function send(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (sending) return
    setSending(true)

    const form = new FormData(event.currentTarget)
    const body = {
      name: String(form.get("name") ?? ""),
      email: String(form.get("email") ?? ""),
      message: String(form.get("message") ?? ""),
    }

    const form_ = event.currentTarget
    try {
      const res = await fetch("/api/ui/contact", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      })
      const payload = (await res.json()) as {
        name?: string
        detail?: { errors?: Array<string> }
      }

      if (res.ok) {
        setResult({ ok: true, name: payload.name ?? body.name })
        form_.reset()
      } else {
        // The API validates and returns the reasons; it is the single source of
        // truth for what counts as a usable message.
        setResult({
          ok: false,
          errors: payload.detail?.errors ?? ["That didn't go through."],
        })
      }
    } catch (e) {
      setResult({
        ok: false,
        errors: [`Could not reach the server: ${String(e)}`],
      })
    } finally {
      setSending(false)
    }
  }

  return (
    <main>
      <section className="hero dotgrid">
        <div className="wrap" style={{ padding: 0 }}>
          <p className="eyebrow">Contact</p>
          <h1 className="hero-title">Get in touch.</h1>
          <p className="hero-lead">
            Requests for new sources, corrections to existing data, and
            questions about how a number was derived are all welcome.
          </p>
        </div>
      </section>

      <section className="section">
        <div className="wrap" style={{ padding: 0 }}>
          <div className="hero-grid">
            <Reveal>
              <h2 className="section-head">Send a message</h2>
              <p className="section-lead">
                Tell us which dataset you mean and what you expected to see. If
                you are reporting a wrong number, the query you ran helps
                enormously.
              </p>

              <form className="form" onSubmit={send}>
                <div className="field">
                  <label htmlFor="cf-name">Your name</label>
                  <input
                    id="cf-name"
                    name="name"
                    type="text"
                    required
                    maxLength={120}
                    autoComplete="name"
                  />
                </div>
                <div className="field">
                  <label htmlFor="cf-email">Email</label>
                  <input
                    id="cf-email"
                    name="email"
                    type="email"
                    required
                    maxLength={254}
                    autoComplete="email"
                  />
                </div>
                <div className="field">
                  <label htmlFor="cf-message">Message</label>
                  <textarea
                    id="cf-message"
                    name="message"
                    required
                    minLength={10}
                    maxLength={4000}
                  />
                </div>
                <div className="hstack">
                  <button
                    type="submit"
                    className="btn btn-primary"
                    style={{ minHeight: "48px" }}
                    disabled={sending}
                  >
                    {sending ? "Sending…" : "Send message"}
                  </button>
                </div>
              </form>

              <div className="mt-3" aria-live="polite">
                {result?.ok === false && (
                  <div className="notice notice-error" role="alert">
                    <strong>That didn&apos;t go through.</strong>
                    <ul>
                      {result.errors.map((error) => (
                        <li key={error}>{error}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {result?.ok && (
                  <div className="notice" role="status">
                    <strong>Thanks, {result.name}.</strong>
                    <p style={{ margin: "0.5rem 0 0" }}>
                      Your message was recorded. This form does not send mail on
                      its own — for anything time-sensitive, email{" "}
                      <a href={`mailto:${EMAIL}?subject=lake`}>{EMAIL}</a>{" "}
                      directly.
                    </p>
                  </div>
                )}
              </div>
            </Reveal>

            <div>
              <h2 className="section-head">Other ways</h2>

              <div
                className="grid-cards"
                style={{ gridTemplateColumns: "1fr" }}
              >
                <div className="tile">
                  <h3 className="tile-title" style={{ fontSize: "1rem" }}>
                    Email
                  </h3>
                  <p className="tile-meta">
                    <a href={`mailto:${EMAIL}?subject=lake`}>{EMAIL}</a>
                  </p>
                  <p className="tile-meta mt-3">
                    The most reliable route. Mail is read by a person, not a
                    queue — the form on this page records your message but does
                    not send it.
                  </p>
                </div>
                <div className="tile">
                  <h3 className="tile-title" style={{ fontSize: "1rem" }}>
                    Found a wrong number?
                  </h3>
                  <p className="tile-meta">
                    Include the SQL you ran. Every dataset is queryable at{" "}
                    <Link to="/query" search={{}}>
                      /query
                    </Link>
                    , so a reproducible query is the fastest possible bug
                    report.
                  </p>
                </div>
                <div className="tile">
                  <h3 className="tile-title" style={{ fontSize: "1rem" }}>
                    Want a new source?
                  </h3>
                  <p className="tile-meta">
                    Say where the data lives and how often it changes. Adding a
                    source is a YAML edit plus a small scraper — see{" "}
                    <Link to="/about" hash="deploy">
                      About
                    </Link>
                    .
                  </p>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>
    </main>
  )
}
