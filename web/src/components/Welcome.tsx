import { useState } from "react"
import { ArrowRight } from "lucide-react"
import { post, switchTo, useApi, useMe } from "@/lib/api"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"

type Invite = { workspace: string; role: string; invited_by: string }

/** An invite link lands here after sign-in: joining is a button, never just a visit to the link. */
function JoinInvite({ token }: { token: string }) {
  const inv = useApi<Invite>(`/api/invites/${encodeURIComponent(token)}`)
  const [error, setError] = useState("")
  const join = () =>
    post(`/api/invites/${encodeURIComponent(token)}/accept`).then(() => (location.href = "/"), (e) => setError(e.message))
  return (
    <Card className="w-full max-w-md gap-6 py-6 [--card-spacing:--spacing(6)]">
      <CardHeader>
        <span className="bg-brand text-brand-foreground mb-3 grid size-9 place-items-center rounded-xl font-mono text-sm font-bold">gg</span>
        <CardTitle className="text-xl font-semibold tracking-tight">{inv.data ? `Join ${inv.data.workspace}` : "Your invite"}</CardTitle>
        {inv.data && <CardDescription>{inv.data.invited_by} invited you as {inv.data.role === "member" ? "a member" : `an ${inv.data.role}`}.</CardDescription>}
      </CardHeader>
      <CardContent className="space-y-4">
        {(error || inv.error) && (
          <Alert variant="destructive">
            <AlertDescription>{error || inv.error}</AlertDescription>
          </Alert>
        )}
        {!inv.data && !inv.error && <Skeleton className="h-9 w-full" />}
        {inv.data && <Button className="h-9 w-full" onClick={join}>Join {inv.data.workspace}</Button>}
        <a className="link block text-xs" href="/welcome/">Not now: create or open another workspace</a>
      </CardContent>
    </Card>
  )
}

/** Create a workspace (then import a repository into it), or open one you already belong to. */
export function Welcome() {
  const me = useMe()
  const [name, setName] = useState("")
  const [error, setError] = useState(() => new URLSearchParams(location.search).get("error") || "")
  const invite = new URLSearchParams(location.search).get("invite")
  if (invite) return <JoinInvite token={invite} />
  const create = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await post("/api/workspaces", { name })
      location.href = "/new/?onboarding=1"
    } catch (err: any) {
      setError(err.message)
    }
  }

  return (
    <Card className="w-full max-w-md gap-6 py-6 [--card-spacing:--spacing(6)]">
      <CardHeader>
        <span className="bg-brand text-brand-foreground mb-3 grid size-9 place-items-center rounded-xl font-mono text-sm font-bold">gg</span>
        <CardTitle className="text-xl font-semibold tracking-tight">Create a workspace</CardTitle>
        <CardDescription>
          A workspace holds your team's repositories, people and settings. Joining a team instead? Open the invite link
          from your email.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <form onSubmit={create} className="space-y-3">
          <div className="grid gap-1.5">
            <Label htmlFor="ws-name">Workspace name</Label>
            <Input id="ws-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Acme engineering" maxLength={80} autoFocus required className="h-9" />
          </div>
          <Button className="h-9 w-full">Create, then pick its repositories <ArrowRight /></Button>
        </form>
        {me === undefined ? (
          <Skeleton className="h-9 w-full" />
        ) : !!me?.workspaces.length && (
          <div className="space-y-2">
            <p className="text-muted-foreground text-xs">Or open one of yours</p>
            <div className="divide-y rounded-xl border">
              {me.workspaces.map((w) => (
                <button key={w.id} type="button" onClick={() => switchTo(w.id)} className="hover:bg-muted/60 flex w-full items-center gap-2 px-3 py-2 text-left text-sm">
                  <span className="truncate font-medium">{w.name}</span>
                  <span className="text-muted-foreground ml-auto text-xs">{w.role}</span>
                </button>
              ))}
            </div>
          </div>
        )}
        <p className="text-muted-foreground text-xs">
          Signed in as {me?.login ?? "…"} · <a className="link" href="/auth/logout">Sign out</a>
        </p>
      </CardContent>
    </Card>
  )
}
