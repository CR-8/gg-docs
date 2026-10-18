import { useApi } from "@/lib/api"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"

export function Login() {
  const q = new URLSearchParams(location.search)
  const next = q.get("next") || "/"
  const error = q.get("error")
  const m = useApi<{ github: boolean; password: boolean; org: string }>("/auth/methods").data

  return (
    <Card className="w-full max-w-sm gap-6 py-6 [--card-spacing:--spacing(6)]">
      <CardHeader>
        <span className="bg-brand text-brand-foreground mb-3 grid size-9 place-items-center rounded-xl font-mono text-sm font-bold">gg</span>
        <CardTitle className="text-xl font-semibold tracking-tight">GeneGenius engineering portal</CardTitle>
        <CardDescription>Architecture, activity, reviews and code quality for the GeneGenius repositories.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {!m && <Skeleton className="h-20 w-full" />}
        {m?.github && (
          <Button asChild variant={m.password ? "outline" : "default"} className="h-9 w-full">
            <a href={`/auth/github?next=${encodeURIComponent(next)}`}>Sign in with GitHub</a>
          </Button>
        )}
        {m?.github && m.password && (
          <div className="text-muted-foreground flex items-center gap-3 text-xs before:h-px before:flex-1 before:bg-border after:h-px after:flex-1 after:bg-border">
            or
          </div>
        )}
        {m?.password && (
          <form method="post" action="/auth/password" className="space-y-3">
            <input type="hidden" name="next" value={next} />
            <div className="grid gap-1.5">
              <Label htmlFor="pw">Password</Label>
              <Input id="pw" name="password" type="password" autoComplete="current-password" autoFocus required className="h-9" />
            </div>
            <Button className="h-9 w-full">Sign in</Button>
            <p className="text-muted-foreground text-xs text-pretty">
              The viewer password shows everything; the admin password can also change Settings.
            </p>
          </form>
        )}
        {m && !m.github && !m.password && (
          <p className="text-muted-foreground text-sm">
            No login method is configured: set PORTAL_PASSWORD / ADMIN_PASSWORD, or the GitHub App, in .env.
          </p>
        )}
        {m?.github && <p className="text-muted-foreground text-xs">GitHub sign-in is for members of {m.org}.</p>}
      </CardContent>
    </Card>
  )
}
