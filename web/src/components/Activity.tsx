import { useApi, repoName } from "@/lib/api"
import {
  Bar, Choice, CiChecks, DataTable, Failed, Loading, PERIODS, Person, Section, Sha, Tag, Time, useDebounced, useQueryState,
  Empty, type Check,
} from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Input } from "@/components/ui/input"

type Push = {
  id: number; repo: string; branch: string; pusher: string; pushed_at: string; forced: number; commits: number
  commit_list: { sha: string; person: string; message: string; additions: number; deletions: number }[]
  checks: Check[]; broke: string[]; reviews: { id: number; findings: number }[]
}
type Commit = {
  repo: string; sha: string; person: string; committed_at: string; message: string
  additions: number; deletions: number; is_merge: number; on_default: number
}
type Breakage = { repo: string; branch: string; check_name: string; broken_sha: string; person: string; pusher: string; broken_at: string; fixed_sha: string | null }

export function Activity() {
  const [repo, setRepo] = useQueryState("repo", "all")
  const [days, setDays] = useQueryState("days", "30")
  const [person, setPerson] = useQueryState("person", "")
  const who = useDebounced(person.trim())
  const q = new URLSearchParams({ days, ...(repo !== "all" && { repo }), ...(who && { person: who }) })
  const data = useApi<{ pushes: Push[]; commits: Commit[] }>(`/api/activity?${q}`)
  const breaks = useApi<Breakage[]>(`/api/breakages?days=${days}`)
  const summary = useApi("/api/summary")
  const repos: string[] = summary.data?.repos ?? []
  const open = (breaks.data || []).filter((b) => !b.fixed_sha)

  return (
    <div className="space-y-6">
      <Bar>
        <Choice label="Repository" value={repo} onChange={setRepo} options={[["all", "All repos"], ...repos.map((r) => [r, repoName(r)] as [string, string])]} />
        <Choice label="Period" value={days} onChange={setDays} options={PERIODS.slice(0, 3)} />
        <Input value={person} onChange={(e) => setPerson(e.target.value)} placeholder="Filter by GitHub login or email" className="h-7 w-64" />
      </Bar>

      {open.length > 0 && (
        <Alert variant="destructive">
          <AlertTitle>{open.length} CI check{open.length > 1 ? "s are" : " is"} broken on a default branch</AlertTitle>
          <AlertDescription>
            <ul className="mt-1 space-y-1">
              {open.map((b) => (
                <li key={b.repo + b.check_name + b.broken_sha}>
                  <b className="font-medium">{b.check_name}</b> in {repoName(b.repo)}@{b.branch}, since <Sha repo={b.repo} sha={b.broken_sha} /> by{" "}
                  <Person name={b.person} />
                  {b.pusher && b.pusher !== b.person && <> (pushed by {b.pusher})</>}, <Time iso={b.broken_at} />
                </li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}

      {data.error && <Failed error={data.error} />}
      <Section flush title="Pushes" description="Who pushed what, and what CI said about the pushed head.">
        {!data.data ? <Loading /> : (
          <DataTable
            rows={data.data.pushes}
            sort="pushed_at"
            empty={<Empty title="No pushes in this period">Pushes come from GitHub once a token or the GitHub App is set. The commits below come straight from git.</Empty>}
            columns={[
              { key: "pushed_at", label: "When", render: (r) => <Time iso={r.pushed_at} /> },
              { key: "pusher", label: "Pusher", render: (r) => <Person name={r.pusher} /> },
              { key: "branch", label: "Where", render: (r) => (
                <span className="flex flex-wrap items-center gap-1">
                  <span className="text-muted-foreground">{repoName(r.repo)}@</span><span className="font-medium">{r.branch}</span>
                  {!!r.forced && <Tag tone="warning">force</Tag>}
                </span>
              ) },
              { key: "commits", label: "Commits", num: true, render: (r) => (
                <details className="group text-left">
                  <summary className="text-right">{r.commits} <span className="text-muted-foreground group-open:hidden">›</span></summary>
                  <ul className="mt-2 min-w-72 space-y-1 text-left">
                    {r.commit_list.map((c) => (
                      <li key={c.sha} className="text-xs">
                        <Sha repo={r.repo} sha={c.sha} /> {c.message} <span className="text-muted-foreground">+{c.additions} −{c.deletions}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              ) },
              { key: "after", label: "CI on head", render: (r) => (
                <div className="flex flex-wrap gap-1"><CiChecks checks={r.checks} />{r.broke.map((b) => <Tag key={b} tone="error">broke {b}</Tag>)}</div>
              ) },
              { key: "reviews", label: "Review", render: (r) => r.reviews.length
                ? r.reviews.map((v) => <a key={v.id} className="link mr-2" href={`/reviews/#r${v.id}`}>{v.findings} findings</a>)
                : <span className="text-muted-foreground">–</span> },
            ]}
          />
        )}
      </Section>

      <Section flush title="Commits" description="Every branch, straight from git. Merges and commits not yet on the default branch are marked.">
        {!data.data ? <Loading /> : (
          <DataTable
            rows={data.data.commits}
            sort="committed_at"
            empty={<Empty title="No commits in this period" />}
            columns={[
              { key: "committed_at", label: "When", render: (r) => <Time iso={r.committed_at} /> },
              { key: "person", label: "Author", render: (r) => <Person name={r.person} /> },
              { key: "repo", label: "Repo", wide: true, render: (r) => <span className="whitespace-nowrap">{repoName(r.repo)}</span> },
              { key: "sha", label: "Commit", render: (r) => <Sha repo={r.repo} sha={r.sha} /> },
              { key: "message", label: "Message", render: (r) => (
                <span>
                  {r.message} {!!r.is_merge && <Tag>merge</Tag>} {!r.on_default && <Tag>branch</Tag>}
                </span>
              ) },
              { key: "additions", label: "+", num: true },
              { key: "deletions", label: "−", num: true },
            ]}
          />
        )}
      </Section>
    </div>
  )
}
