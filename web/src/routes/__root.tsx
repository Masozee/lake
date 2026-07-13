import {
  HeadContent,
  Scripts,
  createRootRoute,
  useRouterState,
} from "@tanstack/react-router"
import { Footer, Header } from "@/components/shell"
import { THEME_SCRIPT } from "@/components/theme-toggle"

import appCss from "../styles.css?url"

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: "utf-8" },
      { name: "viewport", content: "width=device-width, initial-scale=1" },
      { title: "lake · open data lake" },
      {
        name: "description",
        content:
          "Public data, collected on a schedule and queryable in the browser. Read-only.",
      },
    ],
    links: [{ rel: "stylesheet", href: appCss }],
    scripts: [
      // Sets .dark before first paint. An effect cannot do this: it runs after
      // paint, which is the exact frame the white flash happens in.
      { children: THEME_SCRIPT },
    ],
  }),
  notFoundComponent: () => (
    <main className="wrap page-pad">
      <h1 className="page-title">404</h1>
      <p className="page-sub">That page is not here. Try the datasets.</p>
    </main>
  ),
  shellComponent: RootDocument,
})

function RootDocument({ children }: { children: React.ReactNode }) {
  // The admin panel brings its own chrome — a full-height sidebar that owns the
  // viewport. Painting the public header and footer around it would box the rail
  // in and give the reader two navigations for one page.
  const isAdmin = useRouterState({
    select: (s) => s.location.pathname.startsWith("/admin"),
  })

  return (
    // THEME_SCRIPT adds `.dark` to this element before React hydrates, so the
    // client's <html> deliberately differs from the server's. Say so: otherwise
    // React logs a mismatch it explicitly refuses to patch up, on every load.
    <html lang="en" suppressHydrationWarning>
      <head>
        <HeadContent />
      </head>
      <body>
        {isAdmin ? (
          children
        ) : (
          <>
            <Header />
            {children}
            <Footer />
          </>
        )}
        <Scripts />
      </body>
    </html>
  )
}
