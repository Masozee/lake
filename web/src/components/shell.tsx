/** The chrome every page sits inside: the sticky topbar and the inverted footer. */

import { Link, useRouterState } from "@tanstack/react-router"
import { ThemeToggle } from "@/components/theme-toggle"

const NAV = [
  { to: "/", label: "Overview" },
  { to: "/datasets", label: "Datasets" },
  { to: "/query", label: "Query" },
  { to: "/ask", label: "Ask AI" },
  { to: "/about", label: "About" },
  { to: "/contact", label: "Contact" },
] as const

/** A dataset or table page sits *under* Datasets, so that link stays lit there. */
function isCurrent(to: string, path: string): boolean {
  if (to === "/") return path === "/"
  if (to === "/datasets") {
    return (
      path.startsWith("/datasets") ||
      path.startsWith("/dataset/") ||
      path.startsWith("/table/")
    )
  }
  return path.startsWith(to)
}

export function Header() {
  const path = useRouterState({ select: (s) => s.location.pathname })

  return (
    <header className="topbar">
      <div className="bar">
        <Link to="/" className="brand">
          <span className="logo" role="img" aria-label="lake logo" />
          <span>lake</span>
        </Link>
        <nav className="nav">
          {NAV.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              // Nav links are always the unfiltered, unparameterised page: clicking
              // "Datasets" from a filtered view clears the filters, as it should.
              search={{}}
              className={`btn ${isCurrent(item.to, path) ? "btn-secondary" : "btn-ghost"}`}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="spacer hstack">
          <span className="badge">read-only</span>
          <ThemeToggle />
        </div>
      </div>
    </header>
  )
}

export function Footer() {
  return (
    <footer className="footer">
      <div className="footer-inner">
        <div>
          <div className="footer-brand">
            <span className="logo" role="img" aria-label="lake logo" />
            <h3 style={{ margin: 0 }}>lake</h3>
          </div>
          <p>
            A small, durable data lake for one server and one NAS. Immutable raw
            bytes, a Postgres catalog, and typed Parquet you can query in the
            browser.
          </p>
        </div>
        <div>
          <h3>Explore</h3>
          <ul>
            <li>
              <Link to="/datasets" search={{}}>
                Datasets
              </Link>
            </li>
            <li>
              <Link to="/query" search={{}}>
                SQL query
              </Link>
            </li>
            <li>
              <Link to="/ask">Ask AI</Link>
            </li>
          </ul>
        </div>
        <div>
          <h3>Project</h3>
          <ul>
            <li>
              <Link to="/about">About</Link>
            </li>
            <li>
              <Link to="/contact">Contact</Link>
            </li>
            <li>
              <Link to="/about" hash="deploy">
                Run your own
              </Link>
            </li>
            <li>
              <Link to="/about" hash="pipeline">
                How it works
              </Link>
            </li>
          </ul>
        </div>
      </div>
      <p className="footer-note">
        MIT licensed. Data is served read-only — the API cannot change it.
      </p>
    </footer>
  )
}
