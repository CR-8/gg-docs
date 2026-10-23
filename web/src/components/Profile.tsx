import { useEffect, useMemo } from "react"
import { ArrowLeft, FileDiff, GitCommitHorizontal, GitPullRequest, OctagonAlert } from "lucide-react"
import { cn } from "@/lib/utils"
import { repoName, useApi } from "@/lib/api"
import {
  Bar, Choice, Empty, Failed, KpiStrip, Loading, PageAside, PERIODS, Pr, Section, Severity, Sha, Tag, Tile, Time,
  useQueryState,
} from "@/components/common"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"

type Profile = { person: string; member: boolean; role: string | null; joined_at: string | null; name: string | null; avatar_url: string | null; github: string | null }
type Detail = {
  profile: Profile
  totals: { commits?: number; adds?: number; dels?: number; first?: string; last?: string }
  days: Record<string, number>
  repos: { repo: string; commits: number; adds: number; dels: number }[]
  pushes: number
  commits: any[]
  pulls: any[]
  breakages: any[]
  findings: any[]
}
type Task = { id: string; title: string; status: string; priority: string; dueDate: string | null; blocked: boolean; project: string }
type Tasks = { tasks: Task[]; workflows: number; matched_by: "account" | "name" | null; errors: string[] }

const dayKey = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
const n = (v?: number) => (v ?? 0).toLocaleString()

/** GitHub-style calendar: one column per week, one cell per day, one hue whose strength is the day's commits. */
function Calendar({ days, counts }: { days: number; counts: Record<string, number> }) {
  const cells = useMemo(() => {
    const today = new Date()
    const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - days + 1)
    const first = new Date(start.getFullYear(), start.getMonth(), start.getDate() - start.getDay()) // back to Sunday
    const out: (Date | null)[] = []
    for (let d = first; d <= today; d = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 1)) out.push(d < start ? null : d)
    return out
  }, [days])
  const max = Math.max(1, ...Object.values(counts))
  const level = (c: number) => (c ? Math.ceil((c / max) * 4) : 0)
  const shade = ["bg-muted ring-1 ring-foreground/10 ring-inset", "bg-brand/25", "bg-brand/45", "bg-brand/70", "bg-brand"]
  const weeks = Array.from({ length: Math.ceil(cells.length / 7) }, (_, w) => cells.slice(w * 7, w * 7 + 7))
  const total = Object.values(counts).reduce((a, b) => a + b, 0)
  const active = Object.values(counts).filter(Boolean).length
  return (
    <figure role="img" aria-label={`${total} commits on ${active} of the last ${days} days`}>
      <div className="overflow-x-auto pb-1">
        <div className="grid grid-flow-col gap-[3px]" aria-hidden style={{
          gridTemplateRows: "auto repeat(7, auto)", gridTemplateColumns: `repeat(${weeks.length}, minmax(10px, 1fr))`,
          maxWidth: weeks.length * 25,
        }}>
          {weeks.map((week, w) => {
            // a month's name over the week its 1st falls in; the first column too, unless a 1st follows right away
            const firstOf = (wk: (Date | null)[]) => wk.find((d) => d?.getDate() === 1)
            const month = firstOf(week) ?? (w === 0 && !weeks.slice(1, 3).some(firstOf) ? week.find(Boolean) : undefined)
            return [
              <span key={`m${w}`} className="text-muted-foreground h-4 overflow-visible text-[10px] whitespace-nowrap">
                {month ? month.toLocaleDateString(undefined, { month: "short" }) : ""}
              </span>,
              ...week.map((d, i) => {
                const c = d ? counts[dayKey(d)] ?? 0 : 0
                return d ? (
                  <span key={i} className={cn("aspect-square rounded-[3px]", shade[level(c)])}
                    title={`${d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}: ${c} commit${c === 1 ? "" : "s"}`} />
                ) : <span key={i} className="aspect-square" />
              }),
            ]
          })}
        </div>
      </div>
      <figcaption className="text-muted-foreground mt-2 flex items-center justify-between gap-3 text-[11px]">
        <span>{total.toLocaleString()} commits on {active} day{active === 1 ? "" : "s"}</span>
        <span className="flex items-center gap-1" aria-hidden>
          Less {shade.map((s, i) => <i key={i} className={cn("size-2.5 rounded-[3px]", s)} />)} More
        </span>
      </figcaption>
    </figure>
  )
}

function Repos({ repos }: { repos: Detail["repos"] }) {
  if (!repos.length) return <p className="text-muted-foreground text-sm">No commits in this period.</p>
  const top = repos[0].commits
  return (
    <ul className="space-y-3">
      {repos.slice(0, 8).map((r) => (
        <li key={r.repo} title={`${r.commits} commits, +${r.adds} −${r.dels} lines`}>
          <div className="flex items-baseline justify-between gap-3 text-sm">
            <a className="link truncate font-mono text-xs" href={`https://github.com/${r.repo}`} target="_blank" rel="noreferrer">{repoName(r.repo)}</a>
            <span className="tabular-nums">{n(r.commits)} <span className="text-muted-foreground text-xs">+{n(r.adds)} −{n(r.dels)}</span></span>
          </div>
          <div className="bg-muted mt-1.5 h-2 rounded-full">
            <div className="bg-brand h-2 rounded-full" style={{ width: `${Math.max(3, (r.commits / top) * 100)}%` }} />
          </div>
        </li>
      ))}
    </ul>
  )
}

const STATUS: Record<string, ["success" | "info" | "warning" | "error" | undefined, string]> = {
  DONE: ["success", "Done"], IN_PROGRESS: ["info", "In progress"], BLOCKED: ["error", "Blocked"], TODO: [undefined, "To do"],
}

function GgOpsTasks({ person, name }: { person: string; name: string }) {
  const t = useApi<Tasks>(`/api/people/${encodeURIComponent(person)}/tasks`)
  if (t.error) return <Failed error={t.error} />
  if (!t.data) return <Loading rows={3} />
  const { tasks, workflows, matched_by, errors } = t.data
  if (!workflows) return <Empty title="No GG-Ops projects linked">Start a project from a coding agent (start_project), or link a GG-Ops project under Settings → Integrations.</Empty>
  const count = (s: string) => tasks.filter((x) => x.status === s).length
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Tile label="To do">{count("TODO")}</Tile>
        <Tile label="In progress">{count("IN_PROGRESS")}</Tile>
        <Tile label="Done">{count("DONE")}</Tile>
        <Tile label="Blocked" className={count("BLOCKED") || tasks.some((x) => x.blocked) ? "bg-destructive/10 text-destructive" : undefined}>
          {tasks.filter((x) => x.status === "BLOCKED" || x.blocked).length}
        </Tile>
      </div>
      {errors.length > 0 && <p className="text-muted-foreground text-xs">{errors[0]}</p>}
      {tasks.length ? (
        <ul className="divide-y text-sm">
          {tasks.map((x) => (
            <li key={x.id} className="flex flex-wrap items-center gap-x-2 gap-y-1 py-2">
              <Tag tone={STATUS[x.status]?.[0]}>{STATUS[x.status]?.[1] ?? x.status}</Tag>
              <span className={cn("min-w-0 flex-1", x.status === "DONE" && "text-muted-foreground line-through")}>{x.title}</span>
              <span className="text-muted-foreground text-xs">
                {x.project}{x.dueDate ? ` · due ${new Date(x.dueDate).toLocaleDateString(undefined, { month: "short", day: "numeric" })}` : ""}
                {x.priority === "HIGH" || x.priority === "URGENT" ? ` · ${x.priority.toLowerCase()}` : ""}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-muted-foreground text-sm">
          No tasks assigned to {name} in the {workflows} linked GG-Ops project{workflows === 1 ? "" : "s"}
          {matched_by === "name" ? " (matched by name: they haven't connected their GG-Ops account)" : ""}.
        </p>
      )}
    </div>
  )
}

const PR_TONE: Record<string, "success" | "info" | undefined> = { merged: "success", open: "info" }

export function Profile() {
  const person = new URLSearchParams(location.search).get("p") || ""
  const [days, setDays] = useQueryState("days", "90")
  const d = useApi<Detail>(person ? `/api/people/${encodeURIComponent(person)}?days=${days}` : null)
  const p = d.data?.profile
  const name = p?.name || person
  useEffect(() => {
    const h = document.querySelector("main h1")
    if (h && person) h.textContent = name
    if (person) document.title = `${name} · gg-pulse`
  }, [name, person])

  if (!person) return <Empty title="No one picked">Open someone from <a className="link" href="/people/">People</a>.</Empty>
  const t = d.data?.totals
  const prs = d.data?.pulls
  const period = PERIODS.find(([v]) => v === days)?.[1]?.toLowerCase()
  return (
    <div className="space-y-6">
      <PageAside>
        <KpiStrip items={[
          { label: `Commits, ${days} days`, icon: GitCommitHorizontal, value: d.data && n(t?.commits), title: "Non-merge commits on any branch" },
          { label: "Lines added", icon: FileDiff, value: d.data && `+${n(t?.adds)}`, title: `and ${n(t?.dels)} deleted` },
          { label: "Pull requests", icon: GitPullRequest, value: prs?.length, title: "Opened by them, any time: open, merged or closed" },
          { label: "Broke CI", icon: OctagonAlert, value: d.data?.breakages.length, tone: d.data?.breakages.some((b) => !b.fixed_sha) ? "error" : undefined,
            title: "Default-branch commits of theirs where a check that passed before failed" },
        ]} />
      </PageAside>
      <Bar>
        <a href="/people/" className="text-muted-foreground hover:text-foreground mr-auto inline-flex items-center gap-1 text-sm">
          <ArrowLeft className="size-4" /> People
        </a>
        <Choice label="Period" value={days} onChange={setDays} options={PERIODS} />
      </Bar>
      {d.error && <Failed error={d.error} />}
      {!d.data ? <Loading rows={8} /> : (
        <div className="grid gap-6 lg:grid-cols-12">
          <Section className="lg:col-span-4" title="Profile">
            <div className="flex items-center gap-3">
              <Avatar className="size-14">
                {p?.avatar_url && <AvatarImage src={p.avatar_url} alt="" />}
                <AvatarFallback className="text-lg font-semibold uppercase">{name[0]}</AvatarFallback>
              </Avatar>
              <div className="min-w-0">
                <div className="truncate font-semibold">{name}</div>
                {p?.github ? (
                  <a className="link font-mono text-xs" href={`https://github.com/${p.github}`} target="_blank" rel="noreferrer">@{p.github}</a>
                ) : <span className="text-muted-foreground font-mono text-xs">{person}</span>}
                <div className="mt-1">
                  {p?.member ? <Tag>{p.role}</Tag> : <Tag>not a member</Tag>}
                </div>
              </div>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-2">
              <Tile label="Active days" title="Days with at least one commit">{Object.keys(d.data.days).length}</Tile>
              <Tile label="Repositories">{d.data.repos.length}</Tile>
              <Tile label="Pushes">{d.data.pushes}</Tile>
              <Tile label="Review findings" title="On lines they wrote (git blame)">{d.data.findings.length}</Tile>
            </div>
            <p className="text-muted-foreground mt-3 text-xs">
              {t?.last ? <>Last commit <Time iso={t.last} />{p?.joined_at && <> · joined <Time iso={p.joined_at} /></>}</> : `No commits in the ${period}.`}
            </p>
          </Section>
          <Section className="lg:col-span-8" title="Contributions" description={`Commits per day, ${period}, every branch of every repository.`}>
            <Calendar days={Number(days)} counts={d.data.days} />
          </Section>
          <Section className="lg:col-span-7" title="Tasks in GG-Ops" description="Assigned to them in this workspace's linked GG-Ops projects, read now with your GG-Ops access.">
            <GgOpsTasks person={person} name={name} />
          </Section>
          <Section className="lg:col-span-5" title="Repositories" description="Their commits per repository in the period.">
            <Repos repos={d.data.repos} />
          </Section>
          <Section className="lg:col-span-6" title={`Pull requests (${prs!.length})`}>
            {prs!.length ? (
              <ul className="divide-y text-sm">
                {prs!.map((r) => (
                  <li key={`${r.repo}#${r.number}`} className="flex flex-wrap items-center gap-x-2 gap-y-1 py-2">
                    <Tag tone={PR_TONE[r.state]}>{r.draft && r.state === "open" ? "draft" : r.state}</Tag>
                    <span className="min-w-0 flex-1">{r.title}</span>
                    <span className="text-muted-foreground text-xs">{repoName(r.repo)} <Pr repo={r.repo} n={r.number} /> · <Time iso={r.merged_at || r.updated_at} /></span>
                  </li>
                ))}
              </ul>
            ) : <p className="text-muted-foreground text-sm">None the portal knows of (open PRs and the 50 most recently closed per repository).</p>}
          </Section>
          <Section className="lg:col-span-6" title="CI and reviews" description="Signals for a conversation, not a verdict: whoever works in the riskiest code collects more.">
            <h3 className="mb-2 text-sm font-medium">Broke CI ({d.data.breakages.length})</h3>
            {d.data.breakages.length ? (
              <ul className="mb-5 space-y-1.5 text-sm">
                {d.data.breakages.map((b) => (
                  <li key={b.id} className="flex flex-wrap items-center gap-1.5">
                    <Tag tone={b.fixed_sha ? "success" : "error"}>{b.fixed_sha ? "fixed" : "open"}</Tag>
                    <b className="font-medium">{b.check_name}</b> at <Sha repo={b.repo} sha={b.broken_sha} />
                    <span className="text-muted-foreground"><Time iso={b.broken_at} /></span>
                  </li>
                ))}
              </ul>
            ) : <p className="text-muted-foreground mb-5 text-sm">None.</p>}
            <h3 className="mb-2 text-sm font-medium">Review findings on their lines ({d.data.findings.length})</h3>
            {d.data.findings.length ? (
              <ul className="max-h-96 space-y-3 overflow-y-auto">
                {d.data.findings.map((f) => (
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
          </Section>
          <Section className="lg:col-span-12" title={`Commits (${d.data.commits.length}${d.data.commits.length >= 200 ? "+, newest shown" : ""})`}>
            {d.data.commits.length ? (
              <ul className="divide-y text-sm">
                {d.data.commits.map((c) => (
                  <li key={`${c.repo}${c.sha}`} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 py-1.5">
                    <Sha repo={c.repo} sha={c.sha} />
                    <span className="min-w-0 flex-1">{c.message}</span>
                    <span className="text-muted-foreground text-xs tabular-nums">{repoName(c.repo)} +{c.additions} −{c.deletions} · <Time iso={c.committed_at} /></span>
                  </li>
                ))}
              </ul>
            ) : <p className="text-muted-foreground text-sm">No commits in the {period}.</p>}
          </Section>
        </div>
      )}
    </div>
  )
}
