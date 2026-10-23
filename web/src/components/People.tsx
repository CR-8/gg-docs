import { useEffect, useState } from "react"
import { GitCommitHorizontal, OctagonAlert, Upload, Users } from "lucide-react"
import { useApi, repoName } from "@/lib/api"
import {
  Bar, Choice, DataTable, Empty, Failed, KpiStrip, Loading, PageAside, PERIODS, Pr, Section, Severity, Sha, Tag, Time,
  useQueryState,
} from "@/components/common"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"

type Row = Record<string, any> & { person: string }

const fromHash = () => decodeURIComponent(location.hash.slice(1))

function Detail({ person, days }: { person: string; days: string }) {
  const d = useApi(`/api/people/${encodeURIComponent(person)}?days=${days}`)
  if (d.error) return <Failed error={d.error} />
  if (!d.data) return <Loading />
  const { breakages, findings, commits } = d.data
  const h = "text-sm font-medium mb-2"
  return (
    <div className="space-y-6 px-4 pb-6">
      <div>
        <h3 className={h}>Broke CI ({breakages.length})</h3>
        {breakages.length ? (
          <ul className="space-y-1.5 text-sm">
            {breakages.map((b: any) => (
              <li key={b.id} className="flex flex-wrap items-center gap-1.5">
                <Tag tone={b.fixed_sha ? "success" : "error"}>{b.fixed_sha ? "fixed" : "open"}</Tag>
                <b className="font-medium">{b.check_name}</b> at <Sha repo={b.repo} sha={b.broken_sha} />
                <span className="text-muted-foreground"><Time iso={b.broken_at} /></span>
              </li>
            ))}
          </ul>
        ) : <p className="text-muted-foreground text-sm">None in this period.</p>}
      </div>
      <div>
        <h3 className={h}>Review findings on their lines ({findings.length})</h3>
        {findings.length ? (
          <ul className="space-y-3">
            {findings.map((f: any) => (
              <li key={f.id} className="text-sm">
                <div className="flex flex-wrap items-center gap-1.5">
                  <Severity s={f.severity} /> <Tag>{f.category}</Tag>
                  <a className="link font-medium" href={`/reviews/#r${f.review_id}`}>{f.title}</a>
                </div>
                <div className="text-muted-foreground mt-0.5 text-xs">
                  <code className="font-mono">{f.path}:{f.line}</code> · {repoName(f.repo)} {f.pr ? <Pr repo={f.repo} n={f.pr} /> : f.branch}
                </div>
              </li>
            ))}
          </ul>
        ) : <p className="text-muted-foreground text-sm">None.</p>}
      </div>
      <div>
        <h3 className={h}>Commits ({commits.length})</h3>
        <ul className="space-y-1.5 text-sm">
          {commits.map((c: any) => (
            <li key={c.sha}>
              <Sha repo={c.repo} sha={c.sha} /> {c.message}{" "}
              <span className="text-muted-foreground text-xs">{repoName(c.repo)} +{c.additions} −{c.deletions} · <Time iso={c.committed_at} /></span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}

export function People() {
  const [days, setDays] = useQueryState("days", "90")
  const rows = useApi<Row[]>(`/api/people?days=${days}`)
  const [open, setOpen] = useState(fromHash)
  useEffect(() => {
    const on = () => setOpen(fromHash())
    addEventListener("hashchange", on)
    return () => removeEventListener("hashchange", on)
  }, [])
  const close = () => {
    history.replaceState(null, "", location.pathname + location.search)
    setOpen("")
  }

  const total = (key: string) => rows.data?.reduce((n, r) => n + (r[key] || 0), 0)

  return (
    <div className="space-y-6">
      <PageAside>
        <KpiStrip items={[
          { label: "People", icon: Users, value: rows.data?.length, title: "GitHub logins or git emails with any activity in the period" },
          { label: `Commits, ${days} days`, icon: GitCommitHorizontal, value: total("commits") },
          { label: `Pushes, ${days} days`, icon: Upload, value: total("pushes") },
          { label: "Open breakages", icon: OctagonAlert, value: total("open_breakages"), tone: total("open_breakages") ? "error" : undefined },
        ]} />
      </PageAside>
      <Bar>
        <Choice label="Period" value={days} onChange={setDays} options={PERIODS.slice(1)} />
      </Bar>
      {rows.error && <Failed error={rows.error} />}
      <Section
        flush
        title="People"
        description="Signals for a conversation, not a verdict: whoever works in the riskiest code collects more findings. Hover a column for what it counts."
      >
        {!rows.data ? <Loading rows={6} /> : (
          <DataTable
            rows={rows.data}
            sort="commits"
            onRow={(r) => (location.hash = encodeURIComponent(r.person))}
            empty={<Empty title="No one yet">People appear once the first sync has read the git history.</Empty>}
            columns={[
              { key: "person", label: "Person", render: (r) => <span className="font-medium">{r.person}</span> },
              { key: "commits", label: "Commits", num: true, title: "Non-merge commits on any branch" },
              { key: "adds", label: "+ lines", num: true, title: "Lines added by those commits" },
              { key: "dels", label: "− lines", num: true, wide: true, title: "Lines deleted" },
              { key: "pushes", label: "Pushes", num: true, title: "Pushes made by this GitHub login" },
              { key: "breakages", label: "Broke CI", num: true, title: "Default-branch commits where a check that passed before failed",
                render: (r) => r.breakages ? <span className="text-rose-600 dark:text-rose-400">{r.breakages}{r.open_breakages ? ` (${r.open_breakages} open)` : ""}</span> : "" },
              { key: "findings", label: "Findings", num: true, title: "Review findings on lines this person wrote (git blame): critical / major / minor / nit",
                render: (r) => r.findings ? <>{r.findings} <span className="text-muted-foreground text-xs">{r.critical || 0}c {r.major || 0}M {r.minor || 0}m {r.nit || 0}n</span></> : "" },
              { key: "slop", label: "Slop", num: true, title: "Findings in the slop category: works, but shouldn't be merged as is" },
              { key: "weighted_per_kloc", label: "Per 1k lines", num: true, title: "Findings weighted nit 0.5, minor 1, major 3, critical 8, per 1,000 added lines" },
            ]}
          />
        )}
      </Section>
      <Sheet open={!!open} onOpenChange={(o) => !o && close()}>
        <SheetContent className="w-full overflow-y-auto data-[side=right]:sm:max-w-xl">
          <SheetHeader>
            <SheetTitle>{open}</SheetTitle>
            <SheetDescription>{PERIODS.find(([d]) => d === days)?.[1]}</SheetDescription>
          </SheetHeader>
          {open && <Detail person={open} days={days} />}
        </SheetContent>
      </Sheet>
    </div>
  )
}
