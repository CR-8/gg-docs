import { useEffect, useMemo, useRef, useState } from "react"
import { Marked } from "marked"
import { gfmHeadingId, getHeadingList } from "marked-gfm-heading-id"
import markedAlert from "marked-alert"
import DOMPurify from "dompurify"
import { cn } from "@/lib/utils"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"

export type Heading = { level: number; text: string; id: string }

const base = new Marked({ gfm: true }, gfmHeadingId(), markedAlert())
let withMath: Marked | null = null // KaTeX loads only for documents that have math in them
const MATH = /\$\$[\s\S]+?\$\$|(^|[^\\$])\$[^\s$][^$\n]*\$/
let n = 0

const theme = () => (document.documentElement.classList.contains("dark") ? "dark" : "default")

async function drawDiagrams(el: HTMLElement, light: boolean, onZoom: (svg: string) => void) {
  const blocks = [...el.querySelectorAll<HTMLElement>("pre > code.language-mermaid")]
  if (!blocks.length) return
  const { default: mermaid } = await import("mermaid")
  mermaid.initialize({ startOnLoad: false, maxEdges: 20000, maxTextSize: 10_000_000, theme: light ? "default" : theme() })
  for (const code of blocks) {
    try {
      const { svg } = await mermaid.render(`md-mermaid-${n++}`, code.textContent || "")
      const box = document.createElement("figure")
      box.className = "diagram not-prose my-5 cursor-zoom-in overflow-auto rounded-xl border bg-card p-4"
      box.title = "Click to enlarge"
      box.innerHTML = svg // mermaid sanitises labels (securityLevel "strict")
      box.onclick = () => onZoom(svg)
      code.parentElement!.replaceWith(box)
    } catch {
      code.parentElement!.classList.add("mermaid-invalid") // not valid Mermaid: stays a code block
    }
  }
}

async function highlight(el: HTMLElement) {
  const blocks = [...el.querySelectorAll<HTMLElement>("pre > code")].filter((c) => !c.classList.contains("language-mermaid"))
  if (!blocks.length) return
  const { default: hljs } = await import("highlight.js/lib/common")
  for (const code of blocks) {
    if (!code.dataset.highlighted) hljs.highlightElement(code)
    const pre = code.parentElement!
    if (pre.querySelector(".copy")) continue
    const btn = document.createElement("button")
    btn.className = "copy"
    btn.textContent = "Copy"
    btn.onclick = () =>
      navigator.clipboard.writeText(code.textContent || "").then(() => {
        btn.textContent = "Copied"
        setTimeout(() => (btn.textContent = "Copy"), 1200)
      })
    pre.append(btn)
  }
}

/** Markdown with everything the generated docs use: GitHub alerts, heading ids (for a table of contents), syntax
 *  highlighting with copy buttons, KaTeX math, and Mermaid diagrams that open full screen with zoom and pan.
 *  Always sanitised: it renders repo content, diffs and model output. `fix` rewrites links and images afterwards;
 *  `light` forces light diagrams (printing); `onRendered` fires once diagrams and highlighting are done. */
export function Markdown(props: {
  text: string
  className?: string
  fix?: (el: HTMLElement) => void
  onHeadings?: (h: Heading[]) => void
  onRendered?: () => void
  light?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [mathReady, setMathReady] = useState(!!withMath)
  const [zoom, setZoom] = useState<string | null>(null)
  const hasMath = MATH.test(props.text || "")
  useEffect(() => {
    if (hasMath && !withMath)
      Promise.all([import("marked-katex-extension"), import("katex/dist/katex.min.css")]).then(([{ default: katex }]) => {
        withMath = new Marked({ gfm: true }, gfmHeadingId(), markedAlert(), katex({ throwOnError: false, nonStandard: true }))
        setMathReady(true)
      })
  }, [hasMath])
  const [html, headings] = useMemo(() => {
    const md = hasMath && mathReady && withMath ? withMath : base
    const out = DOMPurify.sanitize(md.parse(props.text || "") as string)
    const list: Heading[] = getHeadingList()
      .filter((h) => h.level >= 2 && h.level <= 3)
      .map(({ level, text, id }) => ({ level, id, text: text.replace(/<[^>]+>/g, "") }))
    return [out, list] as const
  }, [props.text, hasMath, mathReady])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    props.fix?.(el)
    props.onHeadings?.(headings)
    Promise.all([drawDiagrams(el, !!props.light, setZoom), highlight(el)]).finally(() => props.onRendered?.())
  }, [html, props.fix])

  const zoomRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!zoom) return
    const t = setTimeout(async () => {
      const svg = zoomRef.current?.querySelector("svg")
      if (!svg) return
      const { default: svgPanZoom } = await import("svg-pan-zoom")
      svg.removeAttribute("width")
      svg.removeAttribute("height")
      svg.style.cssText = "width:100%;height:100%;max-width:none"
      svgPanZoom(svg, { controlIconsEnabled: true, fit: true, center: true, minZoom: 0.1, maxZoom: 40, zoomScaleSensitivity: 0.3 })
    }, 50)
    return () => clearTimeout(t)
  }, [zoom])

  return (
    <>
      <div
        ref={ref}
        className={cn(
          "markdown prose prose-sm dark:prose-invert prose-headings:scroll-mt-20 prose-headings:tracking-tight prose-pre:bg-muted",
          "prose-pre:text-foreground prose-code:before:content-none prose-code:after:content-none prose-table:text-xs max-w-none",
          props.className
        )}
        dangerouslySetInnerHTML={{ __html: html }}
      />
      <Dialog open={!!zoom} onOpenChange={(o) => !o && setZoom(null)}>
        <DialogContent className="h-[90svh] max-w-[95vw] p-2 sm:max-w-[95vw]">
          <DialogTitle className="sr-only">Diagram</DialogTitle>
          <div ref={zoomRef} className="size-full overflow-hidden" dangerouslySetInnerHTML={{ __html: zoom || "" }} />
        </DialogContent>
      </Dialog>
    </>
  )
}
