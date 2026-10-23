import { useEffect, useState } from "react"
import { LogOut } from "lucide-react"
import { useMe } from "@/lib/api"
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
            <Button variant="ghost" size="sm" className="gap-2 px-1.5" aria-label="Account">
              <span className="bg-muted grid size-6 place-items-center rounded-full text-xs font-semibold uppercase">
                {me.login[0]}
              </span>
              <span className="hidden max-w-32 truncate md:inline">{me.login}</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-56">
            <DropdownMenuLabel>
              {me.login}
              <div className="text-muted-foreground text-xs font-normal">
                {me.admin ? "Admin: can change Settings" : "Viewer"}
              </div>
            </DropdownMenuLabel>
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
