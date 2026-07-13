/**
 * Motion, applied additively.
 *
 * The hidden state (`.will-reveal`) is added by this effect, not by the markup —
 * so the server-rendered HTML is already in its final visible state. If the
 * hydration never lands, or the reader prefers reduced motion, everything is
 * simply there. Nothing important is gated behind an animation.
 */

import { useEffect, useRef } from "react"
import type { ReactNode } from "react"

export function Reveal({
  children,
  className = "",
  as: Tag = "div",
}: {
  children: ReactNode
  className?: string
  as?: "div" | "section" | "figure"
}) {
  const ref = useRef<HTMLElement>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (
      window.matchMedia("(prefers-reduced-motion: reduce)").matches ||
      !("IntersectionObserver" in window)
    ) {
      return
    }

    el.classList.add("will-reveal")
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue
          entry.target.classList.add("is-in")
          observer.unobserve(entry.target)
        }
      },
      { rootMargin: "0px 0px -10% 0px", threshold: 0.05 }
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  return (
    <Tag ref={ref as never} className={className}>
      {children}
    </Tag>
  )
}
