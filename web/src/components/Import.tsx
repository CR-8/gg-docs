import { useEffect, useMemo, useState } from "react"
import { ArrowRight, ArrowUpRight, Check, GitBranch, Lock, Search } from "lucide-react"
import { api, post, repoName, useMe } from "@/lib/api"
import { Choice, Empty, Loading, Section, Tag, Time } from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/kibo-ui/spinner"

type Access = "owner" | "organization" | "collaborator"
type Found = {
  repo: string; description: string | null; private: boolean; visibility: string; default_branch: string | null
  pushed_at: string | null; access: Access; connected: boolean; html_url: string
}
type Listing = { repositories: Found[]; total: number; install_url: string | null }
type Result = { repository: string; ok: boolean; repo?: string; error?: string; install_url?: string }
const ACCESS: Record<Access, string> = { owner: "yours", organization: "organization", collaborator: "collaborator" }

/** Every page of the repositories this person's GitHub account reaches through the App. */
function useGitHubRepos(enabled: boolean) {
  const [state, setState] = useState<{ data?: Listing; error?: string }>({})
  const [tick, setTick] = useState(0)
  useEffect(() => {
    if (!enabled) return
    let live = true
    ;(async () => {
      const all: Found[] = []
      for (let page = 1; ; page++) {
        const r = await api<Listing>(`/api/v1/github/repositories?per_page=100&page=${page}`)
        all.push(...r.repositories)
        if (all.length >= r.total || !r.repositories.length) return { ...r, repositories: all }
      }
    })().then((data) => live && setState({ data }), (e) => live && setState({ error: e.message }))
    return () => { live = false }
  }, [enabled, tick])
  return { ...state, reload: () => setTick((n) => n + 1) }
}

/** Paste a GitHub URL, or pick repositories the GitHub App can read; each is cloned into this workspace and kept in
 * sync by webhooks from then on. Right after a workspace is created this is its onboarding step (?onboarding=1). */
export function Import() {
  const me = useMe()
  const onboarding = new URLSearchParams(location.search).has("onboarding")
  const list = useGitHubRepos(!!me?.admin)
  const [url, setUrl] = useState("")
  const [filter, setFilter] = useState("")
  const [access, setAccess] = useState("all")
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [shown, setShown] = useState(50)
  const [busy, setBusy] = useState(false)
  const [results, setResults] = useState<Result[] | null>(null)
  const matches = useMemo(() => {
    const q = filter.trim().toLowerCase()
    return (list.data?.repositories ?? []).filter((r) => (access === "all" || r.access === access) &&
      (!q || r.repo.toLowerCase().includes(q) || (r.description || "").toLowerCase().includes(q)))
  }, [list.data, filter, access])

  const connect = async (repositories: string[]) => {
    setBusy(true)
    try {
      const r = await post<{ results: Result[] }>(`/api/v1/workspaces/${me!.workspace!.id}/repositories`, { repositories })
      setResults(r.results)
      setPicked(new Set())
      list.reload()
    } catch (e: any) {
      setResults([{ repository: repositories.join(", "), ok: false, error: e.message }])
    } finally {
      setBusy(false)
    }
  }
  const toggle = (repo: string) => setPicked((s) => {
    const next = new Set(s)
    next.has(repo) ? next.delete(repo) : next.add(repo)
    return next
  })

  if (me === undefined) return <Loading rows={4} />
  if (!me?.admin)
    return <Empty title="Only owners and admins connect repositories">Ask one of them, or create a workspace of your own.</Empty>
  const connected = results?.filter((r) => r.ok) ?? []
  const failed = results?.filter((r) => !r.ok) ?? []

  return (
    <div className="grid max-w-3xl gap-6">
      {onboarding && (
        <Alert>
          <AlertTitle>{me.workspace?.name} is ready. Which repositories should it follow?</AlertTitle>
          <AlertDescription>
            <p>Pick them below; each is cloned, synced, reviewed and documented. You can add or remove repositories later in Settings.</p>
            <Button size="sm" variant="outline" className="mt-2" asChild><a href="/">Skip for now <ArrowRight /></a></Button>
          </AlertDescription>
        </Alert>
      )}

      {!!connected.length && (
        <Section title={<span className="flex items-center gap-2"><Check className="size-4" /> Connected {connected.length}</span>}>
          <p className="text-muted-foreground max-w-2xl text-sm text-pretty">
            Each is being cloned now, then its history, pull requests and CI results are read. From here on every push and
            pull request arrives by webhook. The documentation is written right after the first sync.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            {connected.map((r) => (
              <Button key={r.repo} size="sm" variant="outline" asChild><a href={`/docs/#${repoName(r.repo)}/overview`}>{r.repo} docs <ArrowRight /></a></Button>
            ))}
            <Button size="sm" asChild><a href="/">Open the workspace</a></Button>
          </div>
        </Section>
      )}
      {!!failed.length && (
        <Alert variant="destructive">
          <AlertTitle>Couldn't connect {failed.length === 1 ? failed[0].repository : `${failed.length} repositories`}</AlertTitle>
          <AlertDescription>
            {failed.map((r) => <p key={r.repository}>{failed.length > 1 && <b>{r.repository}: </b>}{r.error}</p>)}
            {failed.some((r) => r.install_url) && (
              <Button size="sm" variant="outline" className="mt-2" asChild>
                <a href={failed.find((r) => r.install_url)!.install_url} target="_blank" rel="noreferrer">Install the GitHub App <ArrowUpRight /></a>
              </Button>
            )}
          </AlertDescription>
        </Alert>
      )}

      <Section
        flush
        title="Your GitHub repositories"
        description="Every repository your GitHub account can reach through the portal's GitHub App: your own, your organizations', and ones you collaborate on."
        action={
          <div className="flex flex-wrap items-center gap-2">
            <Choice label="Access" value={access} onChange={setAccess} className="w-36"
              options={[["all", "All"], ["owner", "Yours"], ["organization", "Organizations"], ["collaborator", "Collaborator"]]} />
            <div className="relative">
              <Search className="text-muted-foreground absolute top-1/2 left-2 size-3.5 -translate-y-1/2" />
              <Input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Search" aria-label="Search repositories" className="bg-card h-8 w-44 rounded-full pl-7 text-xs" />
            </div>
          </div>
        }
      >
        {list.error ? (
          <p className="text-destructive px-6 py-4 text-sm">{list.error}</p>
        ) : !list.data ? <Loading rows={5} /> : !list.data.install_url ? (
          <Empty title="No GitHub App on this server">Paste a URL below: the server's own GitHub token or git credentials must be able to read it.</Empty>
        ) : (
          <>
            {!matches.length && (
              <Empty title={filter || access !== "all" ? "Nothing matches" : "No repositories yet"}>
                Install the GitHub App on the account or repositories you want, then reload this page.
              </Empty>
            )}
            <div className="divide-y">
              {matches.slice(0, shown).map((r) => (
                <label key={r.repo} className="hover:bg-muted/40 flex cursor-pointer items-start gap-3 px-6 py-2.5 text-sm has-disabled:cursor-default">
                  <input type="checkbox" className="mt-1 size-4 accent-current" disabled={r.connected} checked={r.connected || picked.has(r.repo)}
                    onChange={() => toggle(r.repo)} aria-label={`Select ${r.repo}`} />
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="truncate font-medium">{r.repo}</span>
                      {r.private && <Lock className="text-muted-foreground size-3.5 shrink-0" aria-label="private" />}
                      <Tag>{ACCESS[r.access]}</Tag>
                      {r.default_branch && <span className="text-muted-foreground inline-flex items-center gap-1 font-mono text-xs"><GitBranch className="size-3" />{r.default_branch}</span>}
                    </span>
                    {r.description && <span className="text-muted-foreground mt-0.5 block truncate text-xs">{r.description}</span>}
                  </span>
                  <span className="text-muted-foreground hidden text-xs sm:inline"><Time iso={r.pushed_at} /></span>
                  {r.connected && <Tag tone="success">Connected</Tag>}
                </label>
              ))}
            </div>
            {matches.length > shown && (
              <div className="border-t px-6 py-2"><Button size="sm" variant="ghost" onClick={() => setShown((n) => n + 50)}>Show {Math.min(50, matches.length - shown)} more</Button></div>
            )}
            <div className="flex flex-wrap items-center gap-3 border-t px-6 py-3 text-xs">
              <Button size="sm" disabled={!picked.size || busy} onClick={() => connect([...picked])}>
                {busy && <Spinner className="size-4" />} Connect {picked.size || ""} {picked.size === 1 ? "repository" : "repositories"}
              </Button>
              <span>Missing one? <a className="link" href={list.data.install_url} target="_blank" rel="noreferrer">Adjust the GitHub App's access</a> on GitHub, then reload this page.</span>
            </div>
          </>
        )}
      </Section>

      <Section title="From a URL" description="A GitHub repository: https://github.com/owner/name, git@github.com:owner/name.git or owner/name.">
        <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); connect([url.trim()]) }}>
          <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/owner/repository" className="h-9 min-w-0 flex-1 font-mono text-xs" required />
          <Button type="submit" disabled={busy}>{busy ? <Spinner className="size-4" /> : <GitBranch />} Connect</Button>
        </form>
      </Section>
    </div>
  )
}
