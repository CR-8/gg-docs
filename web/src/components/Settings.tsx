import { useEffect, useState, type ReactNode } from "react"
import { Check, Copy as CopyIcon, Cpu, GitBranch, Hammer, Plug, Plus, RefreshCw, X } from "lucide-react"
import { bytes, post, useApi, useMe } from "@/lib/api"
import { Choice, DataTable, Loading, Section, Tag, Time } from "@/components/common"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Status, StatusIndicator, StatusLabel } from "@/components/kibo-ui/status"
import { Spinner } from "@/components/kibo-ui/spinner"

type Provider = "local" | "anthropic" | "openai"
type LocalModel = { name: string; size: number; params?: string; quant?: string }
const DEFAULT_MODEL = { anthropic: "claude-opus-5-5", openai: "" }

const Field = ({ id, label, hint, children }: { id: string; label: string; hint?: ReactNode; children: ReactNode }) => (
  <div className="grid gap-1.5">
    <Label htmlFor={id}>{label}</Label>
    {children}
    {hint && <p className="text-muted-foreground text-xs text-pretty">{hint}</p>}
  </div>
)

const Note = ({ children }: { children: ReactNode }) => <span className="text-muted-foreground text-sm">{children}</span>

function StatusPanel({ admin }: { admin: boolean }) {
  const s = useApi("/api/summary")
  const [msg, setMsg] = useState("")
  useEffect(() => {
    const t = setInterval(s.reload, 15000)
    return () => clearInterval(t)
  }, [])
  const act = async (a: string) => {
    try {
      await post(`/api/admin/${a}`)
      setMsg("Queued.")
      s.reload()
    } catch (e: any) {
      setMsg(e.message)
    }
  }
  const d = s.data
  const row = (label: string, ok: boolean | null, text: ReactNode) => (
    <div className="flex items-center justify-between gap-3 py-2 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <Status status={ok === null ? "maintenance" : ok ? "online" : "offline"}>
        <StatusIndicator />
        <StatusLabel className="text-foreground">{text}</StatusLabel>
      </Status>
    </div>
  )
  return (
    <Section title="Status" description="Refreshes every 15 seconds.">
      {!d ? <Loading rows={4} /> : (
        <div className="divide-y">
          {row("GitHub access", !!d.github, d.github === "app" ? "GitHub App" : d.github === "token" ? "Read-only token" : "None")}
          {row("Language model", d.llm, d.llm ? <code className="font-mono text-xs">{d.llm_model}</code> : "Not set")}
          {row("Job queue", d.worker.queued ? null : true, d.worker.queued ? `${d.worker.queued} waiting: ${d.worker.pending.join(", ")}` : "Idle")}
          {row("CI on default branches", !d.open_breakages, d.open_breakages ? `${d.open_breakages} broken` : "Nothing broken")}
        </div>
      )}
      {admin && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => act("sync")}><RefreshCw /> Sync now</Button>
          <Button variant="outline" size="sm" onClick={() => act("rebuild")}><Hammer /> Rebuild views</Button>
          <Note>{msg}</Note>
        </div>
      )}
    </Section>
  )
}

function Copy({ text }: { text: string }) {
  const [done, setDone] = useState(false)
  return (
    <Button type="button" variant="ghost" size="icon-xs" aria-label="Copy" onClick={() =>
      navigator.clipboard.writeText(text).then(() => {
        setDone(true)
        setTimeout(() => setDone(false), 1500)
      })}>
      {done ? <Check /> : <CopyIcon />}
    </Button>
  )
}

const Snippet = ({ label, text }: { label: ReactNode; text: string }) => (
  <div>
    <div className="text-muted-foreground mb-1 text-xs">{label}</div>
    <div className="bg-muted flex items-start gap-2 rounded-lg py-2 pr-2 pl-3">
      <code className="flex-1 font-mono text-xs break-all whitespace-pre-wrap">{text}</code>
      <Copy text={text} />
    </div>
  </div>
)

const MCP_TOOLS = ["pull_requests", "pull_request", "recent_pushes", "docs", "sql", "commit_diff", "code_graph", "architecture", "sonar_issues"]

function Mcp({ admin }: { admin: boolean }) {
  const m = useApi<{ url: string; token_url: string | null; token: string | null }>(admin ? "/api/settings/mcp" : null)
  return (
    <Section
      title={<span className="flex items-center gap-2"><Plug className="size-4" /> Connect a coding agent (MCP)</span>}
      description="Give Claude Code, Codex, ChatGPT, Mistral, Cursor or any MCP client live, read-only access to PRs, pushes, CI, reviews, docs and the code graph."
    >
      {!admin ? (
        <Note>An admin can copy the connection details from this page.</Note>
      ) : !m.data ? <Loading rows={3} /> : !m.data.token ? (
        <Note>
          The MCP server is off. Re-run <code className="font-mono text-xs">install.sh</code> (it creates <code className="font-mono text-xs">MCP_TOKEN</code>), or set{" "}
          <code className="font-mono text-xs">MCP_TOKEN</code> in .env and restart the portal.
        </Note>
      ) : (
        <div className="space-y-4">
          <Snippet label="Claude Code" text={`claude mcp add --transport http genegenius ${m.data.url} --header "Authorization: Bearer ${m.data.token}"`} />
          <Snippet label="Codex CLI" text={`codex mcp add genegenius --url ${m.data.token_url}`} />
          <Snippet
            label="ChatGPT (developer mode), Mistral Le Chat, Cursor, Gemini CLI and others: add a remote MCP server with this URL and no authentication. The URL itself is the secret, so share it like a password."
            text={m.data.token_url!}
          />
          <div className="flex flex-wrap gap-1">{MCP_TOOLS.map((t) => <Tag key={t}>{t}</Tag>)}</div>
        </div>
      )}
    </Section>
  )
}

function Repositories({ admin }: { admin: boolean }) {
  const s = useApi("/api/summary")
  const [repo, setRepo] = useState("")
  const [msg, setMsg] = useState("")
  const act = async (action: string, r: string) => {
    try {
      await post(`/api/admin/${action}`, { repo: r })
      setMsg(action === "add_repo" ? `Added. ${r} is being cloned; its documentation follows.` : `Removed ${r}.`)
      setRepo("")
      s.reload()
    } catch (e: any) {
      setMsg(e.message)
    }
  }
  const extra: string[] = s.data?.extra_repos ?? []
  return (
    <Section title={<span className="flex items-center gap-2"><GitBranch className="size-4" /> Repositories</span>}
      description="Every repository here is synced, reviewed and documented. The first two also get the architecture views and SonarQube.">
      {!s.data ? <Loading rows={2} /> : (
        <div className="space-y-3">
          <div className="divide-y rounded-lg border">
            {(s.data.repos as string[]).map((r) => (
              <div key={r} className="flex items-center gap-2 px-3 py-2 text-sm">
                <span className="font-mono text-xs">{r}</span>
                <Tag className="ml-1">{extra.includes(r) ? "added" : "built in"}</Tag>
                <a className="link ml-auto text-xs" href={`/docs/#${r.split("/")[1]}/overview`}>docs</a>
                {admin && extra.includes(r) && (
                  <Button type="button" variant="ghost" size="icon-xs" aria-label={`Remove ${r}`} onClick={() => act("remove_repo", r)}><X /></Button>
                )}
              </div>
            ))}
          </div>
          {admin && (
            <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); act("add_repo", repo.trim()) }}>
              <Input value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="owner/repository" className="h-8 w-64 font-mono text-xs" pattern="[\w.\-]+/[\w.\-]+" required />
              <Button size="sm" type="submit" variant="outline"><Plus /> Add</Button>
              <Note>{msg || "The GitHub token or App must be able to read it."}</Note>
            </form>
          )}
        </div>
      )}
    </Section>
  )
}

function Jobs() {
  const ev = useApi<any[]>("/api/events")
  useEffect(() => {
    const t = setInterval(ev.reload, 15000)
    return () => clearInterval(t)
  }, [])
  return (
    <Section flush title="Recent jobs" description="Sync, push, checks, rebuild, review and Sonar runs, newest first. A failed step says why.">
      {!ev.data ? <Loading /> : (
        <DataTable
          rows={ev.data}
          sort="id"
          columns={[
            { key: "id", label: "When", render: (r) => <Time iso={r.at} /> },
            { key: "kind", label: "Job" },
            { key: "ok", label: "Result", render: (r) => <Tag tone={r.ok ? "success" : "error"}>{r.ok ? "ok" : "failed"}</Tag> },
            { key: "detail", label: "Detail", render: (r) => <code className="font-mono text-xs break-all whitespace-pre-wrap">{r.detail}</code> },
          ]}
        />
      )}
    </Section>
  )
}

export function Settings() {
  const me = useMe()
  const saved = useApi("/api/settings")
  const local = useApi<{ base_url: string; models: LocalModel[] }>("/api/settings/local")
  const [provider, setProvider] = useState<Provider>("anthropic")
  const [model, setModel] = useState<Record<Provider, string>>({ local: "", ...DEFAULT_MODEL })
  const [base, setBase] = useState("")
  const [key, setKey] = useState("")
  const [effort, setEffort] = useState("high")
  const [postReviews, setPostReviews] = useState(false)
  const [sonar, setSonar] = useState("")
  const [msg, setMsg] = useState<{ ok?: boolean; text: string; busy?: boolean }>({ text: "" })

  // Fill the form once both the saved settings and the local model list are in.
  const ready = saved.data && local.data
  useEffect(() => {
    if (!ready) return
    const s = saved.data, models = local.data!.models
    const isLocal = s.llm_kind === "openai" && s.llm_base_url === local.data!.base_url
    const p: Provider = isLocal ? "local" : s.llm_kind
    setProvider(p)
    setModel({
      local: models.some((m) => m.name === s.llm_model) ? s.llm_model : models[0]?.name || "",
      anthropic: p === "anthropic" ? s.llm_model : DEFAULT_MODEL.anthropic,
      openai: p === "openai" ? s.llm_model : "",
    })
    setBase(isLocal ? "" : s.llm_base_url)
    setEffort(s.llm_effort || "none")
    setPostReviews(!!s.post_reviews)
  }, [ready])

  const admin = !!me?.admin
  const body = () => ({
    ...(provider === "local"
      ? { llm_kind: "openai", llm_base_url: local.data!.base_url, llm_model: model.local, llm_effort: "" }
      : { llm_kind: provider, llm_base_url: base.trim(), llm_model: model[provider].trim(),
          llm_effort: provider === "anthropic" && effort !== "none" ? effort : "", llm_api_key: key }),
    post_reviews: postReviews,
    sonar_token: sonar,
  })
  const test = async () => {
    setMsg({ text: "Sending a one-word request…", busy: true })
    try {
      const r = await post("/api/settings/test", body())
      setMsg({ ok: r.ok, text: r.ok ? `Works. The model replied “${r.reply}”.` : r.reply })
    } catch (e: any) {
      setMsg({ ok: false, text: e.message })
    }
  }
  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await post("/api/settings", body(), "PUT")
      setKey("")
      setSonar("")
      saved.reload()
      setMsg({ ok: true, text: "Saved. Reviews and Ask use it from now on." })
    } catch (err: any) {
      setMsg({ ok: false, text: err.message })
    }
  }

  const keyHint = saved.data?.llm_api_key ? `A key is saved (${saved.data.llm_api_key}). Leave blank to keep it.` : "Kept on the server, never sent back to the browser."
  const setM = (v: string) => setModel((m) => ({ ...m, [provider]: v }))

  return (
    <div className="space-y-6">
      <div className="grid gap-6 lg:grid-cols-3">
        <form onSubmit={save} className="space-y-6 lg:col-span-2">
          <fieldset disabled={!admin} className="space-y-6">
            <Section
              title="Language model"
              description={admin ? "Writes the code reviews and answers questions in Ask." : "Only admins can change these. Sign in with the admin password to edit."}
            >
              {!ready ? <Loading rows={3} /> : (
                <Tabs value={provider} onValueChange={(v) => setProvider(v as Provider)}>
                  <TabsList>
                    <TabsTrigger value="local"><Cpu /> Local model</TabsTrigger>
                    <TabsTrigger value="anthropic">Claude</TabsTrigger>
                    <TabsTrigger value="openai">OpenAI-compatible</TabsTrigger>
                  </TabsList>

                  <TabsContent value="local" className="mt-4 space-y-4">
                    {local.data!.models.length ? (
                      <Field id="local-model" label="Model" hint={<>Runs on this server through Ollama: no key needed, and no code leaves the VM. Every model already downloaded there is listed.</>}>
                        <Choice
                          label="Local model"
                          className="w-full sm:w-96"
                          value={model.local}
                          onChange={setM}
                          options={local.data!.models.map((m) => [m.name, [m.name, m.params, m.quant, bytes(m.size)].filter(Boolean).join(" · ")])}
                        />
                      </Field>
                    ) : (
                      <p className="text-muted-foreground text-sm text-pretty">
                        No local model found on this server. Install one with{" "}
                        <code className="bg-muted rounded px-1 py-0.5 font-mono text-xs">LOCAL_LLM=auto bash install.sh &lt;vm-ip&gt;</code>;
                        every model it downloads shows up here.
                      </p>
                    )}
                  </TabsContent>

                  <TabsContent value="anthropic" className="mt-4 grid gap-4 sm:grid-cols-2">
                    <Field id="a-key" label="API key" hint={keyHint}>
                      <Input id="a-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} />
                    </Field>
                    <Field id="a-model" label="Model">
                      <Input id="a-model" value={model.anthropic} onChange={(e) => setM(e.target.value)} placeholder={DEFAULT_MODEL.anthropic} />
                    </Field>
                    <Field id="a-effort" label="Effort" hint="Choose “Don't send” for models or gateways that reject it.">
                      <Choice label="Effort" value={effort} onChange={setEffort}
                        options={[["none", "Don't send"], ["low", "low"], ["medium", "medium"], ["high", "high"], ["xhigh", "xhigh"], ["max", "max"]]} />
                    </Field>
                    <Field id="a-base" label="Base URL" hint="Blank for Anthropic's own API.">
                      <Input id="a-base" value={base} onChange={(e) => setBase(e.target.value)} placeholder="https://api.anthropic.com" />
                    </Field>
                  </TabsContent>

                  <TabsContent value="openai" className="mt-4 grid gap-4 sm:grid-cols-2">
                    <Field id="o-base" label="Base URL" hint="OpenAI, a gateway, or your own server (vLLM, llama.cpp, LM Studio); usually ends in /v1.">
                      <Input id="o-base" value={base} onChange={(e) => setBase(e.target.value)} placeholder="https://api.openai.com/v1" />
                    </Field>
                    <Field id="o-model" label="Model">
                      <Input id="o-model" value={model.openai} onChange={(e) => setM(e.target.value)} placeholder="gpt-5" />
                    </Field>
                    <Field id="o-key" label="API key" hint={`${keyHint} Not needed for a server of your own.`}>
                      <Input id="o-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} />
                    </Field>
                  </TabsContent>
                </Tabs>
              )}
            </Section>

            <div className="grid gap-6 sm:grid-cols-2">
              <Section title="Review bot">
                <div className="flex items-start gap-3">
                  <Switch id="post" checked={postReviews} onCheckedChange={setPostReviews} />
                  <Label htmlFor="post" className="block leading-snug font-normal">
                    Post reviews on GitHub PRs
                    <span className="text-muted-foreground mt-1 block text-xs">
                      Off: reviews stay in the portal and in Ask. Posting needs the GitHub App, or a token that can write pull requests.
                    </span>
                  </Label>
                </div>
              </Section>
              <Section title="SonarQube">
                <Field id="sonar" label="Token" hint={<>{saved.data?.sonar_token ? `Saved (${saved.data.sonar_token}). Blank keeps it. ` : ""}SonarQube → My Account → Security → Global Analysis token.</>}>
                  <Input id="sonar" type="password" autoComplete="off" value={sonar} onChange={(e) => setSonar(e.target.value)} />
                </Field>
              </Section>
            </div>
          </fieldset>

          {admin && (
            <div className="flex flex-wrap items-center gap-2">
              <Button type="submit">Save settings</Button>
              <Button type="button" variant="outline" onClick={test} disabled={msg.busy || (provider === "local" && !model.local)}>
                Test connection
              </Button>
              {msg.busy && <Spinner className="size-4" />}
              <span className={msg.ok === false ? "text-destructive text-sm" : msg.ok ? "text-sm text-emerald-600 dark:text-emerald-400" : "text-muted-foreground text-sm"}>
                {msg.text}
              </span>
            </div>
          )}
        </form>
        <StatusPanel admin={admin} />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Repositories admin={admin} />
        <Mcp admin={admin} />
      </div>
      <Jobs />
    </div>
  )
}
