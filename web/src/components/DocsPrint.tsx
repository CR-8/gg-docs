import { useEffect, useRef, useState } from "react"
import { Printer } from "lucide-react"
import { api, short } from "@/lib/api"
import { Markdown } from "@/components/Markdown"
import { Button } from "@/components/ui/button"
import { Spinner } from "@/components/kibo-ui/spinner"

type Doc = { slug: string; title: string; content: string; head_sha: string; generated_at: string }

/** One document, or a repository's whole set as a book, laid out for the browser's "Save as PDF". */
export function DocsPrint() {
  const q = new URLSearchParams(location.search)
  const name = q.get("repo") || "", slug = q.get("slug")
  const [docs, setDocs] = useState<Doc[] | null>(null)
  const [ready, setReady] = useState(false)
  const [count, setCount] = useState(0)
  const rendered = useRef(0)
  useEffect(() => {
    document.documentElement.classList.remove("dark") // paper is white
    ;(async () => {
      const slugs = slug ? [slug] : ((await api<any[]>("/api/docs")).find((r) => r.name === name)?.docs ?? []).map((d: any) => d.slug)
      setDocs(await Promise.all(slugs.map((s: string) => api<Doc>(`/api/docs/${name}/${s}`))))
    })()
  }, [])
  const done = () => {
    rendered.current += 1
    setCount(rendered.current)
    if (docs && rendered.current >= docs.length && !ready) {
      setReady(true)
      setTimeout(() => window.print(), 400)
    }
  }
  return (
    <div className="mx-auto w-full max-w-3xl">
      <div data-print="hide" className="bg-muted mb-8 flex items-center gap-3 rounded-xl border px-4 py-3 text-sm">
        {ready ? <Printer className="size-4" /> : <Spinner className="size-4" />}
        <span className="flex-1">{ready ? "Ready. In the print dialog choose “Save as PDF”." : `Drawing the diagrams… ${count} of ${docs?.length ?? "…"} documents ready`}</span>
        <Button size="sm" disabled={!ready} onClick={() => window.print()}>Print / Save as PDF</Button>
      </div>
      {docs && !slug && (
        <section className="mb-16 pt-24">
          <p className="text-muted-foreground text-sm">gg engineering documentation</p>
          <h1 className="mt-2 text-4xl font-semibold tracking-tight">{name}</h1>
          <p className="text-muted-foreground mt-3 text-sm">{docs.length} documents · generated from <span className="font-mono">{short(docs[0]?.head_sha)}</span> · {new Date().toLocaleDateString()}</p>
          <ol className="mt-10 space-y-1 text-sm">{docs.map((d) => <li key={d.slug}>{d.title}</li>)}</ol>
        </section>
      )}
      {docs?.map((d, i) => (
        <section key={d.slug} className={i || !slug ? "print-break pt-2" : ""}>
          <Markdown text={d.content} light onRendered={done} />
        </section>
      ))}
    </div>
  )
}
