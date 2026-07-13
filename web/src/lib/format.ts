/** Number and date formatting, in one place so a table and a card agree. */

import type { Cell } from "@/lib/types"

const NUM = new Intl.NumberFormat("en-US")
const DEC = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

export const num = (n: number) => NUM.format(n)
export const dec = (n: number) => DEC.format(n)

/** `10 Jul 2026` — day first, month named. Unambiguous in every locale. */
export function day(iso: string | null | undefined): string {
  if (!iso) return "—"
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  })
}

/** `10 Jul` — for a stat tile, where the year is noise. */
export function dayShort(iso: string | null | undefined): string {
  if (!iso) return "—"
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
  })
}

/** `08:57 UTC` — the replica's build time, in the timezone it was recorded in. */
export function timeUtc(iso: string | null | undefined): string {
  if (!iso) return ""
  return `${new Date(iso).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "UTC",
  })} UTC`
}

export function year(iso: string | null | undefined): number | null {
  if (!iso) return null
  return new Date(iso).getUTCFullYear()
}

/** Title Case for a SEKI section heading, which arrives SHOUTED. */
export function titleCase(s: string): string {
  return s
    .toLowerCase()
    .replace(
      /(^|\s|\.)([a-z])/g,
      (_, pre: string, ch: string) => pre + ch.toUpperCase()
    )
}

/** Columns whose integers are labels, not quantities.

    `year` holds 2001. It is a name for a year, and `2,001` is a count of something —
    the separator says "this is a magnitude" about a number that has no magnitude. The
    lake has one such column today; naming it here is smaller than teaching the API to
    send a display type it does not otherwise have. */
const LABEL_COLUMNS = new Set(["year", "row_no"])

/** How one cell of a result table prints. Numbers right-align and go monospace.

    Takes `undefined` as well as `null`: a row shorter than its header would index past
    its end, and a missing cell reads the same as an empty one.

    `column` is optional and only decides grouping — see LABEL_COLUMNS. */
export function cell(
  value: Cell | undefined,
  column?: string
): {
  text: string
  numeric: boolean
  empty: boolean
} {
  if (value === null || value === undefined)
    return { text: "∅", numeric: false, empty: true }
  if (typeof value === "number") {
    if (column && LABEL_COLUMNS.has(column)) {
      return { text: String(value), numeric: true, empty: false }
    }
    return {
      text: Number.isInteger(value) ? num(value) : dec(value),
      numeric: true,
      empty: false,
    }
  }
  return { text: String(value), numeric: false, empty: false }
}
