import { useEffect, useRef, useState } from "react"
import { ArrowUp, RotateCcw, Square } from "lucide-react"
import { api, useApi } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Spinner } from "@/components/kibo-ui/spinner"
import { Markdown } from "@/components/Markdown"

type Step = { tool: string; input: unknown; output: string }
type Msg = { role: "user" | "assistant"; content: string; steps?: Step[]; error?: boolean }

const EXAMPLES = [
  "Who pushed to the default branches this week, and what?",
  "Whose push last broke CI, and is it fixed?",
  "Rank people by weighted review findings per 1k lines over 90 days",
  "Which modules does the VMIS service depend on?",
]
const KEY = "portal-chat"

export function Ask() {
  const summary = useApi("/api/summary")
  const [log, setLog] = useState<Msg[]>(() => {
    try {
      return JSON.parse(sessionStorage.getItem(KEY) || "[]")
    } catch {
      return []
    }
  })
  const [text, setText] = useState("")
  const [busy, setBusy] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const stop = useRef<AbortController | null>(null)
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!busy) return
    const started = Date.now()
    const t = setInterval(() => setElapsed(Math.round((Date.now() - started) / 1000)), 1000)
    return () => clearInterval(t)
  }, [busy])
  useEffect(() => {
    try {
      sessionStorage.setItem(KEY, JSON.stringify(log))
    } catch {}
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" })
  }, [log, busy])

  const ask = async (question: string) => {
    question = question.trim()
    if (!question || busy) return
    const history = log.filter((m) => !m.error).map(({ role, content }) => ({ role, content }))
    setLog((l) => [...l, { role: "user", content: question }])
    setText("")
    setElapsed(0)
    setBusy(true)
    summary.reload() // is the docs writer using the model right now?
    stop.current = new AbortController()
    try {
      const r = await api("/api/chat", { method: "POST", body: JSON.stringify({ history, question }), signal: stop.current.signal })
      setLog((l) => [...l, { role: "assistant", content: r.answer, steps: r.steps }])
    } catch (e: any) {
      const text = e.name === "AbortError" ? "Stopped." : /timed? ?out/i.test(e.message)
        ? `The model didn't answer in time (${e.message}). A local CPU model is slow; a hosted model in Settings answers in seconds.`
        : e.message
      setLog((l) => [...l, { role: "assistant", content: text, error: true }])
    }
    setBusy(false)
  }

  const s = summary.data
  return (
    <div className="mx-auto flex max-w-3xl flex-col">
      {!log.length && (
        <div className="grid gap-2 sm:grid-cols-2">
          {EXAMPLES.map((q) => (
            <button key={q} onClick={() => ask(q)} className="hover:bg-muted/50 rounded-xl border p-4 text-left text-sm transition-colors">
              {q}
            </button>
          ))}
        </div>
      )}

      <div className="space-y-6">
        {log.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="bg-muted ml-auto w-fit max-w-[85%] rounded-2xl px-4 py-2.5 text-sm whitespace-pre-wrap">{m.content}</div>
          ) : (
            <div key={i} className="text-sm">
              {m.error ? (
                <p className="text-destructive">{m.content}</p>
              ) : (
                <Markdown text={m.content} />
              )}
              {!!m.steps?.length && (
                <details className="group mt-3">
                  <summary className="text-muted-foreground hover:text-foreground text-xs">
                    <span className="inline-block transition-transform group-open:rotate-90">›</span> {m.steps.length} lookup{m.steps.length > 1 ? "s" : ""}
                  </summary>
                  <div className="mt-2 space-y-2">
                    {m.steps.map((st, j) => (
                      <div key={j} className="bg-muted/40 rounded-lg border p-2.5 text-xs">
                        <div><b className="font-medium">{st.tool}</b> <code className="text-muted-foreground font-mono break-all">{JSON.stringify(st.input)}</code></div>
                        <pre className="mt-1.5 max-h-48 overflow-auto font-mono whitespace-pre-wrap">{st.output}</pre>
                      </div>
                    ))}
                  </div>
                </details>
              )}
            </div>
          )
        )}
        {busy && (
          <div className="text-muted-foreground space-y-1 text-sm">
            <div className="flex items-center gap-2">
              <Spinner variant="ellipsis" className="size-5" /> Looking it up… <span className="tabular-nums">{elapsed} s</span>
              <Button variant="ghost" size="xs" onClick={() => stop.current?.abort()}><Square /> Stop</Button>
            </div>
            {elapsed > 20 && s?.docs_writing && (
              <p className="text-xs">The model is also writing documentation right now; it pauses between documents so this question goes first.</p>
            )}
          </div>
        )}
        <div ref={end} />
      </div>

      <div className="bg-background/90 sticky bottom-0 mt-6 pt-2 pb-4 backdrop-blur">
        <form
          onSubmit={(e) => {
            e.preventDefault()
            ask(text)
          }}
          className="focus-within:ring-ring/50 bg-card flex items-end gap-2 rounded-2xl border p-2 shadow-sm focus-within:ring-3"
        >
          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault()
                ask(text)
              }
            }}
            rows={1}
            placeholder="Ask about pushes, CI, reviews, people or the code"
            aria-label="Your question"
            className="max-h-48 min-h-9 resize-none border-0 bg-transparent! shadow-none focus-visible:ring-0"
          />
          <Button size="icon" disabled={busy || !text.trim()} aria-label="Ask">
            <ArrowUp />
          </Button>
        </form>
        <div className="text-muted-foreground mt-2 flex items-center gap-2 px-1 text-xs">
          <span>
            {s ? (s.llm ? <>Answered by <code className="font-mono">{s.llm_model}</code> from the portal's own data.</> : "No language model is set yet: an admin can pick one in Settings.") : " "}
          </span>
          {!!log.length && (
            <Button variant="ghost" size="xs" className="ml-auto" onClick={() => setLog([])} disabled={busy}>
              <RotateCcw /> New chat
            </Button>
          )}
        </div>
      </div>
    </div>
  )
}
