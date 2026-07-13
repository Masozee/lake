import { createFileRoute } from "@tanstack/react-router"
import { useEffect, useRef, useState } from "react"
import type { AskEvent, ToolResult } from "@/lib/types"

export const Route = createFileRoute("/ask")({
  head: () => ({ meta: [{ title: "Ask AI · lake" }] }),
  component: Ask,
})

const EXAMPLES = [
  "Which countries had the highest GDP in 2024?",
  "How has Indonesia's GDP changed since 2010?",
  "What datasets and columns are available?",
]

/** One thing on screen: what the reader asked, what the agent said, or a tool it
    reached for. The agent's work is shown, not hidden — that is the whole point
    of streaming it. */
type Turn =
  | { kind: "question"; text: string }
  | { kind: "answer"; text: string }
  | { kind: "tool"; name: string; body: string; result?: boolean }
  | { kind: "error"; text: string }

function Ask() {
  const [turns, setTurns] = useState<Array<Turn>>([])
  const [question, setQuestion] = useState("")
  const [busy, setBusy] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    window.scrollTo(0, document.body.scrollHeight)
  }, [turns])

  async function ask(text: string) {
    if (!text.trim() || busy) return
    setTurns((t) => [...t, { kind: "question", text }])
    setQuestion("")
    setBusy(true)

    try {
      const res = await fetch("/api/ai/ask", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: text }),
      })

      if (res.status === 429) {
        setTurns((t) => [
          ...t,
          {
            kind: "error",
            text: "Too many questions just now — wait a moment and try again.",
          },
        ])
        return
      }
      if (!res.body) {
        setTurns((t) => [
          ...t,
          { kind: "error", text: "The server sent no answer." },
        ])
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""
      // Text arrives token by token and belongs to the answer *currently* being
      // written. A tool call ends that answer: whatever comes after it is a new
      // one, so the transcript reads as work, then conclusion.
      let streamingAnswer = false

      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        let split: number
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, split)
          buffer = buffer.slice(split + 2)

          const line = frame.split("\n").find((l) => l.startsWith("data: "))
          if (!line) continue

          let event: AskEvent
          try {
            event = JSON.parse(line.slice(6)) as AskEvent
          } catch {
            continue // a half-written frame is not worth killing the stream over
          }

          if (event.type === "text") {
            const chunk = event.text
            setTurns((t) => {
              const last = t[t.length - 1]
              if (streamingAnswer && last?.kind === "answer") {
                return [
                  ...t.slice(0, -1),
                  { kind: "answer", text: last.text + chunk },
                ]
              }
              return [...t, { kind: "answer", text: chunk }]
            })
            streamingAnswer = true
          } else if (event.type === "tool_call") {
            streamingAnswer = false
            setTurns((t) => [
              ...t,
              { kind: "tool", name: event.tool, body: event.input?.sql ?? "" },
            ])
          } else if (event.type === "tool_result") {
            setTurns((t) => [
              ...t,
              {
                kind: "tool",
                name: `↳ ${event.tool}`,
                body: summarise(event.result),
                result: true,
              },
            ])
          } else if (event.type === "error") {
            setTurns((t) => [...t, { kind: "error", text: event.error }])
          }
        }
      }
    } catch (e) {
      setTurns((t) => [
        ...t,
        { kind: "error", text: `Something went wrong: ${String(e)}` },
      ])
    } finally {
      setBusy(false)
      input.current?.focus()
    }
  }

  return (
    <main className="wrap page-pad">
      <h1 className="page-title">Ask AI</h1>
      <p className="page-sub">
        Ask a question in plain English. The assistant explores the data with
        read-only tools and answers with real numbers. It cannot change anything
        — there is no edit or delete, and the database is read-only.
      </p>

      <div className="chat mb-4">
        {turns.map((turn, i) => {
          if (turn.kind === "question")
            return (
              <div className="msg-user" key={i}>
                {turn.text}
              </div>
            )
          if (turn.kind === "answer")
            return (
              <div className="msg-assistant" key={i}>
                {turn.text}
              </div>
            )
          if (turn.kind === "error")
            return (
              <div className="notice notice-error" key={i} role="alert">
                {turn.text}
              </div>
            )
          return (
            <div className="tool-event" key={i}>
              <span className="tool-name">{turn.name}</span>
              {turn.body && <pre>{turn.body}</pre>}
            </div>
          )
        })}
      </div>

      <form
        className="hstack mb-4"
        onSubmit={(e) => {
          e.preventDefault()
          void ask(question)
        }}
      >
        <input
          ref={input}
          type="text"
          className="mono"
          autoComplete="off"
          placeholder="e.g. Which countries had the highest GDP in 2024?"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          style={{
            flex: 1,
            minHeight: "48px",
            background: "var(--muted)",
            border: 0,
            borderBottom: "1px solid var(--muted-foreground)",
            padding: "11px 16px",
            color: "var(--foreground)",
          }}
        />
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </form>

      <div className="hstack">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            className="btn btn-outline"
            style={{ minHeight: "32px", fontSize: "0.75rem" }}
            disabled={busy}
            onClick={() => void ask(example)}
          >
            {example}
          </button>
        ))}
      </div>
    </main>
  )
}

/** A tool result, small enough to read at a glance. The full result is the
    answer's job, not this line's. */
function summarise(result: ToolResult): string {
  if (!result) return ""
  if (result.error) return `error: ${result.error}`
  if (result.tables) return result.tables.join(", ")
  if (result.rows) {
    const head = (result.columns ?? []).join(" | ")
    const body = result.rows
      .slice(0, 8)
      .map((row) => row.map((c) => (c === null ? "∅" : String(c))).join(" | "))
      .join("\n")
    const more = result.rows.length > 8 ? `\n… ${result.rows.length} rows` : ""
    return `${head}\n${body}${more}`
  }
  return JSON.stringify(result).slice(0, 400)
}
