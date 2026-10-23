import { useState } from "react"
import { GitBranch, KeyRound, Link2, Plus, RefreshCw, Workflow, X } from "lucide-react"
import { post, useApi, type Me } from "@/lib/api"
import { Field, Loading, Note, Section, Snippet, Tag, Time } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

type Tone = "success" | "error" | "warning" | "info"
const TONE: Record<string, Tone> = {
  active: "success", connected: "success", unverified: "info", unknown: "info", degraded: "warning",
  rate_limited: "warning", revoked: "error", unauthorized: "error", not_found: "error", unavailable: "error",
  disconnected: "warning", invalid: "error",
}
const StatusTag = ({ status }: { status: string }) => <Tag tone={TONE[status] ?? "info"}>{status.replace("_", " ")}</Tag>

function useAct(reload: () => void) {
  const [msg, setMsg] = useState("")
  const act = (fn: () => Promise<unknown>, done = "") =>
    fn().then(() => { setMsg(done); reload() }, (e) => setMsg(e.message))
  return [msg, act] as const
}

/** The GitHub account this person signed in with: reconnect it (fresh consent and token), or disconnect it. */
export function GitHubAccount({ me }: { me: NonNullable<Me> }) {
  const disconnect = async () => {
    if (!confirm("Disconnect your GitHub account? The portal's authorization is revoked on GitHub and you are signed out everywhere. Your workspaces and their data stay.")) return
    await post("/api/v1/github/disconnect")
    location.href = "/login/"
  }
  return (
    <Section title="Your GitHub account" description="Signing in with GitHub is how the portal knows who you are; it checks your own access before a repository is connected.">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        {me.avatar_url ? <img src={me.avatar_url} alt="" className="size-8 rounded-full" /> : null}
        <span className="font-medium">{me.login}</span>
        {me.github_id && <span className="text-muted-foreground text-xs">GitHub id {me.github_id}</span>}
        <span className="ml-auto flex gap-2">
          <Button size="sm" variant="outline" asChild><a href="/auth/github?next=/settings/"><RefreshCw /> Reconnect</a></Button>
          <Button size="sm" variant="ghost" onClick={disconnect}>Disconnect</Button>
        </span>
      </div>
    </Section>
  )
}

type GGOpsMe = {
  enabled: boolean; connected: boolean; name?: string | null; email?: string | null; error?: string
  workspaces?: { name: string; slug: string; role: string }[]
}

/** This person's own GG-Ops account: the portal reads GG-Ops as them, so they see what GG-Ops shows them. */
export function GGOpsAccount() {
  const d = useApi<GGOpsMe>("/api/v1/integrations/gg-ops/me")
  const [msg, act] = useAct(d.reload)
  const error = new URLSearchParams(location.search).get("ggops_error")
  const a = d.data
  if (!a?.enabled) return null
  const disconnect = () => confirm("Disconnect GG-Ops? The portal stops reading GG-Ops as you, and the connection is revoked in GG-Ops.") &&
    act(() => post("/api/v1/integrations/gg-ops/me", undefined, "DELETE"), "Disconnected.")
  return (
    <Section title="Your GG-Ops account" description="Read-only, as you: linked GG-Ops projects show only what GG-Ops lets you see.">
      <div className="flex flex-wrap items-center gap-3 text-sm">
        {a.connected ? (
          <>
            <span className="font-medium">{a.name || "Connected"}</span>
            {a.email && <span className="text-muted-foreground text-xs">{a.email}</span>}
            {a.workspaces && <span className="text-muted-foreground text-xs">{a.workspaces.map((w) => w.name).join(", ") || "No GG-Ops workspaces"}</span>}
          </>
        ) : <span className="text-muted-foreground">Not connected</span>}
        <span className="ml-auto flex gap-2">
          {a.connected
            ? <Button size="sm" variant="ghost" onClick={disconnect}>Disconnect</Button>
            : <Button size="sm" asChild><a href="/connect/ggops">Connect GG-Ops</a></Button>}
        </span>
      </div>
      {(msg || a.error || error) && <Note>{msg || a.error || error}</Note>}
    </Section>
  )
}

type Repo = {
  id: string; repo: string; status: string; status_detail: string | null; visibility: string | null
  default_branch: string | null; checked_at: string | null; html_url: string | null
}

/** The workspace's repositories with whether GitHub still lets the portal read each; refresh re-checks them. */
export function ConnectedRepos({ admin, workspace }: { admin: boolean; workspace: number }) {
  const base = `/api/v1/workspaces/${workspace}/repositories`
  const r = useApi<{ repositories: Repo[] }>(base)
  const [msg, act] = useAct(r.reload)
  const [busy, setBusy] = useState(false)
  const refresh = () => { setBusy(true); act(() => post(`${base}/refresh`), "Checked with GitHub.").finally(() => setBusy(false)) }
  return (
    <Section title={<span className="flex items-center gap-2"><GitBranch className="size-4" /> Repositories</span>}
      description="Every repository here is synced (webhooks, plus a poll every few minutes), reviewed and documented."
      action={admin && (
        <span className="flex gap-2">
          <Button size="sm" variant="ghost" onClick={refresh} disabled={busy}><RefreshCw /> Check access</Button>
          <Button size="sm" variant="outline" asChild><a href="/new/"><Plus /> Connect</a></Button>
        </span>
      )}>
      {!r.data ? <Loading rows={2} /> : (
        <div className="space-y-3">
          {r.data.repositories.length ? (
            <div className="divide-y rounded-xl border">
              {r.data.repositories.map((x) => (
                <div key={x.id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                  <span className="font-mono text-xs">{x.repo}</span>
                  <StatusTag status={x.status} />
                  {x.visibility && <span className="text-muted-foreground text-xs">{x.visibility}</span>}
                  {x.default_branch && <span className="text-muted-foreground font-mono text-xs">{x.default_branch}</span>}
                  <span className="ml-auto flex items-center gap-2">
                    {x.checked_at && <span className="text-muted-foreground text-xs">checked <Time iso={x.checked_at} /></span>}
                    <a className="link text-xs" href={`/docs/#${x.repo.split("/")[1]}/overview`}>docs</a>
                    {admin && (
                      <Button type="button" variant="ghost" size="icon-xs" aria-label={`Disconnect ${x.repo}`} onClick={() =>
                        confirm(`Stop syncing ${x.repo}? Its history, reviews and docs stay; connecting it again picks them back up.`) &&
                        act(() => post(`${base}/${x.id}`, undefined, "DELETE"), `Disconnected ${x.repo}.`)}><X /></Button>
                    )}
                  </span>
                  {x.status_detail && x.status !== "active" && <p className="text-destructive w-full text-xs">{x.status_detail}</p>}
                </div>
              ))}
            </div>
          ) : <Note>None yet. {admin ? <a className="link" href="/new/">Connect the first one.</a> : "An owner or admin connects them."}</Note>}
          {msg && <Note>{msg}</Note>}
        </div>
      )}
    </Section>
  )
}

type Integration = { service: string; base_url?: string; has_key?: boolean; status: string; verified_at?: string | null; last_error?: string | null }
type Flow = { workflow_id: string; name: string; status: string; verified_at: string | null; last_error: string | null }
type Integrations = { integrations: Integration[]; workflows: Flow[] }

function ServiceForm({ s, admin, reload, keyed }: { s: Integration; admin: boolean; reload: () => void; keyed: boolean }) {
  const [base, setBase] = useState(s.base_url ?? "")
  const [key, setKey] = useState("")
  const [msg, act] = useAct(reload)
  const id = `svc-${s.service}`
  const save = (e: React.FormEvent) => {
    e.preventDefault()
    act(() => post(`/api/v1/integrations/${s.service}`, { base_url: base, api_key: key }, "PUT").then(() => setKey("")), "Saved. Verify it to check the connection.")
  }
  const verify = () => act(async () => {
    const r = await post("/api/v1/integrations/connections/verify", { service: s.service })
    if (r.status !== "connected") throw new Error(r.detail)
  }, "Connected.")
  return (
    <form onSubmit={save} className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <StatusTag status={s.status} />
        {s.verified_at && <span className="text-muted-foreground text-xs">last verified <Time iso={s.verified_at} /></span>}
        {s.last_error && <span className="text-destructive text-xs">{s.last_error}</span>}
      </div>
      <fieldset disabled={!admin} className="grid gap-3 sm:grid-cols-2">
        <Field id={`${id}-url`} label="Address" hint={keyed ? "Where GG-Ops runs, e.g. https://ops.example.com" : "Where gg-pulse runs; the portal checks its /api/health."}>
          <Input id={`${id}-url`} value={base} onChange={(e) => setBase(e.target.value)} placeholder="https://" required />
        </Field>
        {keyed && (
          <Field id={`${id}-key`} label="GG-Ops API key" hint={s.has_key ? "A key is saved (encrypted; never shown again). Blank keeps it." : "GG-Ops → Account → API keys. A read-only key is enough."}>
            <Input id={`${id}-key`} type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} placeholder="ggops_sk_…" />
          </Field>
        )}
      </fieldset>
      {admin && (
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" type="submit">Save</Button>
          {s.status !== "not_configured" && <Button size="sm" type="button" variant="outline" onClick={verify}>Verify</Button>}
          {s.status !== "not_configured" && (
            <Button size="sm" type="button" variant="ghost" onClick={() => confirm(`Disconnect ${s.service}?`) &&
              act(() => post(`/api/v1/integrations/${s.service}`, undefined, "DELETE"), "Disconnected.")}>Disconnect</Button>
          )}
          {msg && <Note>{msg}</Note>}
        </div>
      )}
    </form>
  )
}

/** GG-Ops (and its projects as workflows) and gg-pulse, each verified against the real service. */
export function Services({ admin }: { admin: boolean }) {
  const d = useApi<Integrations>("/api/v1/integrations")
  const [flow, setFlow] = useState("")
  const [name, setName] = useState("")
  const [msg, act] = useAct(d.reload)
  const svc = (name: string) => d.data!.integrations.find((i) => i.service === name)!
  return (
    <Section title={<span className="flex items-center gap-2"><Link2 className="size-4" /> Integrations</span>}
      description="Called only by this server; keys are stored encrypted and never sent back to browsers.">
      {!d.data ? <Loading rows={3} /> : (
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-3">
            <h3 className="text-sm font-semibold">GG-Ops</h3>
            <ServiceForm s={svc("gg-ops")} admin={admin} reload={d.reload} keyed />
            <div className="space-y-2 border-t pt-3">
              <h4 className="flex items-center gap-2 text-sm font-medium"><Workflow className="size-4" /> Workflows</h4>
              <p className="text-muted-foreground text-xs text-pretty">
                A workflow is a GG-Ops project linked to this workspace, by its id (the last part of the project's URL in GG-Ops).
                The id is not a password: every read goes through the key above, so GG-Ops decides what can be seen.
              </p>
              {d.data.workflows.map((w) => (
                <div key={w.workflow_id} className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="font-medium">{w.name}</span>
                  <code className="text-muted-foreground font-mono text-xs">{w.workflow_id}</code>
                  <StatusTag status={w.status} />
                  {admin && (
                    <Button type="button" variant="ghost" size="icon-xs" className="ml-auto" aria-label={`Remove ${w.name}`} onClick={() =>
                      confirm(`Unlink ${w.name}? API clients bound to it are revoked.`) &&
                      act(() => post(`/api/v1/integrations/workflows/${w.workflow_id}`, undefined, "DELETE"), "Unlinked.")}><X /></Button>
                  )}
                  {w.last_error && <p className="text-destructive w-full text-xs">{w.last_error}</p>}
                </div>
              ))}
              {admin && (
                <form className="flex flex-wrap gap-2" onSubmit={(e) => {
                  e.preventDefault()
                  act(() => post("/api/v1/integrations/workflows", { workflow_id: flow.trim(), name }).then(() => { setFlow(""); setName("") }), "Linked.")
                }}>
                  <Input value={flow} onChange={(e) => setFlow(e.target.value)} placeholder="GG-Ops project id" aria-label="Workflow id" className="h-8 min-w-0 flex-1 font-mono text-xs" required />
                  <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Name (optional)" aria-label="Workflow name" className="h-8 w-40" />
                  <Button size="sm" type="submit"><Plus /> Link</Button>
                </form>
              )}
              {msg && <Note>{msg}</Note>}
            </div>
          </div>
          <div className="space-y-3">
            <h3 className="text-sm font-semibold">gg-pulse</h3>
            <ServiceForm s={svc("gg-pulse")} admin={admin} reload={d.reload} keyed={false} />
            <p className="text-muted-foreground text-xs text-pretty">gg-pulse offers no analytics API yet, so this is a health check only: the portal shows no analytics it can't actually read.</p>
          </div>
        </div>
      )}
    </Section>
  )
}

type Client = { client_id: string; name: string; scopes: string[]; workflow_id: string | null; created_by: string; created_at: string; last_used_at: string | null; revoked: boolean }

/** Programs (CI, automations, other services) that call the API v1 or the MCP server with their own short-lived tokens. */
export function ApiClients({ admin }: { admin: boolean }) {
  const c = useApi<{ clients: Client[]; scopes: Record<string, string> }>(admin ? "/api/v1/clients" : null)
  const flows = useApi<Integrations>(admin ? "/api/v1/integrations" : null)
  const [name, setName] = useState("")
  const [scopes, setScopes] = useState<string[]>(["repository:read"])
  const [flow, setFlow] = useState("")
  const [made, setMade] = useState<{ client: Client; client_secret: string } | null>(null)
  const [msg, act] = useAct(c.reload)
  if (!admin) return null
  const create = (e: React.FormEvent) => {
    e.preventDefault()
    act(() => post("/api/v1/clients", { name, scopes, workflow_id: flow || null }).then((r) => { setMade(r); setName("") }))
  }
  const origin = location.origin
  return (
    <Section title={<span className="flex items-center gap-2"><KeyRound className="size-4" /> API clients</span>}
      description="For programs, not people: each gets a client id and secret, trades them for a one-hour token, and can do only what its scopes allow in this workspace.">
      {!c.data ? <Loading rows={2} /> : (
        <div className="space-y-4">
          {made && (
            <div className="space-y-2 rounded-xl border p-3">
              <Note>Copy the secret now: it is shown this once and stored only as a hash.</Note>
              <Snippet label="Get a token" text={`curl -s ${origin}/api/v1/auth/token -H 'Content-Type: application/json' -d '{"grant_type":"client_credentials","client_id":"${made.client.client_id}","client_secret":"${made.client_secret}"}'`} />
              <Snippet label="Then call the API with it" text={`curl -s ${origin}/api/v1/auth/me -H "Authorization: Bearer <access_token>"`} />
            </div>
          )}
          {!!c.data.clients.length && (
            <div className="divide-y rounded-xl border">
              {c.data.clients.map((x) => (
                <div key={x.client_id} className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
                  <span className="font-medium">{x.name}</span>
                  <code className="text-muted-foreground font-mono text-xs">{x.client_id}</code>
                  {x.revoked ? <Tag tone="error">revoked</Tag> : x.scopes.map((s) => <Tag key={s}>{s}</Tag>)}
                  {x.workflow_id && <span className="text-muted-foreground text-xs">workflow {x.workflow_id.slice(0, 8)}…</span>}
                  <span className="text-muted-foreground ml-auto text-xs">{x.last_used_at ? <>used <Time iso={x.last_used_at} /></> : "never used"}</span>
                  {!x.revoked && (
                    <Button type="button" variant="ghost" size="sm" onClick={() => confirm(`Revoke ${x.name}? Its tokens stop working at once.`) &&
                      act(() => post(`/api/v1/clients/${x.client_id}`, undefined, "DELETE"))}>Revoke</Button>
                  )}
                </div>
              ))}
            </div>
          )}
          <form onSubmit={create} className="space-y-3">
            <div className="flex flex-wrap gap-2">
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Nightly CI" aria-label="Client name" className="h-8 w-56" maxLength={80} required />
              {!!flows.data?.workflows.length && (
                <select value={flow} onChange={(e) => setFlow(e.target.value)} aria-label="Bind to a workflow" className="bg-card h-8 rounded-full border px-3 text-sm">
                  <option value="">Any workflow</option>
                  {flows.data.workflows.map((w) => <option key={w.workflow_id} value={w.workflow_id}>Only {w.name}</option>)}
                </select>
              )}
              <Button size="sm" type="submit" disabled={!scopes.length}><Plus /> Create client</Button>
            </div>
            <div className="grid gap-1 sm:grid-cols-2">
              {Object.entries(c.data.scopes).map(([s, what]) => (
                <label key={s} className="flex items-start gap-2 text-xs">
                  <input type="checkbox" className="mt-0.5" checked={scopes.includes(s)}
                    onChange={(e) => setScopes((v) => e.target.checked ? [...v, s] : v.filter((x) => x !== s))} />
                  <span><code className="font-mono">{s}</code> <span className="text-muted-foreground">{what}</span></span>
                </label>
              ))}
            </div>
          </form>
          {msg && <p className="text-destructive text-sm">{msg}</p>}
        </div>
      )}
    </Section>
  )
}
