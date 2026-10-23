import { useEffect, useMemo, useState, type ReactNode } from "react"
import {
  ArrowRight, ArrowUpRight, BookOpen, Download, FileText, FolderTree, GitBranch, GitCommitHorizontal, GitFork, Network,
  OctagonAlert, Plus, ShieldCheck, Upload, Workflow,
} from "lucide-react"
import { useApi, useMe } from "@/lib/api"
import { cn } from "@/lib/utils"
import { SIDES, VIEWS, viewsFor, type Side, type View } from "@/lib/views"
import { Choice, Empty, KpiStrip, Loading, PageAside, Person, Section, Tag, Tile, Time } from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Status, StatusIndicator, StatusLabel } from "@/components/kibo-ui/status"

export const ICONS: Record<View, typeof Network> = {
  graph: Network, tree: FolderTree, callflow: Workflow, deps: GitFork, api: BookOpen, rules: ShieldCheck, report: FileText,
}

type Route = [string, string, string]
type SideData = { ref: string; symbols: number | null; modules: number | null; files: string[]; violations?: number | null }
type OverviewData = {
  built_at: string
  sides: { backend: SideData & { routes: Record<string, Route[]> }; frontend: SideData & { routes: Route[] } }
}
type Ci = { total: number; passed: number; failed: number }
type Summary = {
  repos: string[]; ci: Partial<Record<Side, Ci>>; open_breakages: number; sonar_url: string | null; views: boolean
  worker: { queued: number; pending: string[] }; github: "app" | "token" | null; llm: boolean; llm_model: string
  docs_writing: boolean; sync_error: string | null
}
type RepoLink = { id: string; repo: string; status: string; status_detail: string | null; visibility: string | null; default_branch: string | null; owner: string | null }
type DocSet = { repo: string; name: string; status: { state: string; done?: number; total?: number }; docs: { status: string; error: string | null }[] }
type PersonRow = { person: string; commits?: number; pushes?: number; open_breakages?: number; findings?: number }
type ReviewRow = { id: number; created_at: string; findings: number; critical: number | null; major: number | null; slop: number | null }
type Activity = { pushes: { pushed_at: string; broke: string[] }[]; commits: { committed_at: string }[] }

const ACTIVITY_LIMIT = 500
const STATUS_TONE: Record<string, "success" | "error" | "info"> = { active: "success", revoked: "error" }
const sum = (rows: Record<string, any>[] | undefined, key: string) => rows?.reduce((n, r) => n + (r[key] || 0), 0)

// ---------- architecture views (only for a workspace with a backend/frontend pair) ----------

function CiStatus({ ci }: { ci?: Ci }) {
  if (!ci) return <span className="text-muted-foreground text-sm">…</span>
  const status = ci.failed ? "offline" : ci.total ? "online" : "maintenance"
  return (
    <Status status={status} className="mt-1">
      <StatusIndicator />
      <StatusLabel>{ci.total ? (ci.failed ? `${ci.failed} failing` : `${ci.passed}/${ci.total} passing`) : "no checks yet"}</StatusLabel>
    </Status>
  )
}

function SideCard({ side, data, ci }: { side: Side; data: SideData; ci?: Ci }) {
  const routes = Array.isArray((data as any).routes) ? (data as any).routes.length
    : Object.values((data as any).routes as Record<string, Route[]>).reduce((n, r) => n + r.length, 0)
  const views = viewsFor(side).filter((v) => data.files.includes(VIEWS[v].file))
  return (
    <Section
      title={<span className="flex items-center gap-2">{SIDES[side].name}<Tag>{SIDES[side].stack}</Tag></span>}
      description={<span className="font-mono text-xs">{data.ref}</span>}
    >
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="bg-muted/60 rounded-xl px-3 py-2.5">
          <div className="text-muted-foreground text-xs" title="CI checks on the default branch head">CI</div>
          <CiStatus ci={ci} />
        </div>
        <Tile label="Routes">{routes}</Tile>
        <Tile label="Symbols">{data.symbols?.toLocaleString()}</Tile>
        <Tile label="Modules">{data.modules}</Tile>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {views.map((v) => {
          const Icon = ICONS[v]
          return (
            <a key={v} href={`/views/${side}/${v}/`} className="group hover:bg-muted/60 rounded-xl border p-3 transition-colors">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Icon className="text-muted-foreground size-4" />
                {VIEWS[v].name}
                {v === "rules" && data.violations ? <Tag tone="warning" className="ml-1">{data.violations}</Tag> : null}
                <ArrowUpRight className="text-muted-foreground ml-auto size-3.5 opacity-0 transition-opacity group-hover:opacity-100" />
              </div>
              <p className="text-muted-foreground mt-1 text-xs">{VIEWS[v].desc}</p>
            </a>
          )
        })}
      </div>
    </Section>
  )
}

function Routes({ data }: { data: OverviewData }) {
  const [q, setQ] = useState("")
  const match = (...s: string[]) => !q || s.some((x) => x.toLowerCase().includes(q.toLowerCase()))
  const backend = useMemo(
    () => Object.entries(data.sides.backend.routes)
      .map(([tag, rows]) => [tag, rows.filter((r) => match(tag, ...r))] as const)
      .filter(([, rows]) => rows.length),
    [data, q]
  )
  const frontend = data.sides.frontend.routes.filter((r) => match(...r))
  const row = "grid grid-cols-[4.5rem_1fr] gap-x-3 gap-y-0.5 px-4 py-1.5 text-sm sm:grid-cols-[4.5rem_minmax(0,22rem)_1fr]"
  return (
    <Tabs defaultValue="backend">
      <Section
        flush
        title="Routes"
        description="Every backend route from its OpenAPI spec, every frontend page from the Next.js app folder."
        action={<Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter routes" aria-label="Filter routes" className="h-8 w-44 rounded-full sm:w-56" />}
      >
        <div className="border-b px-4 py-2">
          <TabsList>
            <TabsTrigger value="backend">Backend</TabsTrigger>
            <TabsTrigger value="frontend">Frontend</TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="backend" className="py-2">
          {backend.length ? backend.map(([tag, rows]) => (
            <details key={tag} open={!!q} className="group">
              <summary className="hover:bg-muted/50 flex items-center gap-2 px-4 py-2 text-sm font-medium">
                <span className="text-muted-foreground transition-transform group-open:rotate-90">›</span>
                {tag} <span className="text-muted-foreground font-normal">{rows.length}</span>
              </summary>
              {[...rows].sort((a, b) => a[1].localeCompare(b[1])).map(([m, p, s]) => (
                <div key={m + p} className={row}>
                  <code className="text-muted-foreground font-mono text-xs leading-5">{m}</code>
                  <code className="font-mono text-xs leading-5 break-all">{p}</code>
                  <span className="text-muted-foreground col-start-2 text-xs sm:col-start-3">{s}</span>
                </div>
              ))}
            </details>
          )) : <Empty title="No backend routes">{q ? "Nothing matches the filter." : "The route export hasn't run yet."}</Empty>}
        </TabsContent>
        <TabsContent value="frontend" className="py-2">
          {frontend.length ? frontend.map(([u, k, p]) => (
            <div key={p} className={row}>
              <span className="text-muted-foreground text-xs leading-5">{k}</span>
              <code className="font-mono text-xs leading-5 break-all">{u}</code>
              <code className="text-muted-foreground col-start-2 font-mono text-xs break-all sm:col-start-3">{p}</code>
            </div>
          )) : <Empty title="No frontend routes">{q ? "Nothing matches the filter." : undefined}</Empty>}
        </TabsContent>
      </Section>
    </Tabs>
  )
}

// ---------- the dashboard panels ----------

const Chip = ({ children, href, tone }: { children: ReactNode; href?: string; tone?: "error" }) => {
  const cls = cn("bg-muted/70 inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs", tone === "error" && "bg-destructive/10 text-destructive",
    href && "hover:bg-muted transition-colors")
  return href ? <a href={href} className={cls}>{children}</a> : <span className={cls}>{children}</span>
}

function Repositories({ summary, links, docs, admin }: { summary: Summary; links?: RepoLink[]; docs?: DocSet[]; admin: boolean }) {
  const byRepo = new Map((links ?? []).map((l) => [l.repo, l]))
  const docCount = new Map((docs ?? []).map((d) => [d.repo, d.docs.length]))
  const queue = summary.worker.queued
  return (
    <Section
      grow
      className="lg:col-span-4"
      title={<span className="flex items-center gap-2">Repositories <Tag>{summary.repos.length}</Tag></span>}
      description="What this workspace syncs, reviews and documents."
      action={admin && (
        <Button variant="outline" size="icon-sm" asChild><a href="/new/" aria-label="Connect a repository"><Plus /></a></Button>
      )}
    >
      {summary.repos.length ? (
        <ul className="-my-1 mb-3 divide-y">
          {summary.repos.map((repo) => {
            const [owner, name] = repo.split("/")
            const link = byRepo.get(repo)
            return (
              <li key={repo} className="flex items-center gap-3 py-2.5">
                <span className="bg-muted text-muted-foreground grid size-9 shrink-0 place-items-center rounded-xl font-mono text-xs font-semibold uppercase" aria-hidden>
                  {name.slice(0, 2)}
                </span>
                <div className="min-w-0 flex-1">
                  <a href={`/docs/#${name}/overview`} className="block truncate text-sm font-medium hover:underline">{name}</a>
                  <div className="text-muted-foreground flex min-w-0 items-center gap-1.5 text-xs">
                    <span className="truncate">{owner}</span>
                    {link?.default_branch && <><span aria-hidden>·</span><code className="font-mono">{link.default_branch}</code></>}
                    {docCount.get(repo) ? <><span aria-hidden>·</span><span className="whitespace-nowrap">{docCount.get(repo)} docs</span></> : null}
                  </div>
                </div>
                {link && link.status !== "active" && (
                  <span title={link.status_detail ?? undefined}><Tag tone={STATUS_TONE[link.status] ?? "info"}>{link.status}</Tag></span>
                )}
                <a href={`/activity/?repo=${encodeURIComponent(repo)}`} className="text-muted-foreground hover:text-foreground hover:bg-muted grid size-7 shrink-0 place-items-center rounded-full transition-colors"
                  aria-label={`Activity of ${name}`} title="Activity"><ArrowRight className="size-3.5" /></a>
              </li>
            )
          })}
        </ul>
      ) : (
        <div className="py-4">
          <p className="text-muted-foreground text-sm text-pretty">
            Nothing is connected yet. Pick repositories your GitHub account can reach; each is cloned, synced, reviewed and documented.
          </p>
          {admin ? <Button size="sm" className="mt-3" asChild><a href="/new/"><Plus /> Connect a repository</a></Button>
            : <p className="text-muted-foreground mt-2 text-sm">An owner or admin connects them.</p>}
        </div>
      )}
      <div className="mt-auto flex flex-wrap gap-1.5 border-t pt-3">
        <Chip href="/settings/">{summary.github === "app" ? "GitHub App" : summary.github === "token" ? "Read-only token" : "No GitHub access"}</Chip>
        <Chip href="/settings/">{summary.llm ? <code className="font-mono">{summary.llm_model}</code> : "No language model"}</Chip>
        <Chip href="/settings/">{queue ? `${queue} job${queue > 1 ? "s" : ""} waiting` : "Sync queue idle"}</Chip>
        {summary.docs_writing && <Chip href="/docs/">Writing docs…</Chip>}
      </div>
    </Section>
  )
}

function ReviewRisk({ reviews, open }: { reviews?: ReviewRow[]; open: number }) {
  const total = sum(reviews, "findings") ?? 0
  const critical = sum(reviews, "critical") ?? 0
  const major = sum(reviews, "major") ?? 0
  const pct = (n: number) => `${total ? (n / total) * 100 : 0}%`
  return (
    <Section title="Review risk" description={reviews?.length ? <>Last {reviews.length} reviews, newest <Time iso={reviews[0].created_at} /></> : "From the review bot"}
      action={<Button variant="ghost" size="icon-sm" asChild><a href="/reviews/" aria-label="All reviews"><ArrowUpRight /></a></Button>}>
      {!reviews ? <Loading rows={2} /> : !reviews.length ? (
        <p className="text-muted-foreground py-2 text-sm text-pretty">No reviews yet. They start once a language model is set in <a className="link" href="/settings/">Settings</a> and a PR or a push comes in.</p>
      ) : (
        <>
          <div className="flex items-baseline gap-2">
            <span className={cn("text-[2.5rem] leading-none font-semibold tracking-tight tabular-nums", critical && "text-destructive")}>{critical}</span>
            <span className="text-muted-foreground text-sm">critical of {total} findings</span>
          </div>
          <div className="bg-muted mt-3 flex h-2.5 overflow-hidden rounded-full" role="img"
            aria-label={`${critical} critical, ${major} major and ${total - critical - major} other findings`}>
            <span className="bg-destructive" style={{ width: pct(critical) }} />
            <span className="bg-amber-500" style={{ width: pct(major) }} />
            <span className="hatch flex-1" />
          </div>
          <div className="text-muted-foreground mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
            <span className="flex items-center gap-1.5"><i className="bg-destructive size-2 rounded-full" /> {critical} critical</span>
            <span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-amber-500" /> {major} major</span>
            <span className="flex items-center gap-1.5"><i className="hatch bg-muted size-2 rounded-full ring-1 ring-foreground/20" /> {total - critical - major} other</span>
          </div>
        </>
      )}
      <a href="/activity/" className={cn("mt-4 flex items-center gap-2 rounded-xl px-3 py-2 text-sm transition-colors",
        open ? "bg-destructive/10 text-destructive hover:bg-destructive/15" : "bg-muted/60 hover:bg-muted")}>
        <OctagonAlert className="size-4" />
        {open ? `${open} CI check${open > 1 ? "s" : ""} broken on a default branch` : "No CI check broken on a default branch"}
        <ArrowRight className="ml-auto size-3.5" />
      </a>
    </Section>
  )
}

/** The page's one inverse panel: the architecture report when views exist, else a way into Ask. */
function Spotlight({ data, summary }: { data: OverviewData | null | undefined; summary: Summary }) {
  const round = "bg-inverse-foreground/10 hover:bg-inverse-foreground/20 grid size-11 place-items-center rounded-full transition-colors"
  if (data || (data === undefined && summary.views))  // a workspace with views: the report (its links once they load)
    return (
      <div className="bg-inverse text-inverse-foreground shadow-panel flex flex-col rounded-2xl p-5">
        <div className="flex items-start justify-between gap-3">
          <h2 className="text-[1.6rem] leading-[1.1] font-medium tracking-tight">Architecture<br />report</h2>
          {data && <span className="border-inverse-foreground/25 rounded-full border px-2.5 py-0.5 text-xs whitespace-nowrap">built <Time iso={data.built_at} /></span>}
        </div>
        <p className="text-inverse-foreground/70 mt-2 text-xs text-pretty">Modules, import maps and every route of the backend and frontend, as one Markdown file.</p>
        <div className="mt-auto flex min-h-16 gap-2 pt-5">
          {data && <a href="/site/ARCHITECTURE.md" download className={round} aria-label="Download ARCHITECTURE.md" title="Download ARCHITECTURE.md"><Download className="size-4" /></a>}
          {summary.sonar_url && <a href={summary.sonar_url} target="_blank" rel="noreferrer" className={round} aria-label="Open SonarQube" title="SonarQube"><ArrowUpRight className="size-4" /></a>}
        </div>
      </div>
    )
  return (
    <div className="bg-inverse text-inverse-foreground shadow-panel flex flex-col rounded-2xl p-5">
      <h2 className="text-[1.6rem] leading-[1.1] font-medium tracking-tight">Ask about<br />this workspace</h2>
      <p className="text-inverse-foreground/70 mt-2 text-xs text-pretty">Pushes, CI, reviews and the generated docs, answered from this workspace's own data.</p>
      <div className="mt-auto flex gap-2 pt-5">
        <a href="/chat/" className={round} aria-label="Open Ask" title="Ask"><ArrowRight className="size-4" /></a>
        <a href="/docs/" className={round} aria-label="Open the docs" title="Docs"><BookOpen className="size-4" /></a>
      </div>
    </div>
  )
}

const dayKey = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
const lastDays = (n: number) => Array.from({ length: n }, (_, i) => {
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  d.setDate(d.getDate() - (n - 1 - i))
  return d
})
const niceTop = (max: number) => {
  if (max <= 4) return 4
  const step = 10 ** Math.floor(Math.log10(max)) / 2
  return Math.ceil(max / step) * step
}

/** Per-day bars: hatched neutral, the busiest day in brand teal, a dashed line at the daily average. */
function DailyBars({ days, counts, what }: { days: Date[]; counts: number[]; what: string }) {
  const top = niceTop(Math.max(0, ...counts))
  const peak = counts.indexOf(Math.max(...counts))
  const avg = counts.reduce((a, b) => a + b, 0) / counts.length
  const wide = days.length > 10
  const label = (d: Date) => wide ? String(d.getDate()) : d.toLocaleDateString(undefined, { weekday: "short" })
  const said = days.map((d, i) => `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}: ${counts[i]}`).join(", ")
  return (
    <figure className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2" role="img" aria-label={`${what} per day. ${said}`}>
      <div className="text-muted-foreground flex h-40 flex-col justify-between text-right text-[11px] tabular-nums sm:h-44" aria-hidden>
        <span>{top}</span><span>{top / 2}</span><span>0</span>
      </div>
      <div className="relative h-40 sm:h-44" aria-hidden>
        {avg > 0 && (
          <div className="border-foreground/30 absolute inset-x-0 z-10 border-t border-dashed" style={{ bottom: `${(avg / top) * 100}%` }}>
            <span className="bg-inverse text-inverse-foreground absolute right-0 -translate-y-1/2 rounded-full px-1.5 py-px text-[10px] tabular-nums">avg {avg.toFixed(1)}</span>
          </div>
        )}
        <div className={cn("flex h-full items-end", wide ? "gap-0.5" : "gap-1.5 sm:gap-2.5")}>
          {counts.map((n, i) => (
            <div key={i} className="flex h-full min-w-0 flex-1 items-end" title={`${days[i].toLocaleDateString()}: ${n}`}>
              <div className={cn("w-full rounded-full", n && i === peak ? "bg-brand" : "hatch bg-muted ring-1 ring-foreground/10 ring-inset")}
                style={{ height: n ? `${Math.max(5, (n / top) * 100)}%` : "4px" }} />
            </div>
          ))}
        </div>
      </div>
      <div />
      <div className={cn("text-muted-foreground mt-2 flex text-[11px]", wide ? "gap-0.5" : "gap-1.5 sm:gap-2.5")} aria-hidden>
        {days.map((d, i) => <span key={i} className="min-w-0 flex-1 truncate text-center">{!wide || i % 5 === (days.length - 1) % 5 ? label(d) : ""}</span>)}
      </div>
    </figure>
  )
}

function ActivityPanel({ open }: { open: number }) {
  const [period, setPeriod] = useState("7")
  const [metric, setMetric] = useState<"commits" | "pushes">("commits")
  const a = useApi<Activity>(`/api/activity?days=${period}&limit=${ACTIVITY_LIMIT}`)
  const days = useMemo(() => lastDays(Number(period)), [period])
  const counts = useMemo(() => {
    const items = metric === "commits" ? a.data?.commits.map((c) => c.committed_at) : a.data?.pushes.map((p) => p.pushed_at)
    const by = new Map<string, number>()
    for (const iso of items ?? []) by.set(dayKey(new Date(iso)), (by.get(dayKey(new Date(iso))) ?? 0) + 1)
    return days.map((d) => by.get(dayKey(d)) ?? 0)
  }, [a.data, metric, days])
  const capped = (n?: number) => n === undefined ? undefined : n >= ACTIVITY_LIMIT ? `${ACTIVITY_LIMIT}+` : n
  const broke = a.data?.pushes.filter((p) => p.broke.length).length
  return (
    <Section
      className="md:col-span-2 lg:col-span-5"
      title="Activity"
      description="Every branch of every repository in this workspace."
      action={<Choice label="Period" value={period} onChange={setPeriod} className="w-32" options={[["7", "Last 7 days"], ["30", "Last 30 days"]]} />}
    >
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="What to chart">
        {(["commits", "pushes"] as const).map((m) => (
          <button key={m} type="button" aria-pressed={metric === m} onClick={() => setMetric(m)}
            className={cn("rounded-full border px-3 py-1 text-xs font-medium capitalize transition-colors",
              metric === m ? "bg-primary text-primary-foreground border-transparent" : "hover:bg-muted text-muted-foreground hover:text-foreground")}>
            {m}
          </button>
        ))}
        <a href={`/activity/?days=${period}`} className="text-muted-foreground hover:text-foreground ml-auto text-xs">All activity →</a>
      </div>
      <div className="bg-muted/40 mt-3 rounded-xl p-3 pb-2">
        <div className="mb-3 flex items-center gap-2 text-sm font-medium">
          <i className="bg-brand size-2.5 rounded-sm" aria-hidden /> {metric === "commits" ? "Commits" : "Pushes"} per day
        </div>
        {a.error ? <p className="text-destructive text-sm">{a.error}</p> : !a.data ? <Loading rows={3} /> : (
          <DailyBars days={days} counts={counts} what={metric === "commits" ? "Commits" : "Pushes"} />
        )}
        {a.data && a.data[metric].length >= ACTIVITY_LIMIT && (
          <p className="text-muted-foreground mt-2 text-[11px]">Only the newest {ACTIVITY_LIMIT} {metric} are counted, so earlier days may read low.</p>
        )}
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2">
        <Tile label="Commits">{capped(a.data?.commits.length)}</Tile>
        <Tile label="Pushes">{capped(a.data?.pushes.length)}</Tile>
        <Tile label="Broke CI" title="Pushes whose commits broke a check that passed before"
          className={broke ? "bg-destructive/10 text-destructive" : undefined}>{broke}</Tile>
      </div>
      {open > 0 && <p className="sr-only">{open} CI checks are broken on default branches.</p>}
    </Section>
  )
}

function TeamSignals({ people }: { people?: PersonRow[] }) {
  const rows = (people ?? []).filter((p) => p.commits || p.pushes).slice(0, 6)
  const cell = "px-2 py-2 text-right tabular-nums"
  return (
    <Section
      className="md:col-span-2 lg:col-span-7"
      title="Team signals"
      description="Last 30 days. Operational signals to start a conversation, not a ranking of anyone's work."
      action={<Button variant="ghost" size="icon-sm" asChild><a href="/people/?days=30" aria-label="All people"><ArrowUpRight /></a></Button>}
    >
      {!people ? <Loading rows={4} /> : !rows.length ? (
        <p className="text-muted-foreground py-2 text-sm">No commits or pushes in the last 30 days.</p>
      ) : (
        <div className="-mx-2 overflow-x-auto">
          <table className="w-full min-w-[28rem] text-sm">
            <thead className="text-muted-foreground text-xs">
              <tr>
                <th className="px-2 pb-2 text-left font-normal">Person</th>
                <th className="px-2 pb-2 text-right font-normal">Commits</th>
                <th className="px-2 pb-2 text-right font-normal">Pushes</th>
                <th className="px-2 pb-2 text-right font-normal" title="CI checks they broke that are still broken">Open breakages</th>
                <th className="px-2 pb-2 text-right font-normal" title="Review-bot findings on lines they wrote">Findings</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((p) => (
                <tr key={p.person}>
                  <td className="px-2 py-2">
                    <span className="flex min-w-0 items-center gap-2">
                      <span className="bg-muted grid size-7 shrink-0 place-items-center rounded-full text-xs font-semibold uppercase" aria-hidden>{p.person[0]}</span>
                      <span className="truncate"><Person name={p.person} /></span>
                    </span>
                  </td>
                  <td className={cell}>{p.commits ?? 0}</td>
                  <td className={cell}>{p.pushes ?? 0}</td>
                  <td className={cn(cell, p.open_breakages && "text-destructive font-medium")}>{p.open_breakages ?? 0}</td>
                  <td className={cell}>{p.findings ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  )
}

function Documentation({ docs }: { docs?: DocSet[] }) {
  return (
    <Section
      className="md:col-span-2 lg:col-span-5"
      title="Documentation"
      description="Written and kept current for every repository."
      action={<Button variant="ghost" size="icon-sm" asChild><a href="/docs/" aria-label="Open the docs"><ArrowUpRight /></a></Button>}
    >
      {!docs ? <Loading rows={3} /> : !docs.length ? (
        <p className="text-muted-foreground py-2 text-sm">Documents appear after a repository's first sync.</p>
      ) : (
        <ul className="-my-1 divide-y">
          {docs.map((d) => {
            const written = d.docs.filter((x) => x.status === "written").length
            const failed = d.docs.filter((x) => x.error).length
            const busy = d.status.state !== "idle"
            return (
              <li key={d.repo} className="flex items-center gap-3 py-2.5 text-sm">
                <a href={`/docs/#${d.name}/overview`} className="min-w-0 flex-1 truncate font-medium hover:underline">{d.name}</a>
                {busy ? <Tag tone="info">{d.status.total ? `writing ${d.status.done}/${d.status.total}` : d.status.state}</Tag>
                  : failed ? <Tag tone="error">{failed} failed</Tag> : null}
                <span className="text-muted-foreground text-xs whitespace-nowrap tabular-nums">
                  {d.docs.length ? `${d.docs.length} docs${written < d.docs.length ? `, ${written} with prose` : ""}` : "none yet"}
                </span>
              </li>
            )
          })}
        </ul>
      )}
    </Section>
  )
}

export function Overview() {
  const me = useMe()
  const s = useApi<Summary>("/api/summary").data
  const people = useApi<PersonRow[]>("/api/people?days=30").data
  const reviews = useApi<ReviewRow[]>("/api/reviews?limit=100").data
  const docs = useApi<DocSet[]>("/api/docs").data
  const links = useApi<{ repositories: RepoLink[] }>(me?.workspace ? `/api/v1/workspaces/${me.workspace.id}/repositories` : null).data
  const [data, setData] = useState<OverviewData | null | undefined>()
  useEffect(() => {
    fetch("/site/overview.json").then((r) => (r.ok ? r.json() : null)).then(setData, () => setData(null))
  }, [])

  const kpis = (
    <PageAside>
      <KpiStrip items={[
        { label: "Repositories", icon: GitBranch, value: s?.repos.length, href: "/settings/" },
        { label: "Commits, 30 days", icon: GitCommitHorizontal, value: sum(people, "commits"), title: "Commits that are not merges, every branch", href: "/activity/?days=30" },
        { label: "Pushes, 30 days", icon: Upload, value: sum(people, "pushes"), href: "/activity/?days=30" },
        { label: "Open CI breakages", icon: OctagonAlert, value: s?.open_breakages, tone: s?.open_breakages ? "error" : undefined,
          title: "Checks that passed before and fail on a default branch now", href: "/activity/" },
      ]} />
    </PageAside>
  )
  if (!s) return <>{kpis}<Loading rows={8} /></>

  return (
    <div className="space-y-5">
      {kpis}
      {s.sync_error && (
        <Alert variant="destructive">
          <AlertTitle>The last sync failed</AlertTitle>
          <AlertDescription><pre className="font-mono text-xs whitespace-pre-wrap">{s.sync_error}</pre></AlertDescription>
        </Alert>
      )}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-12 lg:gap-5">
        <Repositories summary={s} links={links?.repositories} docs={docs} admin={!!me?.admin} />
        <div className="grid gap-4 lg:col-span-3 lg:gap-5">
          <ReviewRisk reviews={reviews} open={s.open_breakages} />
          <Spotlight data={data} summary={s} />
        </div>
        <ActivityPanel open={s.open_breakages} />
        <TeamSignals people={people} />
        <Documentation docs={docs} />
      </div>

      {(data || s.views) && (
        <section aria-labelledby="architecture" className="space-y-4 pt-4">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div>
              <h2 id="architecture" className="text-xl font-semibold tracking-tight">Architecture</h2>
              <p className="text-muted-foreground mt-1 text-sm">How the backend and frontend are organised, what CI says about each default branch, and every route.</p>
            </div>
            {data && <span className="text-muted-foreground text-xs">Views built <Time iso={data.built_at} /></span>}
          </div>
          {data === undefined ? <Loading rows={4} /> : !data ? (
            <Section title="Being built">
              <p className="text-muted-foreground text-sm text-pretty">
                The first build is running; this fills in when it finishes, usually in a few minutes. <a className="link" href="/settings/">Settings</a> shows progress.
              </p>
            </Section>
          ) : (
            <>
              <div className="grid gap-4 lg:grid-cols-2 lg:gap-5">
                {(Object.keys(SIDES) as Side[]).map((side) => (
                  <SideCard key={side} side={side} data={data.sides[side]} ci={s.ci?.[side]} />
                ))}
              </div>
              <Routes data={data} />
            </>
          )}
        </section>
      )}
    </div>
  )
}
