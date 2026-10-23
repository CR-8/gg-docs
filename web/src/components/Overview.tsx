import { useEffect, useMemo, useState } from "react"
import { ArrowUpRight, BookOpen, Download, FileText, FolderTree, GitFork, Network, ShieldCheck, Workflow } from "lucide-react"
import { useApi } from "@/lib/api"
import { SIDES, VIEWS, viewsFor, type Side, type View } from "@/lib/views"
import { Bar, Empty, Loading, Section, Tag, Time } from "@/components/common"
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

const Stat = ({ label, children }: { label: string; children: React.ReactNode }) => (
  <div className="rounded-lg border px-3 py-2.5">
    <div className="text-muted-foreground text-xs">{label}</div>
    <div className="mt-0.5 text-lg font-semibold tabular-nums">{children ?? "–"}</div>
  </div>
)

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
        <div className="rounded-lg border px-3 py-2.5">
          <div className="text-muted-foreground text-xs" title="CI checks on the default branch head">CI</div>
          <CiStatus ci={ci} />
        </div>
        <Stat label="Routes">{routes}</Stat>
        <Stat label="Symbols">{data.symbols?.toLocaleString()}</Stat>
        <Stat label="Modules">{data.modules}</Stat>
      </div>
      <div className="mt-4 grid gap-2 sm:grid-cols-2">
        {views.map((v) => {
          const Icon = ICONS[v]
          return (
            <a key={v} href={`/views/${side}/${v}/`} className="group hover:bg-muted/50 rounded-lg border p-3 transition-colors">
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
        action={<Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter routes" className="h-8 w-44 sm:w-56" />}
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

export function Overview() {
  const [data, setData] = useState<OverviewData | null | undefined>()
  const summary = useApi("/api/summary")
  useEffect(() => {
    fetch("/site/overview.json").then((r) => (r.ok ? r.json() : null)).then(setData, () => setData(null))
  }, [])
  const s = summary.data

  if (data === undefined || !s) return <Loading rows={6} />
  if (!data && !s.views)
    return (
      <Section title={s.repos.length ? "Your repositories" : "Import your first repository"}>
        {s.repos.length ? (
          <div className="divide-y rounded-lg border">
            {(s.repos as string[]).map((r) => (
              <div key={r} className="flex items-center gap-3 px-3 py-2 text-sm">
                <span className="font-mono text-xs">{r}</span>
                <a className="link ml-auto text-xs" href={`/docs/#${r.split("/")[1]}/overview`}>docs</a>
                <a className="link text-xs" href={`/activity/?repo=${encodeURIComponent(r)}`}>activity</a>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-muted-foreground max-w-2xl text-sm text-pretty">
            Paste a GitHub URL or pick one of your repositories. It is cloned into this workspace, then every push and
            pull request is synced, reviewed and documented.
          </p>
        )}
        <Button size="sm" className="mt-4" asChild><a href="/new/">Import a repository</a></Button>
      </Section>
    )
  if (!data)
    return (
      <Section title="Getting things ready">
        <p className="text-muted-foreground max-w-2xl text-sm text-pretty">
          The first sync and build are running. This page fills in when they finish, usually in a few minutes.{" "}
          <a className="link" href="/settings/">Settings</a> shows progress; <a className="link" href="/activity/">Activity</a> and{" "}
          <a className="link" href="/chat/">Ask</a> work already.
        </p>
        {s?.sync_error && (
          <Alert variant="destructive" className="mt-4">
            <AlertTitle>The last sync failed</AlertTitle>
            <AlertDescription><pre className="font-mono text-xs whitespace-pre-wrap">{s.sync_error}</pre></AlertDescription>
          </Alert>
        )}
      </Section>
    )

  return (
    <div className="space-y-6">
      <Bar>
        <span className="text-muted-foreground text-sm">Views built <Time iso={data.built_at} /></span>
        <div className="ml-auto flex gap-2">
          <Button variant="outline" size="sm" asChild>
            <a href="/site/ARCHITECTURE.md" download><Download /> ARCHITECTURE.md</a>
          </Button>
          {s?.sonar_url && (
            <Button variant="outline" size="sm" asChild>
              <a href={s.sonar_url} target="_blank" rel="noreferrer">SonarQube <ArrowUpRight /></a>
            </Button>
          )}
        </div>
      </Bar>
      <div className="grid gap-6 lg:grid-cols-2">
        {(Object.keys(SIDES) as Side[]).map((side) => (
          <SideCard key={side} side={side} data={data.sides[side]} ci={s?.ci?.[side]} />
        ))}
      </div>
      <Routes data={data} />
    </div>
  )
}
