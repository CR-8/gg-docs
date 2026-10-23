// The handful of pieces every page is built from, so they all read the same.
import { useEffect, useMemo, useState, type ReactNode } from "react"
import { ArrowDown, ArrowUp, Check, Copy as CopyIcon } from "lucide-react"
import { cn } from "@/lib/utils"
import { ago, FAIL, PASS, short, when } from "@/lib/api"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Pill, PillIndicator } from "@/components/kibo-ui/pill"

/** A compact select: options are [value, label]. */
export const Choice = (props: {
  value: string
  onChange: (v: string) => void
  options: [string, string][]
  label: string
  className?: string
}) => (
  <Select value={props.value} onValueChange={props.onChange}>
    <SelectTrigger size="sm" aria-label={props.label} className={props.className}>
      <SelectValue />
    </SelectTrigger>
    <SelectContent>
      {props.options.map(([v, l]) => (
        <SelectItem key={v} value={v}>
          {l}
        </SelectItem>
      ))}
    </SelectContent>
  </Select>
)

export const PERIODS: [string, string][] = [["7", "Last 7 days"], ["30", "Last 30 days"], ["90", "Last 90 days"], ["365", "Last year"]]

/** Query-string state, kept in the URL so a filtered page can be shared or reloaded. */
export function useQueryState(key: string, initial: string): [string, (v: string) => void] {
  const [value, setValue] = useState(() => new URLSearchParams(location.search).get(key) ?? initial)
  const set = (v: string) => {
    const q = new URLSearchParams(location.search)
    if (v && v !== initial) q.set(key, v)
    else q.delete(key)
    history.replaceState(null, "", `${location.pathname}${q.size ? "?" + q : ""}${location.hash}`)
    setValue(v)
  }
  return [value, set]
}

export const useDebounced = <T,>(value: T, ms = 400) => {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

/** A titled card; `flush` for tables that run edge to edge. */
export function Section(props: {
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
  flush?: boolean
  className?: string
  children: ReactNode
}) {
  return (
    <Card className={cn("gap-0 py-0", props.className)}>
      <CardHeader className="border-b py-3.5">
        <CardTitle>{props.title}</CardTitle>
        {props.description && <CardDescription>{props.description}</CardDescription>}
        {props.action && <CardAction>{props.action}</CardAction>}
      </CardHeader>
      <CardContent className={props.flush ? "px-0" : "py-4"}>{props.children}</CardContent>
    </Card>
  )
}

/** Filters and actions above a page's content. */
export const Bar = ({ children }: { children: ReactNode }) => (
  <div className="mb-6 flex flex-wrap items-center gap-2">{children}</div>
)

export const Empty = ({ title, children }: { title: string; children?: ReactNode }) => (
  <div className="text-muted-foreground px-6 py-12 text-center text-sm">
    <p className="text-foreground font-medium">{title}</p>
    {children && <p className="mx-auto mt-1 max-w-md text-pretty">{children}</p>}
  </div>
)

export const Failed = ({ error }: { error: string }) => (
  <Alert variant="destructive">
    <AlertTitle>Couldn't load this</AlertTitle>
    <AlertDescription>{error}</AlertDescription>
  </Alert>
)

export const Loading = ({ rows = 4 }: { rows?: number }) => (
  <div className="space-y-2 p-4">
    {Array.from({ length: rows }, (_, i) => (
      <Skeleton key={i} className="h-8 w-full" />
    ))}
  </div>
)

export const Time = ({ iso }: { iso?: string | null }) =>
  iso ? (
    <time dateTime={iso} title={when(iso)} className="whitespace-nowrap">
      {ago(iso)}
    </time>
  ) : null

const ext = { target: "_blank", rel: "noreferrer" }
export const Sha = ({ repo, sha }: { repo: string; sha?: string | null }) =>
  sha ? (
    <a className="link font-mono text-xs" href={`https://github.com/${repo}/commit/${sha}`} {...ext}>
      {short(sha)}
    </a>
  ) : null
export const Pr = ({ repo, n }: { repo: string; n: number }) => (
  <a className="link" href={`https://github.com/${repo}/pull/${n}`} {...ext}>
    #{n}
  </a>
)
export const Person = ({ name }: { name?: string | null }) =>
  name ? (
    <a className="link" href={`/people/#${encodeURIComponent(name)}`}>
      {name}
    </a>
  ) : (
    <span className="text-muted-foreground">unknown</span>
  )

type Tone = "success" | "error" | "warning" | "info"
/** The one chip style (Kibo UI Pill), optionally with a coloured dot. */
export const Tag = ({ tone, children, className }: { tone?: Tone; children: ReactNode; className?: string }) => (
  <Pill className={cn("px-2 py-0.5 text-xs", className)}>
    {tone && <PillIndicator variant={tone} />}
    {children}
  </Pill>
)

const SEVERITY: Record<string, Tone> = { critical: "error", major: "warning", minor: "info" }
export const Severity = ({ s }: { s: string }) => <Tag tone={SEVERITY[s]}>{s}</Tag>

export type Check = { name: string; conclusion: string; url?: string }
export function CiChecks({ checks }: { checks?: Check[] }) {
  if (!checks?.length) return <span className="text-muted-foreground text-xs">no CI yet</span>
  const failed = checks.filter((c) => FAIL.includes(c.conclusion))
  const passed = checks.filter((c) => PASS.includes(c.conclusion)).length
  return (
    <div className="flex flex-wrap gap-1">
      <Tag tone={failed.length ? "error" : "success"}>
        {passed}/{checks.length} passed
      </Tag>
      {failed.map((c) => (
        <a key={c.name} href={c.url} {...ext}>
          <Tag className="text-rose-600 dark:text-rose-400">{c.name}</Tag>
        </a>
      ))}
    </div>
  )
}

export type Column<T> = {
  key: string
  label: string
  num?: boolean
  wide?: boolean // hidden on small screens
  title?: string
  render?: (row: T) => ReactNode
}

/** Sortable table: click a header to sort, click a row for `onRow` (links inside a row still work). */
export function DataTable<T extends Record<string, any>>(props: {
  rows: T[]
  columns: Column<T>[]
  sort: string
  desc?: boolean
  onRow?: (row: T) => void
  empty?: ReactNode
}) {
  const [key, setKey] = useState(props.sort)
  const [down, setDown] = useState(props.desc ?? true)
  const rows = useMemo(() => {
    const none = down ? -Infinity : Infinity
    return [...props.rows].sort((a, b) => {
      const x = a[key] ?? none, y = b[key] ?? none
      return (x > y ? 1 : x < y ? -1 : 0) * (down ? -1 : 1)
    })
  }, [props.rows, key, down])
  if (!rows.length) return <>{props.empty ?? <Empty title="Nothing here yet" />}</>
  const cell = (c: Column<T>) => cn(c.num && "text-right tabular-nums", c.wide && "hidden md:table-cell")
  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          {props.columns.map((c) => (
            <TableHead key={c.key} title={c.title} className={cn(cell(c), "first:pl-4 last:pr-4")}>
              <button
                className="hover:text-foreground inline-flex items-center gap-1"
                onClick={() => {
                  setDown(c.key === key ? !down : true)
                  setKey(c.key)
                }}
              >
                {c.label}
                {c.key === key && (down ? <ArrowDown className="size-3" /> : <ArrowUp className="size-3" />)}
              </button>
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r, i) => (
          <TableRow
            key={i}
            className={props.onRow && "cursor-pointer"}
            onClick={(e) => props.onRow && !(e.target as HTMLElement).closest("a,button") && props.onRow(r)}
          >
            {props.columns.map((c) => (
              <TableCell key={c.key} className={cn(cell(c), "align-top whitespace-normal first:pl-4 last:pr-4")}>
                {c.render ? c.render(r) : c.num && typeof r[c.key] === "number" ? r[c.key].toLocaleString() : (r[c.key] ?? "")}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** A labelled form field with an optional hint under it. */
export const Field = ({ id, label, hint, children }: { id: string; label: string; hint?: ReactNode; children: ReactNode }) => (
  <div className="grid gap-1.5">
    <Label htmlFor={id}>{label}</Label>
    {children}
    {hint && <p className="text-muted-foreground text-xs text-pretty">{hint}</p>}
  </div>
)

export const Note = ({ children }: { children: ReactNode }) => <span className="text-muted-foreground text-sm">{children}</span>

export function Copy({ text }: { text: string }) {
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

export const Snippet = ({ label, text }: { label: ReactNode; text: string }) => (
  <div>
    <div className="text-muted-foreground mb-1 text-xs">{label}</div>
    <div className="bg-muted flex items-start gap-2 rounded-lg py-2 pr-2 pl-3">
      <code className="flex-1 font-mono text-xs break-all whitespace-pre-wrap">{text}</code>
      <Copy text={text} />
    </div>
  </div>
)
