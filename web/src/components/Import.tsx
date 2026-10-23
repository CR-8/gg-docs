import { useMemo, useState } from "react"
import { ArrowRight, ArrowUpRight, Check, GitBranch, Lock, Search } from "lucide-react"
import { post, repoName, useApi, useMe } from "@/lib/api"
import { Empty, Loading, Section, Tag, Time } from "@/components/common"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Spinner } from "@/components/kibo-ui/spinner"

type Found = { repo: string; private: boolean; pushed_at: string | null; imported: boolean }

/** Paste a GitHub URL or pick a repository the GitHub App can read; it is cloned into this workspace and kept in
 * sync by webhooks from then on. */
export function Import() {
  const me = useMe()
  const list = useApi<{ repos: Found[]; install_url: string | null }>(me?.admin ? "/api/import/repos" : null)
  const [url, setUrl] = useState("")
  const [filter, setFilter] = useState("")
  const [busy, setBusy] = useState("")
  const [error, setError] = useState<{ text: string; install?: string } | null>(null)
  const [done, setDone] = useState("")
  const shown = useMemo(
    () => (list.data?.repos ?? []).filter((r) => r.repo.toLowerCase().includes(filter.trim().toLowerCase())).slice(0, 50),
    [list.data, filter]
  )

  const run = async (target: string) => {
    setBusy(target)
    setError(null)
    try {
      const r = await post<{ repo: string }>("/api/import", { url: target })
      setDone(r.repo)
    } catch (e: any) {
      setError({ text: e.message, install: e.data?.install_url })
    } finally {
      setBusy("")
    }
  }

  if (me === undefined) return <Loading rows={4} />
  if (!me?.admin)
    return <Empty title="Only owners and admins import repositories">Ask one of them, or create a workspace of your own.</Empty>
  if (done)
    return (
      <Section title={<span className="flex items-center gap-2"><Check className="size-4" /> Importing {done}</span>}>
        <p className="text-muted-foreground max-w-2xl text-sm text-pretty">
          It is being cloned now, then its history, pull requests and CI results are read. From here on every push and
          pull request arrives by webhook. The documentation is written right after the first sync.
        </p>
        <div className="mt-4 flex flex-wrap gap-2">
          <Button size="sm" asChild><a href={`/docs/#${repoName(done)}/overview`}>Open its docs <ArrowRight /></a></Button>
          <Button size="sm" variant="outline" asChild><a href="/activity/">Activity</a></Button>
          <Button size="sm" variant="ghost" onClick={() => { setDone(""); setUrl(""); list.reload() }}>Import another</Button>
        </div>
      </Section>
    )

  return (
    <div className="grid max-w-3xl gap-6">
      <Section title="From a URL" description="A GitHub repository: https://github.com/owner/name, git@github.com:owner/name.git or owner/name.">
        <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); run(url.trim()) }}>
          <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/owner/repository" className="h-9 min-w-0 flex-1 font-mono text-xs" required />
          <Button type="submit" disabled={!!busy}>{busy && busy === url.trim() ? <Spinner className="size-4" /> : <GitBranch />} Import</Button>
        </form>
        {error && (
          <Alert variant="destructive" className="mt-4">
            <AlertTitle>Couldn't import it</AlertTitle>
            <AlertDescription>
              <p>{error.text}</p>
              {error.install && (
                <Button size="sm" variant="outline" className="mt-2" asChild>
                  <a href={error.install} target="_blank" rel="noreferrer">Install the GitHub App <ArrowUpRight /></a>
                </Button>
              )}
            </AlertDescription>
          </Alert>
        )}
      </Section>

      <Section
        flush
        title="Your GitHub repositories"
        description="Every repository your GitHub account can reach through the portal's GitHub App."
        action={
          <div className="relative">
            <Search className="text-muted-foreground absolute top-1/2 left-2 size-3.5 -translate-y-1/2" />
            <Input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Search" aria-label="Search repositories" className="h-8 w-44 pl-7 text-xs" />
          </div>
        }
      >
        {list.error ? (
          <p className="text-destructive px-6 py-4 text-sm">{list.error}</p>
        ) : !list.data ? <Loading rows={5} /> : !list.data.install_url ? (
          <Empty title="No GitHub App on this server">Paste a URL above: the server's own GitHub token or git credentials must be able to read it.</Empty>
        ) : (
          <>
            {!shown.length && (
              <Empty title={filter ? "Nothing matches" : "No repositories yet"}>Install the GitHub App on the account or repositories you want, then come back.</Empty>
            )}
            <div className="divide-y">
              {shown.map((r) => (
                <div key={r.repo} className="flex items-center gap-2 px-6 py-2.5 text-sm">
                  <span className="truncate font-medium">{r.repo}</span>
                  {r.private && <Lock className="text-muted-foreground size-3.5 shrink-0" aria-label="private" />}
                  <span className="text-muted-foreground hidden text-xs sm:inline"><Time iso={r.pushed_at} /></span>
                  <span className="ml-auto">
                    {r.imported ? <Tag tone="success">Imported</Tag> : (
                      <Button size="sm" variant="outline" disabled={!!busy} onClick={() => run(r.repo)}>
                        {busy === r.repo && <Spinner className="size-4" />} Import
                      </Button>
                    )}
                  </span>
                </div>
              ))}
            </div>
            <div className="border-t px-6 py-3 text-xs">
              Missing one? <a className="link" href={list.data.install_url} target="_blank" rel="noreferrer">Adjust the GitHub App's access</a> on GitHub, then reload this page.
            </div>
          </>
        )}
      </Section>
    </div>
  )
}
