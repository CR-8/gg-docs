import { useEffect, useState } from "react"
import { post, repoName, useApi, useMe } from "@/lib/api"
import {
  Bar, Choice, DataTable, Empty, Failed, Loading, Person, Pr, Section, Severity, Sha, Tag, Time,
} from "@/components/common"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"

const fromHash = () => location.hash.match(/^#r(\d+)$/)?.[1] || ""

function ReviewNow() {
  const summary = useApi("/api/summary")
  const repos: string[] = summary.data?.repos ?? []
  const [repo, setRepo] = useState("")
  const [pr, setPr] = useState("")
  const [msg, setMsg] = useState("")
  const run = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await post("/api/admin/review", { repo: repo || repos[0], pr })
      setMsg("Queued. It shows up here when the model is done.")
    } catch (err: any) {
      setMsg(err.message)
    }
  }
  if (!repos.length) return null
  return (
    <form onSubmit={run} className="flex flex-wrap items-center gap-2">
      <Choice label="Repository" value={repo || repos[0]} onChange={setRepo} options={repos.map((r) => [r, repoName(r)])} />
      <Input value={pr} onChange={(e) => setPr(e.target.value.replace(/\D/g, ""))} placeholder="PR number" inputMode="numeric" className="h-7 w-28" required />
      <Button size="sm" disabled={!pr}>Review now</Button>
      <span className="text-muted-foreground text-sm">{msg}</span>
    </form>
  )
}

function Detail({ id }: { id: string }) {
  const r = useApi(`/api/reviews/${id}`)
  if (r.error) return <div className="px-4"><Failed error={r.error} /></div>
  if (!r.data) return <Loading />
  const v = r.data
  return (
    <div className="space-y-5 px-4 pb-6">
      <div className="text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span><Sha repo={v.repo} sha={v.base_sha} /> → <Sha repo={v.repo} sha={v.head_sha} /></span>·<span>{v.model}</span>·<Time iso={v.created_at} />
        {v.posted_url && <>· <a className="link" href={v.posted_url} target="_blank" rel="noreferrer">on GitHub</a></>}
      </div>
      <p className="bg-muted/50 rounded-lg border p-3 text-sm whitespace-pre-wrap">{v.summary || "No summary."}</p>
      {v.findings.length ? (
        <ul className="space-y-3">
          {v.findings.map((f: any) => (
            <li key={f.id} className="rounded-lg border p-3 text-sm">
              <div className="flex flex-wrap items-center gap-1.5">
                <Severity s={f.severity} /> <Tag>{f.category}</Tag>
                <span className="font-medium">{f.title}</span>
              </div>
              <p className="text-muted-foreground mt-2 whitespace-pre-wrap">{f.body}</p>
              <div className="text-muted-foreground mt-2 flex flex-wrap gap-x-3 text-xs">
                <a className="link font-mono" href={`https://github.com/${v.repo}/blob/${v.head_sha}/${f.path}#L${f.line}`} target="_blank" rel="noreferrer">
                  {f.path}:{f.line}
                </a>
                <span>line by <Person name={f.person} /></span>
              </div>
            </li>
          ))}
        </ul>
      ) : <Empty title="No findings">The model found nothing to flag in this diff.</Empty>}
    </div>
  )
}

export function Reviews() {
  const me = useMe()
  const rows = useApi<any[]>("/api/reviews")
  const [open, setOpen] = useState(fromHash)
  useEffect(() => {
    const on = () => setOpen(fromHash())
    addEventListener("hashchange", on)
    return () => removeEventListener("hashchange", on)
  }, [])
  const current = rows.data?.find((r) => String(r.id) === open)
  const close = () => {
    history.replaceState(null, "", location.pathname + location.search)
    setOpen("")
  }

  return (
    <div className="space-y-6">
      {me?.admin && <Bar><ReviewNow /></Bar>}
      {rows.error && <Failed error={rows.error} />}
      <Section flush title="Reviews" description="Every PR update and direct push to a default branch, reviewed on its diff only. Each finding is blamed to whoever wrote the line.">
        {!rows.data ? <Loading rows={6} /> : (
          <DataTable
            rows={rows.data}
            sort="created_at"
            onRow={(r) => (location.hash = `r${r.id}`)}
            empty={<Empty title="No reviews yet">Reviews start once a language model is set in Settings and a PR or a push to a default branch comes in.</Empty>}
            columns={[
              { key: "created_at", label: "When", render: (r) => <Time iso={r.created_at} /> },
              { key: "repo", label: "What", render: (r) => (
                <span>
                  <span className="text-muted-foreground">{repoName(r.repo)} </span>
                  {r.pr ? <Pr repo={r.repo} n={r.pr} /> : <span className="text-muted-foreground">@{r.branch}</span>}{" "}
                  <span className="font-medium">{r.title}</span>
                </span>
              ) },
              { key: "author", label: "Author", wide: true, render: (r) => <Person name={r.author} /> },
              { key: "findings", label: "Findings", num: true, render: (r) => (
                <span className="inline-flex flex-wrap justify-end gap-1">
                  {r.findings}
                  {r.critical ? <Tag tone="error">{r.critical} critical</Tag> : null}
                  {r.major ? <Tag tone="warning">{r.major} major</Tag> : null}
                </span>
              ) },
              { key: "slop", label: "Slop", num: true },
              { key: "posted_url", label: "On GitHub", wide: true, render: (r) => r.posted_url
                ? <a className="link" href={r.posted_url} target="_blank" rel="noreferrer">posted</a>
                : <span className="text-muted-foreground">–</span> },
            ]}
          />
        )}
      </Section>
      <Sheet open={!!open} onOpenChange={(o) => !o && close()}>
        <SheetContent className="w-full overflow-y-auto data-[side=right]:sm:max-w-2xl">
          <SheetHeader>
            <SheetTitle>{current?.title || "Review"}</SheetTitle>
            <SheetDescription>
              {current && <>{repoName(current.repo)} {current.pr ? <Pr repo={current.repo} n={current.pr} /> : `@${current.branch}`}</>}
            </SheetDescription>
          </SheetHeader>
          {open && <Detail id={open} />}
        </SheetContent>
      </Sheet>
    </div>
  )
}
