# gg portal — Design System

Applies to the gg-docs web portal (`web/`). The Healthsnap and DisputeFox dashboards are visual references for density,
soft surfaces, pill controls, rounded cards and clear metric hierarchy. This product is an engineering/workspace
analytics portal, not a health or credit application.

## Design principles

- **Calm and analytical:** present GitHub activity, CI reliability, documentation coverage, reviews, repository
  structure and team contribution signals without visual noise.
- **Preserve the gg branding** and the existing information architecture. Never introduce medical metrics or
  replace real product data with fictional dashboard values.
- **Derive metrics from existing portal APIs** or show an honest loading, unavailable or empty state.
- **Respect the stack:** Astro, React 19, Tailwind CSS 4, shadcn/ui, Kibo UI, Lucide React, Geist Variable and Geist
  Mono. Reuse `web/src/components/ui` and `web/src/components/common.tsx`; avoid new dependencies unless justified.
- **Improve composition and shared styling** without breaking workspace switching, authentication, tenant scoping,
  routes or existing product flows.

## Visual language

Use the references' gentle, compact dashboard treatment: cool light-gray canvas, white/near-white main surfaces, rounded
cards, thin neutral borders, restrained shadows, pill controls, a large page heading, muted descriptions and a clear KPI
hierarchy. Keep the existing gg teal as the primary accent. Red/coral is reserved for meaningful CI failures and
critical review findings. Avoid heavy glassmorphism, loud gradients, oversized hero sections, noisy backgrounds and
generic admin-template styling.

## Tokens

Existing tokens in `web/src/styles/global.css` are the source of truth. Extend centrally if necessary; do not scatter
hard-coded colors. Any added CSS variable is defined there with a `.dark` override.

| Purpose | Token / intent |
|---|---|
| Page | `bg-background` (the cool gray canvas); `bg-muted/40` for a backing surface inside a card. In light mode `muted` equals the canvas, so anything highlighted directly on the canvas uses `bg-card` |
| Card | `bg-card text-card-foreground` |
| Primary text | `text-foreground` |
| Secondary text | `text-muted-foreground` |
| Borders | `border-border` |
| Brand | `text-brand`, `bg-brand`, `ring-ring` |
| Inverse panel | `bg-inverse text-inverse-foreground`: one dark emphasis panel per page at most, dark in both themes |
| Panel shadow | `shadow-panel`: a faint, cool-tinted lift for cards |
| Success | existing emerald status / Kibo `Status` |
| Warning | existing amber status / Kibo `Status` |
| Failure / critical | existing `destructive` / rose-red status |
| Typography | Geist Variable sans; Geist Mono for SHA, branch, route and technical identifiers |
| Radius | the CSS `--radius` scale; panels `rounded-2xl`, controls `rounded-full`, inner tiles `rounded-xl` |

## Typography and density

- Page titles: 24–30px (`text-3xl` on desktop), semibold, modest negative tracking.
- Section/card titles: 14–16px, medium or semibold.
- KPI values: 24–32px, semibold with `tabular-nums`.
- Body text: 13–14px; supporting metadata: 11–12px.
- Technical identifiers, commit SHAs, branches, paths and API routes use Geist Mono.
- Sentence case and concise labels. Maintain contrast in light and dark mode; never rely on color alone to communicate
  CI state.

## Layout

- Content stays centered at `max-w-7xl`.
- Gutters: 24px desktop, 20px tablet, 16px mobile.
- Navigation is a sidebar at left (240px, `bg-card`, a border on its right). On desktop it collapses to a 72px icon
  rail and the choice is remembered per browser; between 768 and 1024px it is always the rail; on phones it is a menu
  drawer (a native `<dialog>`) opened from the top bar.
- A compact top bar (60px) over the content holds the theme switcher, connect-a-repository and the workspace/account
  menu at right; on phones also the menu button and the gg mark.
- Page head: title and description at left, the page's KPI strip at right (it wraps under the title on small screens).
- Desktop: a balanced 12-column grid with repository/health panels, then full-width tables or route details.
- Tablet: two columns with wrapping controls. Mobile: one column, stacked controls and table-specific horizontal
  scrolling; no page-level overflow.
- 16–24px between major sections, 12–16px within cards. Do not remove navigation entries or change routes to imitate
  a reference.

## Component rules

### Navigation

Overview, Docs, Activity, People, Reviews, Ask and Settings stay, each with an icon and its label. The current route is
a filled pill (`bg-primary`); keep `aria-current="page"`, keyboard focus, 40px touch targets, accessible names and the
workspace/account menu. In the rail the label stays for screen readers and shows on hover. The collapse button carries
`aria-expanded`; the drawer closes on Escape, its close button or a click on the backdrop. Rail-only styles use the
`rail:` variant in `global.css` and apply inside `.sidebar` only, so the drawer keeps its labels.

### KPI and metric cards

Each metric has a clear label, value and optional context. Use API-derived values only: repository counts, route and
module counts, commits, pushes, CI checks, review findings or open breakages where exposed. Missing data gets an honest
loading, not-yet-synced or unavailable state; no fabricated zeroes. A count capped by an API limit says so (`100+`).
Never label commit counts as productivity or rank people as good or bad developers.

### Cards and sections

Build on the shared `Section` and shadcn `Card`. Keep title, description and action placement consistent. Thin
borders, subtle hover surfaces, restrained shadows. Avoid nested cards unless they communicate real hierarchy.

### Status and badges

Reuse Kibo `Status`, `Pill` and `Tag`. Every status carries text or an icon as well as color. CI breakages and
critical/major findings may use destructive colors; neutral metadata stays neutral.

### Charts and summaries

Add a chart only when existing API data supports it and it answers a useful operational question. Give it a title, the
period, labels, an accessible text summary and a no-data state. Neutral (hatched) bars, brand teal for emphasis,
destructive red only for real failures. No decorative gauges. Contribution charts may show commits or pushes over time;
never infer effort or code quality from raw line counts.

### Tables and filters

Keep sortability, URL-backed filters, GitHub links, the person and review detail sheets and repository filters.
Compact, accessible controls that keep their query parameters. Dense tables may scroll on mobile; error details stay
inspectable.

### Loading, empty and error states

Reuse `Loading`, `Empty` and `Failed`. Say what is missing and how it will fill in, with the existing links (connect a
repository, Settings, Activity, Docs). Keep stale data visible while refreshing where the hook supports it.

## Interaction and accessibility

- Every control works; no inert decorative buttons.
- Keep the current data fetching and endpoints; never bypass API authorization.
- Respect tenant isolation: never aggregate, cache or expose data across workspaces.
- Keyboard operation, visible focus, semantic headings, accessible labels, readable statuses, and
  `prefers-reduced-motion` honoured.
- Readable contrast in light and dark themes; test both.

## Responsive checkpoints

About 1440px, 1024px, 768px and 390px: navigation and workspace menu reachable, metric cards wrapping, no control
collisions, tables reachable, dialogs and sheets fitting the viewport, no page-level horizontal overflow.

## Shared pieces (web/src/components/common.tsx and global.css)

| Piece | Use |
|---|---|
| `SideNav.astro` | Brand and the main navigation, used by the sidebar and the phone drawer in `layouts/main.astro` |
| `rail:` variant | Styles for the collapsed sidebar (and the 768–1024px rail) |
| `Section` | Every panel: `rounded-2xl`, `bg-card`, `shadow-panel`; `flush` for tables (divider under the header) |
| `PageAside` | Renders into the page head's right side (`#page-aside` in `layouts/main.astro`), from inside a page's React island |
| `KpiStrip` | The page's headline numbers: icon disc, value, label; `undefined` value = loading, `null` = unavailable |
| `Tile` | A small labelled value inside a panel (for example CI, routes, symbols) |
| `hatch` utility | Neutral striped fill for chart bars |
| `bg-inverse` | The one dark emphasis panel (for example the architecture report on Overview) |

## Implementation guardrails

1. Inspect `web/src/layouts/main.astro`, `web/src/styles/global.css`, `web/src/components/common.tsx`, the affected
   React page and its API shape before editing.
2. Prefer shared components and global tokens over duplicated page CSS.
3. Use existing design-system dependencies; no charting library unless justified.
4. Do not rewrite FastAPI, MongoDB queries, authentication, GitHub integration, API schemas or tenant filtering for a
   visual task.
5. Do not replace workspace content with health information or fake sample data.
6. Establish the system in the shared shell and the overview/analytics pages first; avoid redesigning every page in
   one pass.
7. Type-check and build from `web/`; run the Python tests only if backend code changed.
8. Report files changed, behaviour retained, verification and limitations.
