/**
 * A number that counts up once it scrolls into view.
 *
 * It renders its final value on the server, so the correct number is in the HTML
 * from the start — the animation only replaces text that was already right. With
 * no JS, reduced motion, or no IntersectionObserver, the number is just there.
 */

import { useEffect, useRef, useState } from "react"
import { num } from "@/lib/format"

const DURATION_MS = 900

export function CountUp({
  value,
  className,
}: {
  value: number
  className?: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [shown, setShown] = useState(value)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (
      window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
      !("IntersectionObserver" in window)
    ) {
      return
    }

    let frame = 0
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue
          observer.unobserve(entry.target)
          const start = performance.now()
          const tick = (now: number) => {
            const p = Math.min((now - start) / DURATION_MS, 1)
            // easeOutExpo: fast start, gentle landing
            const eased = p === 1 ? 1 : 1 - Math.pow(2, -10 * p)
            setShown(Math.round(value * eased))
            if (p < 1) frame = requestAnimationFrame(tick)
          }
          frame = requestAnimationFrame(tick)
        }
      },
      { threshold: 0.5 }
    )
    observer.observe(el)
    return () => {
      observer.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [value])

  return (
    <div ref={ref} className={className}>
      {num(shown)}
    </div>
  )
}
