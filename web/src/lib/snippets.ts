/**
 * The same request, in the four languages someone actually holds this data in.
 *
 * A dataset page that shows you the data and stops has done half a job: the reader
 * still has to work out how to get the rows into the thing they were going to do with
 * them. These are meant to be copied, pasted, and run — so they carry the real URL of
 * the API that served the page, and they end where the reader's own work begins (a
 * DataFrame, an array, a tibble), not at a raw HTTP response.
 *
 * ## The id is what makes these short enough to paste
 *
 * The API takes no SQL, and it takes no keys either — it takes the same id the page is
 * addressed by. So a snippet is a URL a person can actually read:
 *
 *     pd.read_csv("https://lake.example.org/api/data/i5demefo/rows?format=csv")
 *
 * The alternative spells the keys out —
 * `?dataset_id=seki_indicators&group_id=I.1.&series=Uang+Beredar+Luas%28M2%29` — which
 * is correct, unreadable, and impossible to cite. And the escaping problem that used to
 * dominate this file (SQL carrying newlines and apostrophes through four quoting
 * regimes) is gone twice over: once because there is no SQL, and again because an id is
 * eight characters of lowercase base32 with nothing in it to escape.
 *
 * ## Why `?format=csv` and not `Accept: text/csv`
 *
 * Both work — the format is a property of the request, and Accept is the REST way to
 * state it. But `pandas.read_csv(url)` **cannot send a header**, and under Accept-only
 * negotiation it would get JSON and parse it as CSV *without raising*: an empty frame
 * whose one column name is a blob of JSON. R's `read.csv` fails the same way. So the
 * snippets use the param, which is the thing that works from inside a one-liner.
 *
 * What is left is a choice about what each language actually wants:
 *
 * * **Python and R** get CSV. `read_csv(url)` is one line and lands a DataFrame — for
 *   a researcher pulling a series into pandas, every intermediate step we ask them to
 *   write is a step they can get wrong.
 * * **JavaScript** gets the JSON, because a browser is where you already have `fetch`
 *   and a CSV would need a parser.
 * * **cURL** shows all three, because it is the one client that can set a header, so it
 *   is where showing the REST mechanism costs nothing.
 */

import { exportUrl, rowsUrl } from "./query"
import type { DataQuery } from "./query"

export type Language = "curl" | "python" | "javascript" | "r"

export const LANGUAGES: Array<{ id: Language; label: string }> = [
  { id: "curl", label: "cURL" },
  { id: "python", label: "Python" },
  { id: "javascript", label: "JavaScript" },
  { id: "r", label: "R" },
]

/** A download of everything the filters match, not just the first page.

    No `filename`: the server names the file after the thing itself, so a reader who
    downloads the M2 series gets `M2.csv` rather than another `observations.csv`. */
function csv(query: DataQuery, api: string): string {
  return exportUrl(query, "csv", api)
}

function curl(query: DataQuery, api: string): string {
  // curl is the one client here that can trivially set a header, so it is the one place
  // worth showing that `?format=` and `Accept:` are the same request. Everywhere else,
  // the param is the only thing that works.
  return `# the rows, as JSON
curl -s ${JSON.stringify(rowsUrl(query, api))}

# the whole filtered set, as a spreadsheet (-OJ keeps the server's filename)
curl -sOJ ${JSON.stringify(csv(query, api))}

# same thing, asked for the REST way
curl -s -H 'Accept: text/csv' ${JSON.stringify(rowsUrl(query, api))}`
}

function python(query: DataQuery, api: string): string {
  // A CSV URL straight into pandas. `read_csv` speaks HTTP, so there is no request to
  // make, no JSON to unpack, and no column list to zip back onto the rows.
  return `import pandas as pd

df = pd.read_csv(${JSON.stringify(csv(query, api))})
print(df.head())`
}

function javascript(query: DataQuery, api: string): string {
  return `const r = await fetch(${JSON.stringify(rowsUrl(query, api))})
if (!r.ok) throw new Error(await r.text())
const { columns, rows } = await r.json()

// -> an array of objects
const data = rows.map((row) =>
  Object.fromEntries(columns.map((c, i) => [c, row[i]])),
)
console.table(data.slice(0, 5))`
}

function r(query: DataQuery, api: string): string {
  // Same reasoning as Python: `read.csv` takes a URL, and the alternative — unpacking a
  // list of lists whose columns are of mixed type — was fifteen lines of `rbind`.
  return `df <- read.csv(${JSON.stringify(csv(query, api))})
head(df)`
}

const BUILDERS: Record<Language, (query: DataQuery, api: string) => string> = {
  curl,
  python,
  javascript,
  r,
}

/** The snippet for one language. `api` is the public base URL of the API, which the
    server sends us — it is the one thing a browser cannot work out for itself. */
export function snippet(language: Language, query: DataQuery, api: string): string {
  return BUILDERS[language](query, api)
}
