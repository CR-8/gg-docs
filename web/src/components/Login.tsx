import { useApi } from "@/lib/api"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

export function Login() {
  const q = new URLSearchParams(location.search)
  const next = q.get("next") || "/"
  const error = q.get("error")
  const m = useApi<{ github: boolean }>("/auth/methods").data
  const invited = next.startsWith("/invite/")

  return (
    <Card className="w-full max-w-sm gap-6 py-6 [--card-spacing:--spacing(6)]">
      <CardHeader>
        <span className="bg-brand text-brand-foreground mb-3 grid size-9 place-items-center rounded-xl font-mono text-sm font-bold">gg</span>
        <CardTitle className="text-xl font-semibold tracking-tight">{invited ? "You're invited" : "gg engineering portal"}</CardTitle>
        <CardDescription>
          {invited
            ? "Sign in with GitHub to join the workspace."
            : "Architecture, activity, reviews, docs and code quality for your team's repositories."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {!m && <Skeleton className="h-9 w-full" />}
        {m?.github && (
          <Button asChild className="h-9 w-full">
            <a href={`/auth/github?next=${encodeURIComponent(next)}`}>Continue with GitHub</a>
          </Button>
        )}
        {m && !m.github && (
          <p className="text-muted-foreground text-sm">
            Sign-in isn't set up: the GitHub App's GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET go in .env.
          </p>
        )}
        {m?.github && (
          <p className="text-muted-foreground text-xs text-pretty">
            New here? Signing in creates your account; then create a workspace or open an invite link.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
