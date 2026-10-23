import { GitCommitHorizontal, OctagonAlert, Upload, Users } from "lucide-react"
import { useApi } from "@/lib/api"
import {
  Bar, Choice, DataTable, Empty, Failed, KpiStrip, Loading, PageAside, PERIODS, personHref, Section, useQueryState,
} from "@/components/common"

type Row = Record<string, any> & { person: string }

// links from before each person had a page: /people/#login
if (location.hash.length > 1) location.replace(personHref(decodeURIComponent(location.hash.slice(1))))

export function People() {
  const [days, setDays] = useQueryState("days", "90")
  const rows = useApi<Row[]>(`/api/people?days=${days}`)
  const total = (key: string) => rows.data?.reduce((n, r) => n + (r[key] || 0), 0)

  return (
    <div className="space-y-6">
      <PageAside>
        <KpiStrip items={[
          { label: "People", icon: Users, value: rows.data?.length, title: "GitHub logins or git emails with any activity in the period" },
          { label: `Commits, ${days} days`, icon: GitCommitHorizontal, value: total("commits") },
          { label: `Pushes, ${days} days`, icon: Upload, value: total("pushes") },
          { label: "Open breakages", icon: OctagonAlert, value: total("open_breakages"), tone: total("open_breakages") ? "error" : undefined },
        ]} />
      </PageAside>
      <Bar>
        <Choice label="Period" value={days} onChange={setDays} options={PERIODS.slice(1)} />
      </Bar>
      {rows.error && <Failed error={rows.error} />}
      <Section
        flush
        title="People"
        description="Signals for a conversation, not a verdict: whoever works in the riskiest code collects more findings. Hover a column for what it counts."
      >
        {!rows.data ? <Loading rows={6} /> : (
          <DataTable
            rows={rows.data}
            sort="commits"
            onRow={(r) => (location.href = `${personHref(r.person)}&days=${days}`)}
            empty={<Empty title="No one yet">People appear once the first sync has read the git history.</Empty>}
            columns={[
              { key: "person", label: "Person", render: (r) => <a className="font-medium hover:underline" href={`${personHref(r.person)}&days=${days}`}>{r.person}</a> },
              { key: "commits", label: "Commits", num: true, title: "Non-merge commits on any branch" },
              { key: "adds", label: "+ lines", num: true, title: "Lines added by those commits" },
              { key: "dels", label: "− lines", num: true, wide: true, title: "Lines deleted" },
              { key: "pushes", label: "Pushes", num: true, title: "Pushes made by this GitHub login" },
              { key: "breakages", label: "Broke CI", num: true, title: "Default-branch commits where a check that passed before failed",
                render: (r) => r.breakages ? <span className="text-rose-600 dark:text-rose-400">{r.breakages}{r.open_breakages ? ` (${r.open_breakages} open)` : ""}</span> : "" },
              { key: "findings", label: "Findings", num: true, title: "Review findings on lines this person wrote (git blame): critical / major / minor / nit",
                render: (r) => r.findings ? <>{r.findings} <span className="text-muted-foreground text-xs">{r.critical || 0}c {r.major || 0}M {r.minor || 0}m {r.nit || 0}n</span></> : "" },
              { key: "slop", label: "Slop", num: true, title: "Findings in the slop category: works, but shouldn't be merged as is" },
              { key: "weighted_per_kloc", label: "Per 1k lines", num: true, title: "Findings weighted nit 0.5, minor 1, major 3, critical 8, per 1,000 added lines" },
            ]}
          />
        )}
      </Section>
    </div>
  )
}
