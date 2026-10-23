import { useEffect, useReducer, useState } from "react"

/** JSON from the portal API; a lapsed session goes to sign-in and comes back here afterwards, and someone in no
 * workspace yet goes to the welcome page. */
export async function api<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts })
  if (r.status === 401) {
    location.href = "/auth/login?next=" + encodeURIComponent(location.pathname + location.search)
    return new Promise(() => {})
  }
  if (r.status === 409 && !location.pathname.startsWith("/welcome")) {
    location.href = "/welcome/"
    return new Promise(() => {})
  }
  const data = await r.json().catch(() => ({}))
  if (!r.ok) throw Object.assign(new Error(data.error || data.detail || r.statusText), { data })
  return data
}

export const post = <T = any>(path: string, body?: unknown, method = "POST") =>
  api<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) })

/** Loads `path` (null = not yet) and keeps the last data while a new path loads, so nothing flickers. */
export function useApi<T = any>(path: string | null) {
  const [state, setState] = useState<{ data?: T; error?: string }>({})
  const [tick, reload] = useReducer((n: number) => n + 1, 0)
  useEffect(() => {
    if (!path) return
    let live = true
    api<T>(path).then(
      (data) => live && setState({ data }),
      (e) => live && setState((s) => ({ ...s, error: e.message }))
    )
    return () => {
      live = false
    }
  }, [path, tick])
  return { ...state, reload }
}

export type Role = "owner" | "admin" | "member"
export type Workspace = { id: number; name: string; role: Role }
export type Me = {
  login: string
  avatar_url: string | null
  admin: boolean // owner or admin of the current workspace
  role: Role | null
  workspace: { id: number; name: string } | null
  workspaces: Workspace[]
} | null
let mePromise: Promise<Me> | undefined
export const getMe = () => (mePromise ??= api<Me>("/api/me").catch(() => null))

export function useMe() {
  const [me, setMe] = useState<Me | undefined>()
  useEffect(() => {
    getMe().then(setMe)
  }, [])
  return me
}

/** Opens another of my workspaces: the session remembers it, so every page and API call follows. */
export const switchTo = (id: number, to = "/") => post(`/api/workspaces/${id}/switch`).then(() => (location.href = to))

export const short = (sha?: string | null) => (sha || "").slice(0, 8)
export const repoName = (repo?: string | null) => (repo || "").split("/")[1] || repo || ""
export const when = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : ""

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60],
]
export function ago(iso?: string | null) {
  if (!iso) return ""
  const s = (new Date(iso).getTime() - Date.now()) / 1000
  for (const [unit, secs] of UNITS) if (Math.abs(s) >= secs) return rtf.format(Math.round(s / secs), unit)
  return "just now"
}

export const FAIL = ["failure", "timed_out"]
export const PASS = ["success", "skipped", "neutral"] // same rule as /api/summary

export const bytes = (n: number) => (n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.round(n / 1e6)} MB`)
