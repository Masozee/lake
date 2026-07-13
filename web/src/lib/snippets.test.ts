/**
 * The snippets have to *run*, and the URLs have to point at the right rows.
 *
 * A snippet that looks right and does not run is worse than no snippet: the reader
 * trusts it, pastes it, and loses ten minutes to an error we handed them. The API takes
 * no SQL any more, so the old escaping minefield — apostrophes through four quoting
 * regimes — is gone. What replaces it is encoding: a filter value travels in a query
 * string, and a series name with a `/`, a `(`, or a `&` in it must arrive at the server
 * as the name and not as another parameter.
 *
 * SEKI supplies the adversary. It has series called "Bahan makanan / Food Stuff", and
 * names carrying apostrophes and parentheses, so none of this is hypothetical.
 */

import { describe, expect, it } from "vitest"
import { exportUrl, rowsUrl } from "./query"
import { LANGUAGES, snippet } from "./snippets"
import type { DataQuery } from "./query"
import type { Language } from "./snippets"

const API = "https://lake.example.org"

/** A real read, of the shape every dataset page generates. The id carries the keys —
    `dataset_id=seki_indicators&group_id=I.1.&series=Uang+Beredar+Luas%28M2%29` is what
    it stands in for, and what nobody wants to paste into a paper. */
const QUERY: DataQuery = {
  id: "i5demefo",
  select: ["period", "value", "unit"],
  filters: {},
  sort: "period",
  descending: true,
  limit: 100,
}

/** A filter a reader adds on top of the id, carrying the worst punctuation in the lake:
    an apostrophe, a slash, a paren — and an ampersand, which is the one that would
    silently split the query string in two. */
const NASTY: DataQuery = {
  id: "i5demefo",
  filters: { series: "O'Brien / Food (net) & drink" },
}

const ALL = LANGUAGES.map((l) => l.id)

describe("rowsUrl", () => {
  it("encodes a value that would otherwise break the query string", () => {
    const url = rowsUrl(NASTY, API)

    // The ampersand is the dangerous one: unencoded, `& drink` becomes a second
    // parameter, the API rejects it as an unknown column, and the reader is told their
    // series does not exist.
    expect(url).not.toContain("& drink")
    expect(url).toContain("%26")

    // And it round-trips: what the server parses out is the name we put in.
    const parsed = new URL(url).searchParams
    expect(parsed.get("series")).toBe("O'Brien / Food (net) & drink")
  })

  it("addresses the thing by its id, and carries the projection and the order", () => {
    const url = rowsUrl(QUERY, API)

    expect(url).toContain(`${API}/api/data/i5demefo/rows`)

    const parsed = new URL(url).searchParams
    expect(parsed.get("select")).toBe("period,value,unit")
    expect(parsed.get("sort")).toBe("period")
    expect(parsed.has("desc")).toBe(true)
    expect(parsed.get("limit")).toBe("100")

    // The keys are NOT in the URL. That is the whole point of the id — it stands in for
    // them, so the URL is something a person can read, paste, and cite.
    expect(url).not.toContain("seki_indicators")
    expect(url).not.toContain("group_id")
  })

  it("has nothing in an id that needs escaping", () => {
    // Eight characters of lowercase base32. A series name has parentheses and slashes
    // in it and an id does not, which is why it can sit in a path segment at all.
    expect(rowsUrl({ id: "i5demefo", filters: {} })).toBe(
      "/api/data/i5demefo/rows"
    )
  })
})

describe("exportUrl", () => {
  it("drops the page, because an export is not a page", () => {
    const parsed = new URL(exportUrl(NASTY, "csv", API)).searchParams

    // Carrying the limit over would hand the reader 100 rows of a 3,000-row series and
    // call it the data. The filters are the view; the limit was only the screen.
    expect(parsed.has("limit")).toBe(false)
    expect(parsed.has("offset")).toBe(false)
    // The reader's own filter survives, though — it is part of what they are taking.
    expect(parsed.get("series")).toBe("O'Brien / Food (net) & drink")
  })

  it("is the rows endpoint, asked for as a file", () => {
    // Not `/rows/export.csv`. The rows are one resource and CSV is one way of writing
    // them down — a `.csv` on the end of a path is a filename pretending to be a
    // resource. Same URL as `rowsUrl`, one more param.
    const url = exportUrl(QUERY, "xlsx", API)

    expect(url).toContain(`${API}/api/data/i5demefo/rows?`)
    expect(url).not.toContain("export.")
    expect(new URL(url).searchParams.get("format")).toBe("xlsx")
  })

  it("uses ?format= rather than Accept, because an <a href> cannot set a header", () => {
    // This URL is handed to a download link and pasted into `pd.read_csv`. Neither can
    // negotiate — and under Accept-only both would silently receive JSON.
    expect(new URL(exportUrl(QUERY, "csv", API)).searchParams.get("format")).toBe(
      "csv"
    )
  })

  it("names the file only when asked to", () => {
    // Left alone, the server names it after the thing — a reader downloading M2 gets
    // `M2.csv`, not a fourth `observations.csv`. The *file* keeps its extension even
    // though the URL has lost one; a file on disk should say what it is.
    expect(exportUrl(QUERY, "csv", API)).not.toContain("filename")
    expect(exportUrl(QUERY, "csv", API, "m2")).toContain("filename=m2")
  })
})

describe.each(ALL)("%s", (language: Language) => {
  it("points at the public API, not at a loopback address", () => {
    const code = snippet(language, QUERY, API)

    expect(code).toContain(API)
    // The frontend reaches the API over the loopback; a reader cannot. A snippet that
    // says 127.0.0.1 is a snippet that works on exactly one machine.
    expect(code).not.toContain("127.0.0.1")
  })

  it("asks for this thing, not for the whole lake", () => {
    const code = snippet(language, QUERY, API)

    // The id has to survive into the snippet. Without it the reader runs what we gave
    // them and gets 987,860 rows of everything.
    expect(code).toContain("/api/data/i5demefo/")
  })

  it("carries a filter the reader added on top of the id", () => {
    const code = snippet(language, NASTY, API)

    // A space encodes as `+` in a query string — that is what `URLSearchParams` emits
    // and what every server decodes — so the name is matched around it rather than
    // assuming a particular escape.
    expect(code).toMatch(/Food[+ %20]/)
  })

  it("survives a name full of punctuation", () => {
    const code = snippet(language, NASTY, API)

    // Whatever each language does to the URL, the string it ends up holding must be one
    // token — an unescaped newline or an unbalanced quote is a snippet that will not run.
    expect(code).toBeTruthy()
    expect(code).not.toContain("& drink")
  })
})

describe("python", () => {
  it("ends at a DataFrame, in one line, via ?format=csv", () => {
    // The snippet's job is to get the reader to where their own work starts, and
    // `read_csv` takes a URL — so this is one line with no JSON to unpack.
    //
    // It MUST carry `?format=csv`. `pd.read_csv` cannot send an Accept header, so
    // without the param it would receive JSON and parse it as CSV *without raising* —
    // an empty frame whose one column name is a blob of JSON. This assertion is the
    // guard on that silent corruption.
    const code = snippet("python", QUERY, API)

    expect(code).toContain("pd.read_csv")
    expect(code).toContain("format=csv")
    expect(code).not.toContain("export.csv")
  })
})

describe("javascript", () => {
  it("reads JSON, and checks the response before destructuring it", () => {
    const code = snippet("javascript", QUERY, API)

    expect(code).toContain("/rows?")
    // A 422 naming an unknown column has a JSON body that is not a result set. A snippet
    // that destructures it anyway hands the reader `undefined` and no reason why.
    expect(code).toContain("if (!r.ok)")
  })
})

describe("r", () => {
  it("lands a data frame in one line", () => {
    const code = snippet("r", QUERY, API)

    // Same trap as pandas: `read.csv` cannot negotiate either.
    expect(code).toContain("read.csv")
    expect(code).toContain("format=csv")
  })
})

describe("curl", () => {
  it("shows the JSON, the file, and the REST way to ask for the file", () => {
    const code = snippet("curl", QUERY, API)

    // Someone at a terminal is either piping it somewhere or saving a file, so show
    // both. And curl is the one client here that can trivially set a header — so it is
    // the one place where demonstrating content negotiation costs nothing.
    expect(code).toContain("/rows?")
    expect(code).toContain("format=csv")
    expect(code).toContain("Accept: text/csv")
    expect(code).not.toContain("export.csv")
  })
})
