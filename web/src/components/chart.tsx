/**
 * The hero sparkline. Decorative *and* true: it plots a real series, never a
 * hand-drawn squiggle, and it renders nothing at all when the shape isn't there
 * — so the page degrades to no chart rather than to a lie.
 */

import { Reveal } from "@/components/reveal"
import type { SeriesPoint } from "@/lib/types"

/** The viewbox the path is drawn into. The SVG scales; these numbers don't. */
const WIDTH = 640
const HEIGHT = 160

export type Path = {
  line: string
  area: string
  width: number
  height: number
}

/** Turn (period, value) points into a path. Same math the API uses for its own.

    Points with no value are dropped, not drawn as zero: the World Bank reports 2,681
    country-years it has no number for, and a line through zero would invent a
    collapse that never happened. */
export function toPath(points: Array<SeriesPoint>): Path | null {
  const values = points
    .map((p) => p.value)
    .filter((v): v is number => v !== null)
  if (values.length < 2) return null

  const low = Math.min(...values)
  const high = Math.max(...values)
  const span = high - low || 1
  const step = WIDTH / (values.length - 1)

  const coords = values.map((v, i): [number, number] => [
    +(i * step).toFixed(2),
    +(HEIGHT - ((v - low) / span) * HEIGHT).toFixed(2),
  ])
  const line = "M" + coords.map(([x, y]) => `${x},${y}`).join(" L")

  return {
    line,
    // close the path down to the baseline so it can be filled
    area: `${line} L${WIDTH},${HEIGHT} L0,${HEIGHT} Z`,
    width: WIDTH,
    height: HEIGHT,
  }
}

export function Chart({
  path,
  label,
  caption,
  height = "12rem",
}: {
  path: Path
  label: string
  caption: string
  height?: string
}) {
  return (
    <Reveal as="figure" className="m-0">
      <svg
        className="chart"
        viewBox={`-2 -8 ${path.width + 4} ${path.height + 16}`}
        role="img"
        aria-label={label}
        preserveAspectRatio="none"
        style={{ height }}
      >
        <line
          className="chart-frame"
          x1={0}
          y1={path.height}
          x2={path.width}
          y2={path.height}
        />
        <path className="chart-area" d={path.area} />
        <path className="chart-line" d={path.line} />
        <circle className="chart-cap" cx={path.width} cy={0} r={4} />
      </svg>
      <figcaption className="chart-caption">{caption}</figcaption>
    </Reveal>
  )
}
