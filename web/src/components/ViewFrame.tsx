import { useEffect, useId, useRef, useState } from "react"
import { ArrowUpRight } from "lucide-react"
import { cn } from "@/lib/utils"
import { SIDES, VIEWS, viewsFor, type Side, type View } from "@/lib/views"
import { Empty, Loading } from "@/components/common"
import { Markdown } from "@/components/Markdown"
import { Button } from "@/components/ui/button"

/** Links styled like the pill tabs and the main navigation, since each tab is its own page. */
function LinkTabs({ items }: { items: { href: string; label: string; on: boolean }[] }) {
  return (
    <nav className="bg-card shadow-panel ring-foreground/5 text-muted-foreground inline-flex flex-wrap gap-0.5 rounded-full p-1 ring-1">
      {items.map((t) => (
        <a
          key={t.href}
          href={t.href}
          aria-current={t.on ? "page" : undefined}
          className={cn(
            "rounded-full px-3 py-1 text-[13px] font-medium transition-colors",
            t.on ? "bg-primary text-primary-foreground" : "hover:bg-muted hover:text-foreground"
          )}
        >
          {t.label}
        </a>
      ))}
    </nav>
  )
}

/** A Mermaid diagram that fills its box, fitted, with wheel-zoom and drag-pan. */
export function Mermaid({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null)
  const id = "mmd" + useId().replace(/[^a-zA-Z0-9]/g, "") // one render id per diagram, so several fit on a page
  useEffect(() => {
    let live = true
    ;(async () => {
      const [{ default: mermaid }, { default: svgPanZoom }] = await Promise.all([import("mermaid"), import("svg-pan-zoom")])
      mermaid.initialize({
        startOnLoad: false, maxEdges: 20000, maxTextSize: 10_000_000, flowchart: { useMaxWidth: false },
        theme: document.documentElement.classList.contains("dark") ? "dark" : "default",
      })
      const { svg } = await mermaid.render(id, text)
      if (!live || !ref.current) return
      ref.current.innerHTML = svg // mermaid sanitises labels (securityLevel "strict")
      const el = ref.current.querySelector("svg")!
      el.removeAttribute("width")
      el.removeAttribute("height")
      el.style.cssText = "width:100%;height:100%;max-width:none"
      // diagrams come out thousands of px wide: fit, then wheel-zoom and drag-pan
      const pz = svgPanZoom(el, { controlIconsEnabled: true, fit: true, center: true, minZoom: 0.05, maxZoom: 40, zoomScaleSensitivity: 0.3 })
      const scale = pz.getSizes().realZoom
      if (scale > 1) { // a small diagram: shown at its own size, not blown up to fill the box
        pz.zoomBy(1 / scale)
        pz.center()
      }
    })()
    return () => {
      live = false
    }
  }, [text])
  return <div ref={ref} className="size-full" />
}

export function ViewFrame({ side, view }: { side: Side; view: View }) {
  const file = `/site/${side}/${VIEWS[view].file}`
  const frame = file.endsWith(".html")
  const [content, setContent] = useState<string | null | undefined>() // undefined = loading, null = not built
  useEffect(() => {
    fetch(file, frame ? { method: "HEAD" } : undefined)
      .then((r) => (r.ok ? (frame ? "" : r.text()) : null))
      .then(setContent, () => setContent(null))
  }, [file])

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <LinkTabs
          items={(Object.keys(SIDES) as Side[]).map((s) => ({
            href: `/views/${s}/${viewsFor(s).includes(view) ? view : "graph"}/`, label: SIDES[s].name, on: s === side,
          }))}
        />
        <LinkTabs items={viewsFor(side).map((v) => ({ href: `/views/${side}/${v}/`, label: VIEWS[v].name, on: v === view }))} />
        {content != null && (
          <Button variant="outline" size="sm" className="ml-auto" asChild>
            <a href={file} target="_blank" rel="noreferrer">
              {frame ? "Full screen" : "Raw file"} <ArrowUpRight />
            </a>
          </Button>
        )}
      </div>
      <div className="bg-card h-[calc(100svh-15rem)] min-h-[28rem] overflow-hidden rounded-xl border">
        {content === undefined ? (
          <Loading rows={6} />
        ) : content === null ? (
          <Empty title="Not built yet">
            This view appears after the next build. Admins can start one from{" "}
            <a className="link" href="/settings/">Settings</a> → Rebuild views.
          </Empty>
        ) : frame ? (
          <iframe src={file} title={`${SIDES[side].name} ${VIEWS[view].name}`} className="size-full" />
        ) : file.endsWith(".mmd") ? (
          <Mermaid text={content} />
        ) : (
          <Markdown text={content} className="h-full overflow-auto p-6" />
        )}
      </div>
    </div>
  )
}
