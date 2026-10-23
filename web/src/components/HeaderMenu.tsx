import { useEffect, useState } from "react"
import { Check, GitBranch, LogOut, Plus } from "lucide-react"
import { switchTo, useMe } from "@/lib/api"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { ThemeSwitcher } from "@/components/kibo-ui/theme-switcher"

type Theme = "light" | "dark" | "system"
const dark = matchMedia("(prefers-color-scheme: dark)")
const apply = (t: Theme) =>
  document.documentElement.classList.toggle("dark", t === "dark" || (t === "system" && dark.matches))

export function HeaderMenu() {
  const me = useMe()
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem("theme") as Theme) || "system"
    } catch {
      return "system"
    }
  })
  useEffect(() => {
    const follow = () => theme === "system" && apply("system")
    dark.addEventListener("change", follow)
    return () => dark.removeEventListener("change", follow)
  }, [theme])
  const choose = (t: Theme) => {
    setTheme(t)
    apply(t)
    try {
      if (t === "system") localStorage.removeItem("theme")
      else localStorage.setItem("theme", t)
    } catch {}
  }

  return (
    <div className="ml-auto flex shrink-0 items-center gap-2">
      <ThemeSwitcher value={theme} onChange={choose} className="hidden sm:flex" />
      {me && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" className="gap-2 px-1.5" aria-label="Account and workspaces">
              {me.avatar_url ? (
                <img src={me.avatar_url} alt="" className="size-6 rounded-full" />
              ) : (
                <span className="bg-muted grid size-6 place-items-center rounded-full text-xs font-semibold uppercase">
                  {me.login[0]}
                </span>
              )}
              <span className="hidden max-w-40 truncate md:inline">{me.workspace?.name ?? me.login}</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-64">
            <DropdownMenuLabel>
              {me.login}
              <div className="text-muted-foreground text-xs font-normal">
                {me.role ? `${me.role[0].toUpperCase()}${me.role.slice(1)} of ${me.workspace?.name}` : "In no workspace yet"}
              </div>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuLabel className="text-muted-foreground text-xs font-normal">Workspaces</DropdownMenuLabel>
            {me.workspaces.map((w) => (
              <DropdownMenuItem key={w.id} onSelect={() => w.id !== me.workspace?.id && switchTo(w.id)}>
                <Check className={w.id === me.workspace?.id ? "" : "invisible"} />
                <span className="truncate">{w.name}</span>
              </DropdownMenuItem>
            ))}
            <DropdownMenuItem asChild>
              <a href="/welcome/">
                <Plus /> New workspace
              </a>
            </DropdownMenuItem>
            {me.admin && (
              <DropdownMenuItem asChild>
                <a href="/new/">
                  <GitBranch /> Import repository
                </a>
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem asChild>
              <a href="/auth/logout">
                <LogOut /> Sign out
              </a>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  )
}
