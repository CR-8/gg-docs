import { useEffect, useMemo, useState } from "react"
import {
  BookOpen, Download, FileArchive, FileText, GitBranch, History, OctagonAlert, Printer, RefreshCw, Search, Sparkles,
} from "lucide-react"
import { api, post, short, useApi, useMe, when } from "@/lib/api"
import { cn } from "@/lib/utils"
import { Choice, Empty, Failed, KpiStrip, Loading, PageAside, Section, Tag, Time, useDebounced } from "@/components/common"
import { Markdown, type Heading } from "@/components/Markdown"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Status, StatusIndicator, StatusLabel } from "@/components/kibo-ui/status"
import { Spinner } from "@/components/kibo-ui/spinner"

type DocMeta = {
  slug: string; kind: string; title: string; summary: string; status: "written" | "reference"; error: string | null
  model: string | null; head_sha: string; generated_at: string
}
type RepoDocs = {
  repo: string; name: string; docs: DocMeta[]
  status: { state: string; done?: number; total?: number; current?: string | null; head?: string; queued: number }
}
const KIND_LABEL: Record<string, string> = {
  overview: "Overview", architecture: "Architecture", "data-flow": "Data flow", "system-design": "System design",
  "ml-system": "ML system", api: "API", module: "Component",
}

const route = () => {
  const [name, ...rest] = decodeURIComponent(location.hash.slice(1)).split("/")
  return { name: name || "", slug: rest.join("/") }
}
const busy = (r: RepoDocs) => r.status.state !== "idle" || r.status.queued > 0

function RepoStatus({ r }: { r: RepoDocs }) {
  const s = r.status
  if (busy(r))
    return (
      <Status status="degraded">
        <StatusIndicator />
        <StatusLabel>
          {s.state === "writing" ? `Writing ${Math.min((s.done ?? 0) + 1, s.total ?? 0)} of ${s.total}${s.current ? `: ${s.current}` : ""}`
            : s.state === "idle" ? "Queued" : "Reading the code"}
        </StatusLabel>
      </Status>
    )
  const head = r.docs[0]?.head_sha
  return (
    <Status status={r.docs.length ? "online" : "maintenance"}>
      <StatusIndicator />
      <StatusLabel>{r.docs.length ? <>Up to date at <span className="font-mono">{short(head)}</span></> : "Not generated yet"}</StatusLabel>
    </Status>
  )
}

const dot = (d: DocMeta, current?: string | null) =>
  d.error ? "bg-rose-500" : current === d.title ? "bg-amber-500 animate-pulse" : d.status === "written" ? "bg-emerald-500" : "bg-sky-500"

function Searcher() {
  const [q, setQ] = useState("")
  const query = useDebounced(q.trim(), 250)
  const hits = useApi<{ repo: string; slug: string; title: string; snippet: string }[]>(query ? `/api/docs/search?q=${encodeURIComponent(query)}` : null)
  return (
    <div className="relative">
      <Search className="text-muted-foreground absolute top-3 left-3.5 size-4" />
      <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search every document: a module, a function, an env var…" aria-label="Search the docs"
        className="bg-card shadow-panel h-10 rounded-full pl-10" />
      {query && hits.data && (
        <div className="bg-popover absolute z-30 mt-1 max-h-96 w-full overflow-auto rounded-xl border p-1 shadow-lg">
          {hits.data.length ? hits.data.map((h) => (
            <a key={h.repo + h.slug} href={`#${h.repo.split("/")[1]}/${h.slug}`} onClick={() => setQ("")} className="hover:bg-muted block rounded-lg px-3 py-2">
              <div className="text-sm font-medium">{h.title} <span className="text-muted-foreground text-xs font-normal">{h.repo.split("/")[1]}</span></div>
              <div className="text-muted-foreground mt-0.5 line-clamp-2 text-xs">
                {h.snippet.split("**").map((part, i) => (i % 2 ? <mark key={i} className="bg-amber-500/20 text-foreground rounded px-0.5">{part}</mark> : part))}
              </div>
            </a>
          )) : <p className="text-muted-foreground px-3 py-2 text-sm">No matches.</p>}
        </div>
      )}
    </div>
  )
}

function Home({ repos, admin }: { repos: RepoDocs[]; admin: boolean }) {
  return (
    <div className="space-y-6">
      <Searcher />
      <div className="grid gap-4 md:grid-cols-2">
        {repos.map((r) => {
          const written = r.docs.filter((d) => d.status === "written").length
          const first = r.docs[0]
          return (
            <Section
              key={r.repo}
              title={<span className="flex items-center gap-2"><BookOpen className="size-4" />{r.name}</span>}
              description={<span className="font-mono text-xs">{r.repo}</span>}
              action={<RepoStatus r={r} />}
            >
              {r.docs.length ? (
                <>
                  <p className="text-muted-foreground text-sm">
                    {r.docs.length} documents · {written} written by a model, {r.docs.length - written} reference only · updated <Time iso={first?.generated_at} />
                  </p>
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {r.docs.filter((d) => d.kind !== "module").map((d) => (
                      <a key={d.slug} href={`#${r.name}/${d.slug}`}><Tag tone={d.status === "written" ? "success" : "info"}>{d.title}</Tag></a>
                    ))}
                    <a href={`#${r.name}/${r.docs.find((d) => d.kind === "module")?.slug ?? ""}`}>
                      <Tag>{r.docs.filter((d) => d.kind === "module").length} components</Tag>
                    </a>
                  </div>
                </>
              ) : (
                <p className="text-muted-foreground text-sm">The first documents appear a few seconds after the repository is synced; the written parts follow as the model gets through them.</p>
              )}
              <div className="mt-4 flex flex-wrap gap-2">
                {first && <Button size="sm" asChild><a href={`#${r.name}/${first.slug}`}>Open</a></Button>}
                {first && <Button size="sm" variant="outline" asChild><a href={`/api/docs/${r.name}/zip`}><FileArchive /> Markdown (.zip)</a></Button>}
                {first && <Button size="sm" variant="outline" asChild><a href={`/docs/print/?repo=${r.name}`} target="_blank" rel="noreferrer"><Printer /> PDF book</a></Button>}
                {admin && <Button size="sm" variant="ghost" onClick={() => post("/api/admin/docs", { repo: r.repo })}><RefreshCw /> Rewrite all</Button>}
              </div>
            </Section>
          )
        })}
      </div>
      {!repos.length && (
        <Empty title="No repositories yet">
          <a className="link" href="/new/">Import one</a> and its documentation is written right after the first sync.
        </Empty>
      )}
      <p className="text-muted-foreground text-xs">
        Every repository the portal syncs gets its own set. Admins <a className="link" href="/new/">import repositories</a>.
      </p>
    </div>
  )
}

function Toc({ headings }: { headings: Heading[] }) {
  const [on, setOn] = useState("")
  useEffect(() => {
    const els = headings.map((h) => document.getElementById(h.id)).filter(Boolean) as HTMLElement[]
    const io = new IntersectionObserver((entries) => {
      const top = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0]
      if (top) setOn(top.target.id)
    }, { rootMargin: "-80px 0px -70% 0px" })
    els.forEach((el) => io.observe(el))
    return () => io.disconnect()
  }, [headings])
  if (headings.length < 3) return null
  return (
    <nav className="hidden xl:block" data-print="hide">
      <div className="sticky top-20 max-h-[calc(100svh-6rem)] overflow-y-auto">
        <p className="text-muted-foreground mb-2 text-xs font-medium">On this page</p>
        {headings.map((h) => (
          <button
            key={h.id}
            onClick={() => document.getElementById(h.id)?.scrollIntoView({ behavior: "smooth" })}
            className={cn("block w-full truncate py-1 text-left text-xs transition-colors", h.level === 3 && "pl-3",
              on === h.id ? "text-foreground font-medium" : "text-muted-foreground hover:text-foreground")}
          >
            {h.text}
          </button>
        ))}
      </div>
    </nav>
  )
}

function HistorySheet({ name, slug, open, onOpenChange }: { name: string; slug: string; open: boolean; onOpenChange: (o: boolean) => void }) {
  const versions = useApi<{ id: number; head_sha: string; generated_at: string; model: string | null; size: number }[]>(open ? `/api/docs/${name}/history/${slug}` : null)
  const [vid, setVid] = useState<number | null>(null)
  const diff = useApi<{ diff: string }>(vid ? `/api/docs/${name}/diff/${vid}` : null)
  useEffect(() => setVid(null), [slug])
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full overflow-y-auto data-[side=right]:sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle>History</SheetTitle>
          <SheetDescription>Every version of this document, newest first. Pick one to see what it changed.</SheetDescription>
        </SheetHeader>
        <div className="space-y-4 px-4 pb-6">
          {!versions.data ? <Loading /> : (
            <div className="divide-y rounded-xl border">
              {versions.data.map((v, i) => (
                <button key={v.id} onClick={() => setVid(v.id)} className={cn("hover:bg-muted/50 flex w-full items-center gap-3 px-3 py-2 text-left text-sm", vid === v.id && "bg-muted")}>
                  <span className="font-mono text-xs">{short(v.head_sha)}</span>
                  <span className="text-muted-foreground" title={when(v.generated_at)}><Time iso={v.generated_at} /></span>
                  <span className="text-muted-foreground ml-auto text-xs">{v.model || "reference"}</span>
                  {i === 0 && <Tag tone="success">current</Tag>}
                </button>
              ))}
            </div>
          )}
          {vid && (!diff.data ? <Loading /> : (
            <pre className="bg-muted max-h-[60svh] overflow-auto rounded-xl border p-3 font-mono text-[11px] leading-relaxed">
              {diff.data.diff.split("\n").map((line, i) => (
                <div key={i} className={cn(line.startsWith("+") && !line.startsWith("+++") && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
                  line.startsWith("-") && !line.startsWith("---") && "bg-rose-500/10 text-rose-700 dark:text-rose-300",
                  line.startsWith("@@") && "text-sky-600 dark:text-sky-400")}>{line || " "}</div>
              ))}
            </pre>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  )
}

function Reader({ r, slug, admin }: { r: RepoDocs; slug: string; admin: boolean }) {
  const meta = r.docs.find((d) => d.slug === slug)
  const [doc, setDoc] = useState<(DocMeta & { content: string }) | null | undefined>()
  const [headings, setHeadings] = useState<Heading[]>([])
  const [history, setHistory] = useState(false)
  const [msg, setMsg] = useState("")
  useEffect(() => {
    let live = true
    api(`/api/docs/${r.name}/${slug}`).then((d) => live && setDoc(d), () => live && setDoc(null))
    window.scrollTo({ top: 0 })
    return () => {
      live = false
    }
  }, [r.name, slug, meta?.generated_at]) // a rewritten document reloads by itself

  // relative links between generated documents (overview -> modules/x.md) stay inside the portal
  const fix = useMemo(() => (el: HTMLElement) => {
    const dir = slug.includes("/") ? slug.slice(0, slug.lastIndexOf("/") + 1) : ""
    el.querySelectorAll<HTMLAnchorElement>("a[href]").forEach((a) => {
      const href = a.getAttribute("href")!
      if (/^[a-z]+:|^\/\/|^#/i.test(href)) {
        if (!href.startsWith("#")) (a.target = "_blank"), (a.rel = "noreferrer")
        return
      }
      if (href.endsWith(".md")) a.href = `#${r.name}/${new URL(href, `https://d/${dir}`).pathname.slice(1, -3)}`
    })
  }, [r.name, slug])

  if (doc === undefined) return <Loading rows={10} />
  if (doc === null) return <Empty title="This document isn't there yet">It's written on the next pass over the repository.</Empty>
  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_13rem]">
      <article className="min-w-0">
        <div className="mb-4 flex flex-wrap items-start gap-3" data-print="hide">
          <div className="min-w-0 flex-1">
            <div className="text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
              <Tag tone={doc.status === "written" ? "success" : "info"}>{KIND_LABEL[doc.kind] ?? doc.kind}</Tag>
              <span>{doc.model ? <>written by <code className="font-mono">{doc.model}</code></> : doc.kind === "api" ? "built from the code" : "reference only"}</span>·
              <span className="font-mono">{short(doc.head_sha)}</span>·<span>updated <Time iso={doc.generated_at} /></span>
            </div>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <Button variant="outline" size="sm" asChild><a href={`/api/docs/${r.name}/${slug}?download=1`}><Download /> .md</a></Button>
            <Button variant="outline" size="sm" asChild><a href={`/docs/print/?repo=${r.name}&slug=${encodeURIComponent(slug)}`} target="_blank" rel="noreferrer"><Printer /> PDF</a></Button>
            <Button variant="outline" size="sm" onClick={() => setHistory(true)}><History /> History</Button>
            {admin && (
              <Button variant="ghost" size="sm" onClick={() => post("/api/admin/docs", { repo: r.repo, slug }).then(() => setMsg("Queued"), (e) => setMsg(e.message))}>
                <Sparkles /> Rewrite
              </Button>
            )}
            {msg && <span className="text-muted-foreground self-center text-xs">{msg}</span>}
          </div>
        </div>
        {doc.error && (
          <Alert variant="destructive" className="mb-4" data-print="hide">
            <AlertTitle>The last rewrite failed; this is the previous version</AlertTitle>
            <AlertDescription>{doc.error}</AlertDescription>
          </Alert>
        )}
        <div className="bg-card shadow-panel ring-foreground/[0.06] rounded-2xl px-5 py-6 ring-1 sm:px-8">
          <Markdown text={doc.content} fix={fix} onHeadings={setHeadings} />
        </div>
      </article>
      <Toc headings={headings} />
      <HistorySheet name={r.name} slug={slug} open={history} onOpenChange={setHistory} />
    </div>
  )
}

function Sidebar({ repos, r, slug }: { repos: RepoDocs[]; r: RepoDocs; slug: string }) {
  const [q, setQ] = useState("")
  const match = (d: DocMeta) => !q || d.title.toLowerCase().includes(q.toLowerCase())
  const item = (d: DocMeta) => (
    <a
      key={d.slug}
      href={`#${r.name}/${d.slug}`}
      aria-current={d.slug === slug ? "page" : undefined}
      title={d.summary || d.title}
      className={cn("flex items-center gap-2 rounded-full px-2.5 py-1 text-sm transition-colors", // on the canvas, so card-coloured
        d.slug === slug ? "bg-card shadow-panel text-foreground font-medium" : "text-muted-foreground hover:text-foreground hover:bg-card/60")}
    >
      <span className={cn("size-1.5 shrink-0 rounded-full", dot(d, r.status.current))} />
      <span className="truncate">{d.title}</span>
    </a>
  )
  const system = r.docs.filter((d) => d.kind !== "module" && match(d))
  const modules = r.docs.filter((d) => d.kind === "module" && match(d))
  return (
    <aside className="space-y-4 lg:sticky lg:top-20 lg:max-h-[calc(100svh-6rem)] lg:overflow-y-auto" data-print="hide">
      <a href="#" className="text-muted-foreground hover:text-foreground block px-2 text-xs">← All repositories</a>
      <Choice label="Repository" value={r.name} className="w-full" onChange={(n) => (location.hash = `${n}/${repos.find((x) => x.name === n)?.docs[0]?.slug ?? ""}`)}
        options={repos.map((x) => [x.name, x.name])} />
      <div className="px-2"><RepoStatus r={r} /></div>
      <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter documents" aria-label="Filter documents" className="bg-card h-8 rounded-full" />
      <div>
        <p className="text-muted-foreground mb-1 px-2 text-xs font-medium">System</p>
        {system.map(item)}
      </div>
      <div>
        <p className="text-muted-foreground mb-1 flex justify-between px-2 text-xs font-medium">Components <span>{modules.length}</span></p>
        {modules.map(item)}
      </div>
    </aside>
  )
}

export function Docs() {
  const me = useMe()
  const idx = useApi<RepoDocs[]>("/api/docs")
  const [at, setAt] = useState(route)
  useEffect(() => {
    const on = () => setAt(route())
    addEventListener("hashchange", on)
    return () => removeEventListener("hashchange", on)
  }, [])
  const anyBusy = idx.data?.some(busy)
  useEffect(() => { // live progress while documents are being written, otherwise every minute
    const t = setInterval(idx.reload, anyBusy ? 4000 : 60_000)
    return () => clearInterval(t)
  }, [anyBusy])

  const all = idx.data?.flatMap((x) => x.docs)
  const failed = all?.filter((d) => d.error).length
  const kpis = !idx.error && (
    <PageAside>
      <KpiStrip items={[
        { label: "Repositories", icon: GitBranch, value: idx.data?.length },
        { label: "Documents", icon: FileText, value: all?.length },
        { label: "Written by a model", icon: Sparkles, value: all?.filter((d) => d.status === "written").length,
          title: "The rest are reference pages built from the code alone" },
        { label: "Failed rewrites", icon: OctagonAlert, value: failed, tone: failed ? "error" : undefined,
          title: "Documents whose last rewrite failed; the previous version is still shown" },
      ]} />
    </PageAside>
  )

  if (idx.error) return <Failed error={idx.error} />
  if (!idx.data) return <>{kpis}<Loading rows={6} /></>
  const r = idx.data.find((x) => x.name === at.name)
  if (!r || !at.slug) return <>{kpis}<Home repos={idx.data} admin={!!me?.admin} /></>
  return (
    <div className="grid gap-6 lg:grid-cols-[15rem_minmax(0,1fr)]">
      {kpis}
      <Sidebar repos={idx.data} r={r} slug={at.slug} />
      <Reader r={r} slug={at.slug} admin={!!me?.admin} />
    </div>
  )
}
