# gg-pulse

A self-hosted dashboard that stays in sync with your team's GitHub repositories and answers questions about them:

| You want to know | Where |
|---|---|
| How the code is organised: modules, files, imports, call flow, every route | **Overview** (generated views) and `ARCHITECTURE.md` |
| Written documentation of every repository (overview, architecture, data flow, system design, ML system, API, one document per component), kept current on every push | **Docs**, downloadable as Markdown, .zip or PDF |
| Who pushed what, to which branch, and what CI said about it | **Activity** |
| Whose push broke a CI check that was passing before, and whether it is fixed | **Activity** (red banner), **People** |
| What the review bot found in each PR or direct push, and who wrote each flagged line | **Reviews**, and as comments on the PR when posting is on |
| Per person: commits, lines, pushes, CI breakages, review findings, "slop" | **People** |
| Anything across all of the above, in plain language | **Ask** (the chatbot) |
| All of it inside your coding agent (Claude Code, Codex, ChatGPT, Mistral, Cursor, any MCP client) | the **MCP server** at `/mcp` (1.14) |
| Your team: who is in, invites by email, which repositories are synced | **Settings → Team**, **Import** (1.15) |
| Programs (CI, automations, GG-Ops) reading the portal with their own scoped, short-lived credentials | the **API v1** and **API clients** (1.16) |
| Your GG-Ops projects and gg-pulse, linked to the workspace | **Settings → Integrations** (1.16) |

One portal serves many teams: each **workspace** has its own repositories, members, settings and data (1.15). Sign in
with GitHub, create a workspace, pick the repositories your GitHub account can reach, and they are cloned and kept in
sync by webhooks. Everything is stored in MongoDB (1.12).

Tests stay in your CI. The portal reads the CI results (GitHub check runs) and never runs your test suites itself.

---

## Contents

1. [How it works](#1-how-it-works)
2. [Using the portal](#2-using-the-portal)
3. [Setting it up](#3-setting-it-up)
4. [Integrating it into the backend repository](#4-integrating-it-into-the-backend-repository)
5. [Configuration reference](#5-configuration-reference)
6. [Operating it](#6-operating-it)
7. [Local development](#7-local-development)
8. [Limits and known gaps](#8-limits-and-known-gaps)

---

## 1. How it works

### 1.1 The pieces

```
                  push / pull_request / check_run webhooks (signed)
 GitHub ─────────────────────────────────────────────────────────────┐
   ▲   ▲  REST API (installation token): commits, events, check runs │
   │   └──────────────────────────────────────────────┐               ▼
   │ PR reviews as gg-pulse[bot]              ┌────────┴──────────────────────────────┐
   └──────────────────────────────────────────┤ portal container (:7000)              │
                                              │  FastAPI app ── pages + JSON API      │
 Browser ── "Sign in with GitHub" ──────────▶ │  worker thread ── job queue           │
                                              │  poller thread ── every 5 min         │
 Programs ── API v1 (bearer token) ─────────▶ │  /data: git clones, views, key file   │
                                              └──┬────────────────────────────────────┘
                                   MONGODB_URI   │
                                     ┌───────────▼──┐
                                     │ MongoDB      │
                                     └──────────────┘
           server-side only: GG-Ops REST API (its API key) · gg-pulse /api/health
                                   optional:  Caddy (HTTPS on 443) in front of the portal
```

| Component | What it does |
|---|---|
| **portal** (one container) | Runs the web app, a single **worker thread** that executes jobs one at a time, and a **poller thread**. Keeps clones and generated views in the `/data` volume. |
| **MongoDB** | The database: users and their GitHub identities, workspaces, members, connected repositories, everything synced from GitHub, reviews, docs, settings, API clients and integrations (1.12). The `mongo` service in compose, or MongoDB Atlas through `MONGODB_URI`. |
| **Caddy** (optional `https` profile) | Terminates TLS with an automatic certificate and passes every request to the portal. |
| **GitHub App** | The portal's identity on GitHub: it clones the repos, receives webhooks, reads CI results, posts reviews, and provides "Sign in with GitHub". |
| **LLM** (any OpenAI-compatible or Claude-compatible endpoint) | Writes the code reviews and the documentation, and powers the chatbot. The local model (Ollama on the VM) or a hosted one, chosen by an admin in Settings. |
| **Web frontend** (`web/`) | Astro + React + shadcn/ui (with Kibo UI parts), built to static files the portal serves. One page shell for everything: Overview, Docs, Activity, People, Reviews, Ask, Settings and the generated views. |

### 1.2 Repository layout

```
build.py              Generates the architecture views and ARCHITECTURE.md from two checkouts
depcruise.cjs         dependency-cruiser rules for the frontend (circular, orphan, unresolvable imports)
package.json/-lock    Node tools build.py uses (dependency-cruiser, typescript)
requirements.txt      Python dependencies of the portal (pinned)
portal/
  __init__.py         Configuration (every environment variable is read here) and WORKSPACE, the current tenant
  db.py               MongoDB: collections, indexes, the workspace-scoped access every tenant query goes through
  secret.py           Secrets at rest (encrypted with TOKEN_ENCRYPTION_KEY) and the digests of issued credentials
  auth.py             API clients, their short-lived access tokens, login handoffs, rate limits
  integrations.py     GG-Ops (its REST API) and gg-pulse (its health check), called only by the server
  migrate.py          The one-off SQLite -> MongoDB migration (python -m portal.migrate)
  github.py           GitHub App auth (a token per installation), REST helpers, OAuth login, webhook signature
  sync.py             Worker + poller: clone/fetch, ingest, breakage detection, rebuild scheduling
  review.py           The review bot
  llm.py              One interface over Claude-compatible and OpenAI-compatible APIs
  chat.py             The chatbot's tools and loop
  docgen.py           The documentation writer: code analysis, diagrams, LLM prose, incremental updates, its own worker
  mcp.py              The MCP server (Streamable HTTP) over the same tools as the chatbot
  main.py             Web app: login, workspaces, members and invites, import, webhook endpoint, JSON API, MCP
                      endpoint, serves web/dist and the views
  mail.py             Invite emails over SMTP
web/                  The frontend: Astro pages (src/pages), React islands (src/components), shadcn/ui and Kibo UI
                      components (src/components/ui, kibo-ui); `npm run build` writes web/dist
tests/                Checks for the logic that is easy to get subtly wrong, on a throwaway MongoDB database
Dockerfile            The portal image (Python 3.11 + git + Node 22)
docker-compose.yml    portal, mongo, optional caddy and llm
Caddyfile             HTTPS routing for the https profile
.env.example          Every setting you need to fill in
```

### 1.3 Staying in sync with GitHub

Two triggers feed the same job queue:

- **Webhooks** (instant). GitHub calls `POST /webhook/github`. The portal rejects the request unless its `X-Hub-Signature-256` HMAC matches `GITHUB_WEBHOOK_SECRET`, then:
  - `push` → records the pusher, branch, `before` and `after` SHAs and whether it was forced, learns the email-to-login mapping from the payload's commits, and queues a **push** job.
  - `pull_request` with action `opened`, `synchronize`, `reopened` or `ready_for_review` (drafts skipped) → queues a **review_pr** job.
  - `check_run` / `check_suite` completed → queues a **checks** job.
- **Poller** (backup, every `POLL_SECONDS`, default 300). Queues a **sync** job per repo and keeps the job log at 5,000 entries. Each sync also queues a review for every open, non-draft PR whose current head hasn't been reviewed yet (once an LLM is set). So if webhooks are missed or can't reach the portal, pushes, CI results and PR reviews still arrive within one poll. On the first poll after an LLM is set, every open PR gets reviewed once.

Jobs are de-duplicated while they wait, so a burst of pushes collapses into one rebuild. A failing job is logged (Settings → Recent jobs) and never stops the queue.

Each sync also stores every open PR and the 50 most recently closed (`pulls`), and `pull_request` webhooks update them
on every action (opened, edited, synchronised, closed, merged), so Ask and the MCP server always know what is in flight.

**A sync job** does, per repo:

1. **Clone or fetch.** On first run `git clone` into `/data/repos/<name>`, afterwards `git fetch --prune`. The installation token is passed as a one-off HTTP header (`git -c http.extraheader=…`), so it is never written to `.git/config`.
2. **Ingest commits.** `git log` over every remote branch for the last `BACKFILL_DAYS` (default 90): SHA, author, date, subject, lines added and deleted, files changed, and whether it is a merge. Commits reachable from the default branch get `on_default = 1`.
3. **Learn identities.** The commits API returns the GitHub login behind each author email on the default branch; noreply addresses (`123+login@users.noreply.github.com`) are decoded directly. Every table keyed by an email is then re-pointed to the login, which is how "Sagar V Gour" and "Sagar Vinod Gour" become one person.
4. **Record pushes** from the Events API (the latest 300 events) for anything a webhook missed. Each push stores the commits it introduced (`before..after`; for a new branch, the commits not yet on the default branch).
5. **Fetch CI results.** Check runs for the newest 15 first-parent commits of the default branch (60 on the first run). For each commit and check name, the latest completed run wins.
6. **Detect breakages** (next section).
7. **React to a new default-branch head:** queue a views **rebuild** and a **review_push** for the change. The very first sync only records the baseline.

### 1.4 "Whose push broke the code"

The default branch's first-parent history (newest 80 commits) is walked from oldest to newest, check by check:

- A check that **passed** on an earlier commit and **fails** (`failure` or `timed_out`) on this one is a **breakage**: `broken_sha` is this commit and `last_good_sha` the last passing one.
- It stays **open** until a later commit **passes** that check again (`fixed_sha`).
- `cancelled`, `skipped` and `neutral` results don't change anything, and a check that never passed inside the window is not blamed on anyone.

Each breakage records the commit's author (`person`) and whoever pushed it (`pusher`, from the push records). With squash merges, the commit author is the PR author; with merge commits, it is whoever merged.

Example: `unit` passes on c1, fails on c2 and c3, is cancelled on c4 and passes on c5 → one breakage: broken c2, last good c1, fixed c5.

### 1.5 The architecture views (build.py)

On every new default-branch head the worker checks out both default branches in its clones and runs `build.py graphs routes deps`. The build is a separate process, so a failure can't take the portal down.

| Step | What it produces | How |
|---|---|---|
| **graphs** | Module graph, module/file tree, call-flow diagrams and a graph report, for backend `app/` and frontend `src/` | [graphify](https://pypi.org/project/graphifyy/) parses the code (tree-sitter, no LLM) into functions, classes, imports and calls, then clusters it into modules. Modules are named after the file most of their code lives in. Graphs over 5,000 symbols are shown per module. |
| **routes** | API reference (every backend route grouped by tag) and the route tables | Imports the backend's FastAPI app in a dedicated venv (`/data/venvs/backend`, reinstalled when `requirements.txt` changes) and exports its OpenAPI spec. Placeholder `SECRET_KEY`/`MONGODB_URL` let the app import without real secrets; nothing connects anywhere. A failed export keeps the last good spec. Frontend routes come from the Next.js `src/app` folder structure (route groups, `@slots` and `_private` folders handled). |
| **deps** | Import maps for both repos and the frontend dependency-rule report | Backend: Python's own `ast` module reads every import inside `app/` and groups them two folders deep. Frontend: dependency-cruiser (with `depcruise.cjs`) resolves every import through the TypeScript path aliases, reports circular, orphan and unresolvable imports, and groups the map by route folder. Diagrams render with Mermaid, with pan and zoom. |
| *(always)* | `index.html` and `ARCHITECTURE.md` | The overview page and a Markdown version with modules and their files, import maps as Mermaid blocks (GitHub renders them), and every route. |

### 1.5a Generated documentation (docgen.py)

For **every** repository the portal syncs (the two built in, `EXTRA_REPOS`, and any added in Settings), it writes and
maintains a set of Markdown documents:

| Document | What it covers |
|---|---|
| Overview | What the repository is, its main parts, how they fit together, how to run it |
| Architecture | Layers, the component map (diagram), who depends on whom, design decisions, coupling hotspots |
| Data flow | The main flows step by step (with a sequence diagram), entry points to data stores (diagram), external services |
| System design | Deployment (diagram from docker-compose), configuration (every env var the code reads), background work, security, scaling |
| ML system | Only when the code uses ML libraries or ships model files: models, pipeline, where weights live, risks |
| API and routes | Every FastAPI route (from the decorators, with `APIRouter` prefixes) and Next.js page or API route, by component |
| One per component | Purpose, how it works, entry points, dependencies (diagram), files, public interface, routes, config, gotchas |

**How a document is made.** The facts come from the code on the default branch, read with git (no checkout), for any
Python or JS/TS repository: Python through its own `ast` (classes, functions, docstrings, imports, routes, env vars,
pydantic settings), JS/TS through import and export parsing (with `@/` aliases and Next.js app-router routes).
Components are the top-level folders under the source root (`app/`, `src/`, ...), large ones split one level deeper,
small ones merged. Diagrams are drawn from those facts, so they are always right; the configured model writes the
explanations on top, from the same facts only. Without a model, every document still has its full reference part.

**Kept current.** Every document stores a fingerprint of what it was written from (its files' blob SHAs; for the
repository-wide documents, the structure: components, dependencies, routes, libraries, env vars, config files). After
each push only the documents whose inputs changed are rewritten; a new repository gets its whole set; setting a model
for the first time rewrites everything with prose. Every version is kept (Docs → History shows the diff), and all of
them are searchable (every word matches as a prefix, title hits first). Documentation runs on its own worker thread, so a slow local model never holds up
syncs and reviews, and Ask goes first: documentation and reviews send one request at a time and start none while
someone is waiting for an answer. Admins can rewrite one document or a whole set from the Docs page.

**Reading and exporting.** Docs renders GitHub-style alerts, syntax-highlighted code with copy buttons, KaTeX math,
tables, and Mermaid diagrams that open full screen with zoom and pan, with a table of contents. Each document downloads
as `.md`; a repository's set downloads as a `.zip` (with an index) or prints as one PDF book (cover, contents, a page
break per document) through the browser's "Save as PDF".

### 1.6 The review bot

**When it runs:**

- On every **PR** when it is opened, reopened, marked ready, or gets new commits. Drafts are skipped.
- On every **direct push to a default branch**, unless that push is the merge of a PR. The PR was already reviewed while it was open, which the portal checks through GitHub's "PRs for this commit" API.

**What it reviews:**

- **Only the diff.** For a PR, the first review covers the merge-base to the head. Later reviews are **incremental**, covering only what changed since the last reviewed head (a full review again after a force-push).
- **Files it skips:** lockfiles, minified, binary, image and data files (`.csv`, `.vcf`, `.json` other than `package.json`/`tsconfig*.json`), Markdown and text, and `vendor/`, `dist/`, `build/`, `node_modules/`, `__snapshots__/`.
- **Size limits:** files are packed into requests of at most 150,000 characters (about 40k tokens), up to 600,000 characters per review. Anything beyond that is listed in the review summary as "Not reviewed (too large)" instead of being silently cut.

**How a review is made:**

1. Every line of the diff is prefixed with its line number in the new file, so the model can point at exact lines.
2. The model gets a fixed instruction set and returns JSON: a summary, plus findings with `path`, `line`, `severity` and `category`. Severities: `critical`, `major`, `minor`, `nit`. Categories: `bug`, `security`, `perf`, `test-gap`, `slop`. **Slop** means code that works but shouldn't be merged as is: dead or commented-out code, debug prints, copy-paste, needless abstraction, unused imports, placeholder TODOs, comments that restate the code, generated filler.
3. Findings are checked: unknown files, bad line numbers and invalid severities or categories are dropped or normalised.
4. Each finding is **blamed**: `git blame` on that line at the head commit gives the commit and author who actually wrote it.
5. Everything is stored. The review is never redone for the same head.
6. If **Post reviews on GitHub PRs** is on (Settings, off by default), the bot posts **one PR review**: a summary with counts, inline comments on up to 40 findings that sit on lines GitHub can anchor, the rest listed in the summary, and a link back to the portal. If GitHub rejects an anchor, everything goes into the summary instead. Direct pushes aren't posted anywhere; they appear in the portal.

The diff is treated as untrusted input. The model has no tools while reviewing, its output is parsed as JSON, and anything posted to GitHub is plain text.

### 1.7 People metrics

All for the period you pick (30, 90 or 365 days):

| Column | Meaning |
|---|---|
| Commits, + lines, − lines | Non-merge commits on any branch and the lines they add and delete |
| Pushes | Pushes made by that GitHub login |
| Broke CI | Breakages (1.4) whose commit this person wrote, and how many are still open |
| Findings | Review findings on lines this person wrote, by severity (c / M / m / n) |
| Slop | Findings in the `slop` category |
| Per 1k lines | (0.5×nit + 1×minor + 3×major + 8×critical) × 1000 / lines added; blank when nothing of theirs was reviewed |

A person is a GitHub login when the portal knows it, otherwise a git email. **These numbers are signals for a conversation, not a verdict:** someone who works in the riskiest code will collect more findings.

### 1.9 The chatbot (Ask)

Each question runs a loop of up to 10 rounds. The model picks tools, the portal runs them and returns the results, and the model answers. Tools:

| Tool | What it does | Safety |
|---|---|---|
| `query` | One MongoDB aggregation pipeline on one of the workspace's collections (fields described in the prompt); returns up to 200 documents | Read-only stages only (`$match`, `$group`, `$project`, `$sort`, `$lookup` with from/localField/foreignField, …; no `$out`, `$merge`, `$unionWith`, `$function`, `$where`); the first stage keeps only this workspace's documents and every `$lookup` gets the same filter; 5-second limit |
| `commit_diff` | `git show` of a commit, optionally one file's patch | Repo must be one of the two; the SHA must be hex |
| `code_graph` | Searches the graphify code graph ("what calls X", "what imports Y") | Read-only |
| `architecture` | Returns a section of `ARCHITECTURE.md`, or its table of contents | Read-only |
| `pull_requests` · `pull_request` | Open/closed/merged PRs with findings on their head · one PR with its files, every review and the latest findings | Read-only |
| `recent_pushes` | Pushes of the last days with their commits and CI, plus every check currently broken | Read-only |
| `docs` | The generated documentation: list, read one, or full-text search | Read-only |

The system prompt explains how every table is collected, tells the model to cite SHAs, PR numbers and dates, and tells it that commit messages and diffs are data, not instructions. Each answer lists the lookups it made. Your conversation lives in the browser tab (session storage); only the text of earlier turns is sent back with each question (the last 20 messages).

### 1.10 The LLM settings

Admins choose in **Settings**:

| Field | Notes |
|---|---|
| API type | **Claude-compatible** (Anthropic Messages API, via the official `anthropic` SDK) or **OpenAI-compatible** (Chat Completions, via the official `openai` SDK). The second covers OpenAI, Azure/OpenRouter-style gateways, vLLM, Ollama, LM Studio and similar. |
| Base URL | Blank = the provider's own API. Otherwise your gateway or server (OpenAI-compatible servers usually end in `/v1`). |
| API key | Stored encrypted in MongoDB, never returned to the browser (shown as "saved, ends in 1234"); blank keeps the saved key. |
| Model | Default `claude-opus-5-5`. |
| Effort | Claude only (`low` … `max`, default `high`); choose "(don't send)" for models or servers that reject it. |

**Test connection** sends one tiny request with the form's values before you save. When the API type is Claude and the base URL is Anthropic's own API, requests to Claude Opus 5.5 / Opus 5 / Sonnet 5.5 / Fable 5.1 use **server-side refusal fallback** (`fallbacks: "default"`). If a safety classifier declines a request (for example, a security-heavy diff), Anthropic re-runs it on a fallback model instead of returning a refusal.

**Cost:** one LLM call per 150k-character chunk of each PR update or default-branch push, and up to 10 calls per chatbot question.

### 1.11 Login and security

- **Sign in with GitHub is the way in.** "Continue with GitHub" runs the GitHub App's user authorization (OAuth web
  flow) with a one-time `state` (login CSRF, 10 minutes, used up on return) and **PKCE** (S256). Only after GitHub's
  answer is exchanged and stored does the portal make a session, so a failed sign-in leaves nothing half signed in.
- **Identity is GitHub's numeric user id**, never the login: renaming a GitHub account keeps the same portal user and
  workspaces, and someone who later takes the old name gets none of them. Login, name and avatar are refreshed at
  every sign-in. Memberships from before someone's first sign-in (the `ADMINS` list, the SQLite era) name a login and
  are claimed by the account that holds it at its first sign-in; from then on they follow the id. Accounts are never
  merged by email.
- **Identity is not repository access.** A GitHub App user token carries no OAuth scopes: it can reach only
  repositories where the App is installed *and* the user has access. Installing the App is the separate consent step
  for private repositories. The user's token (stored encrypted, refreshed when it expires after 8 hours) is used to
  list their repositories and to check, before a repository is connected, that they can read it.
- **First visit:** sign in → create a workspace (or open an invite) → pick its repositories (or skip) → the workspace.
  No workspace page or API answers before sign-in and membership.
- **Session:** a cookie signed with `SESSION_SECRET`, HTTP-only, `SameSite=Lax`, `Secure` when `PUBLIC_URL` is https,
  valid for 7 days. Cookie-authenticated changes from another site are refused (`Origin`/`Sec-Fetch-Site`).
  **Settings → Your GitHub account → Disconnect** revokes the App's authorization on GitHub, deletes the stored tokens
  and ends every session of that person.
- **Every page and API needs a login**, except `/auth/*`, `/healthz`, `/invite/*` (which sends you to sign in), `/mcp`
  (its own token), `POST /api/v1/auth/token` (client credentials) and the webhook (which needs a valid signature).
- **Owners and admins** of a workspace are the only ones who can change its Settings, test the LLM, invite people,
  connect or remove repositories, manage API clients and integrations, or trigger sync, rebuild and review. Members see
  everything in the workspace. Every check happens on the server, per request.
- **Programs are not people.** API clients (1.16) authenticate with their own credentials and get one-hour tokens
  limited to the scopes an admin gave them in one workspace; a GitHub token is never accepted in their place. The one
  bridge is the `portal:handoff` scope, which an admin must grant explicitly: it lets the client make a single-use
  sign-in link for a member of its workspace, with that member's role, so grant it only to services you trust as much.
- **Tenants can't reach the server's insides:** a workspace's LLM base URL and integration addresses must be public
  (or the local model), so nobody can point them at cloud metadata, MongoDB or other containers
  (`ALLOW_PRIVATE_LLM=1` / `ALLOW_PRIVATE_URLS=1` lift that on a server one trusted team uses).
- **Secrets:** the GitHub App private key is a mounted file (`secrets/github-app.pem`, read-only). GitHub user tokens,
  LLM keys, GG-Ops keys and the legacy MCP token are stored **encrypted** (Fernet) with
  `TOKEN_ENCRYPTION_KEY`, which is not in the database; none is ever sent back to a browser. Invite links, API client
  secrets, access tokens and login handoffs are stored only as SHA-256 digests. The container runs as a non-root user.
- **Audit:** sign-ins, tokens issued and refused, clients created and revoked, repositories connected and
  disconnected, integration changes and handoffs go to the `audit_events` collection (kept two years, never secrets).
- **Untrusted text** (diffs, commit messages, review text) is shown escaped. Chatbot answers are rendered as Markdown and sanitised with DOMPurify.
- `DEV_USER` (automatic login for local development) only works when GitHub login is **not** configured and only for requests from the same machine.

### 1.12 Data model

One MongoDB database (`MONGODB_URI`, default database `gg-docs`; give the portal its own, not GG-Ops's). Tenant data
carries `workspace_id`, and the code reaches it only through a wrapper that adds the current workspace to every query,
write and pipeline; outside a workspace it fails instead of guessing. Identifiers the API and URLs use are the ones
SQLite had: workspaces are integers, and reviews, findings, pushes, breakages, doc versions, events and invites keep an
integer `id` (counters continue after them). Timestamps of synced data stay ISO-8601 strings as GitHub and git give
them, so the API answers exactly as before; the new auth collections use dates, for their TTL indexes.

| SQLite (before) | MongoDB collection | Unique key / indexes | Notes |
|---|---|---|---|
| `control.users` | `users` + `github_accounts` | `github_id` unique · `user_id` unique | Keyed by GitHub's numeric id; tokens only in `github_accounts`, encrypted. Not migrated: everyone signs in again |
| `control.workspaces` | `workspaces` | `_id` (the old id) · `mcp_token_hash` unique | MCP token encrypted, looked up by digest |
| `control.members` | `members` | (workspace, user) unique; (workspace, login) unique while pending | Migrated rows are *pending* until that login first signs in |
| `control.invites` | `invites` | `token_hash` unique · `id` unique | |
| `control.repos` | `repos` | (workspace, lower-cased repo) unique | Plus GitHub id, owner, visibility, default branch, URL, access `status` and when it was checked |
| `settings` | `settings` | (workspace, key) unique | `llm_api_key` encrypted |
| `identities` | `identities` | (workspace, email) unique | |
| `commits` | `commits` | (workspace, repo, sha) unique · (workspace, person, committed_at) | |
| `pushes` + `push_commits` | `pushes` | (workspace, repo, branch, before, after) unique · (workspace, id) unique | `commit_shas[]` (at most 250) replaces `push_commits` |
| `pulls` | `pulls` | (workspace, repo, number) unique | |
| `checks` | `checks` | (workspace, repo, sha, name) unique | |
| `breakages` | `breakages` | (workspace, repo, branch, check_name, broken_sha) unique | |
| `reviews` / `findings` | `reviews` / `findings` | (workspace, repo, pr, branch, head_sha) unique / (workspace, review_id) | |
| `sonar_issues` | (none) | | Not migrated: SonarQube was removed |
| `docs` / `doc_versions` | `docs` / `doc_versions` | (workspace, repo, slug) unique / (workspace, repo, slug, id) | |
| `docs_fts` | (none) | | A search index; search reads `docs` |
| `events` | `events` | (workspace, id) unique | The job log, last 5,000 per workspace |
| new | `api_clients`, `access_tokens` (TTL), `login_handoffs` (TTL), `integrations`, `workflows`, `audit_events` (TTL 2 years), `rate_limits` (TTL), `counters`, `migrations` | | 1.16 |

### 1.13 JSON API

Everything the pages show comes from these endpoints (login required):

| Endpoint | Returns |
|---|---|
| `GET /api/summary` | CI status of each default-branch head, open breakages, worker queue, whether GitHub and the LLM are configured |
| `GET /api/activity?repo=&branch=&person=&days=30&limit=100` | Pushes (with their commits, CI checks, breakages, reviews) and commits |
| `GET /api/people?days=90` · `GET /api/people/{person}?days=90` · `GET /api/people/{person}/tasks` | The People table · one person's page: profile, commits per day and per repository, PRs, breakages, findings · their tasks in the linked GG-Ops projects, read with the viewer's GG-Ops access |
| `GET /api/breakages?days=90` | Breakages in the period plus every open one |
| `GET /api/reviews` · `GET /api/reviews/{id}` | Reviews with counts · one review with its findings |
| `GET /api/events` | The last 100 job runs |
| `GET /api/docs` · `GET /api/docs/{repo}/{slug}` (`?download=1` for the .md) · `GET /api/docs/{repo}/zip` | Generated documents with their status · one document · a repository's set as a .zip |
| `GET /api/docs/search?q=` · `/api/docs/{repo}/history/{slug}` · `/api/docs/{repo}/diff/{version}` | Full-text search · versions · what a version changed |
| `POST /api/admin/docs` `{"repo", "slug"?}` · `/remove_repo` `{"repo"}` | Rewrite documents · stop syncing a repository (admin) |
| `GET /api/me` · `POST /api/workspaces` `{"name"}` · `POST /api/workspaces/{id}/switch` · `PUT /api/workspace` `{"name"}` | Who you are and your workspaces · create one · open one · rename (admin) |
| `GET /api/workspace/members` · `PUT`/`DELETE /api/workspace/members/{login}` `{"role"}` | Members and pending invites · change a role or remove someone (admin; leaving is yours) |
| `POST /api/workspace/invites` `{"email", "role"}` · `DELETE /api/workspace/invites/{id}` | Invite by email (admin; returns the link too) · revoke |
| `GET /invite/{token}` · `GET /api/invites/{token}` · `POST /api/invites/{token}/accept` | The emailed link (sign in, then the Join page) · what it is for · join |
| `GET /api/import/repos` · `POST /api/import` `{"url"}` | Repositories you can import through the App · import one by URL (admin) |
| `GET /api/settings/local` · `GET /api/settings/mcp` | Models downloaded into the local Ollama · MCP connection details (admin) |
| `POST /api/settings/mcp/rotate` · `PUT /api/settings/mcp` `{"legacy": bool}` | A new legacy MCP token · turn it off or on (admin) |
| `GET /healthz` | `{"ok", "database"}`; 503 when MongoDB is unreachable |
| `POST /mcp` | The MCP server (1.14) |
| `POST /api/chat` `{"history": [...], "question": "..."}` | `{"answer", "steps"}` |
| `GET /api/settings` · `PUT /api/settings` · `POST /api/settings/test` | Settings (secrets masked) · save (admin) · test (admin) |
| `POST /api/admin/sync` · `/rebuild` · `/review` `{"repo", "pr"}` | Queue a job (admin) |

Programs use the API v1 instead (1.16).

### 1.14 The MCP server (coding agents)

`/mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server over Streamable HTTP, so any MCP client can
use the portal: Claude Code, Codex, ChatGPT (developer mode), Mistral Le Chat, Cursor, Gemini CLI and others. It offers
the chatbot's tools (1.9) with larger results: PRs being worked on and updated, reviews and findings, pushes and CI,
breakages, people, the code graph, and the generated documentation. Its instructions explain every table.

- **Stateless:** every request is one JSON-RPC call answered with JSON, so there are no sessions to lose. The portal's
  own tools are read-only.
- **Auth** decides who the agent is. Each member has a **personal MCP token** per workspace (Settings → Connect a
  coding agent; made on first view, replaced with "New token", gone when they leave the workspace): an agent using it
  *is* that person, as `Authorization: Bearer <token>` or in the URL (`/mcp/<token>`) for clients that only take a
  URL, such as ChatGPT and Le Chat (the URL is then a secret). The workspace's **legacy MCP token** (same two forms;
  admins can replace it or turn it off) and an **API client** with the `mcp:read` scope (1.16, a one-hour token, never
  in the URL) are the workspace, with no person. Every token is stored encrypted and looked up by its digest.
- **GG-Ops's tools, as the person.** An agent on a personal token whose person has connected their GG-Ops account
  (1.16) also gets GG-Ops's own MCP tools, named `ggops_*` (list workspaces, projects and members, search tasks, read a
  board, create, update, complete and comment on tasks, task drafts, DSRs). `tools/list` asks GG-Ops's MCP server
  (`/api/mcp`) with that person's token, so GG-Ops shows only what their grant allows, and `tools/call` runs the tool
  there as them: tasks are created as that person and GG-Ops sends its usual emails (task assigned, mentions,
  comments). A call is sent once and never retried blind; only a token GG-Ops refuses is refreshed and the call sent
  again. A connection made when the portal asked GG-Ops for `read` only has no write tools until the person chooses
  **Allow task changes** (connect again). Workspace and API-client tokens get no GG-Ops tools.
- **Settings → Connect a coding agent** shows the exact command or URL for each client, for example:
  ```bash
  claude mcp add --transport http gg-pulse https://<domain>/mcp --header "Authorization: Bearer <token>"
  codex mcp add gg-pulse --url https://<domain>/mcp/<token>
  ```

### 1.15 Workspaces, teams and importing repositories

**Workspaces.** Everyone who signs in can create workspaces and belong to several; the menu at the top right switches
between them. Each workspace has its own data in MongoDB (1.12) and its own folder, `/data/workspaces/<id>/`, with
its clones (`repos/`), architecture views (`site/`, `work/`) and venvs. A request runs inside the workspace your session points at, a
background job inside the workspace that queued it, and an MCP call inside the workspace its token belongs to; code
that reads a workspace's data outside one fails instead of guessing. So one team can never see another's commits,
reviews, docs or settings, including through Ask's and MCP's query tool.

| Role | Can |
|---|---|
| **owner** | everything, including making, unmaking and removing owners. A workspace always keeps at least one. |
| **admin** | invite and remove members and admins, import and remove repositories, Settings, sync, rebuild, review |
| **member** | see everything in the workspace, ask questions; change nothing |

**Invites (Settings → Team).** An owner or admin enters an email address and a role. The portal emails a link through
`SMTP_HOST` (any provider: SES, SendGrid, Resend, Postmark, Mailgun) and also shows it, so it can be sent another way;
without SMTP it is only shown. Opening the link asks the person to sign in with GitHub, then to confirm with a Join
button (so no other website can quietly sign someone into a workspace), and adds them with that role.
A link works **once**, for **7 days**, is stored only as a hash, and can be revoked while it waits. At most 50 invites
a day per workspace, so the server's mail can't be used for spam.

**Connecting repositories (Import, `/new/`; right after creating a workspace it is the onboarding step)**:

1. Pick any number from **Your GitHub repositories**: every repository your GitHub account reaches through the App's
   installations (every page of them), newest push first, marked as *yours*, an *organization*'s, or one you are a
   *collaborator* on, with visibility, default branch and whether it is already connected; search and filter them. Or
   paste a URL (`https://github.com/owner/name`, with `.git` or a `/tree/...` path, `git@github.com:owner/name.git`,
   or `owner/name`). Onboarding can be skipped: a workspace without repositories works.
2. The portal asks GitHub **with your own token** whether you can read it. Many teams share the one App, so the App
   being able to read a repository is not enough: you must be able to as well. Then it checks that the App is
   installed on it (it clones it, reads CI and sends its webhooks). If either is missing you get an **Install the
   GitHub App** button; GitHub sends you back to Import afterwards (the App's Setup URL).
3. The repository is cloned into the workspace, its history, PRs and CI are read, and its documentation is written.
4. From then on every push, PR and check run arrives by webhook. One App delivers every tenant's webhooks; each
   delivery goes to every workspace that imported that repository. The 5-minute poll backs them up.

Removing a repository stops syncing it; its history and clone stay, and importing it again picks them back up. Two
repositories with the same name can't share a workspace (one clone folder and docs URL per name).

**Access is re-checked, not assumed.** Every sync first asks GitHub, with the App's token, for the repository: its
metadata is refreshed, and if the App lost access the repository is marked **revoked** and skipped (its data stays),
until access returns. **Settings → Repositories → Check access** re-checks every connected repository with your own
GitHub access, showing why any of them can't be reached and how to fix it.

**Architecture views** (1.5) need a FastAPI backend and a Next.js frontend paired up, so only a
workspace upgraded from a single-tenant install has them. Every workspace gets the generated docs (1.5a).

**Upgrading from SQLite.** Installs from before MongoDB keep their data in SQLite files; the portal refuses to start
until they are migrated with `python -m portal.migrate` (6.1). A single-tenant install (`/data/portal.db`, from before
workspaces) becomes a workspace named after `GITHUB_ORG`, with `BACKEND_REPO`, `FRONTEND_REPO`, `EXTRA_REPOS` and the
repositories added in Settings, its architecture views, and `MCP_TOKEN` as its MCP token, so connected coding agents
keep working. The GitHub logins in `ADMINS` own it; they invite everyone else.

### 1.16 API v1, API clients, login handoff and integrations

For programs (CI jobs, automations, other services). Every endpoint checks, on the server and per request, who is
calling, which workspace they act in and whether they hold the scope; a workspace the caller has no part in answers
404. Errors are `{"error": "..."}`. A signed-in person can call the same endpoints with their session (owners and
admins hold every scope, members the read ones).

| Endpoint | Scope | What |
|---|---|---|
| `POST /api/v1/auth/token` | (client credentials) | OAuth 2.0 client credentials, JSON or form: `grant_type=client_credentials`, `client_id`, `client_secret`, optional `scope` (subset of the client's, space separated or a list) and `workflow_id`. Returns `{access_token, token_type: "Bearer", expires_in: 3600, scope, workflow_id}`. RFC 6749 errors: `invalid_client` (401), `invalid_scope`, `invalid_request`, `unsupported_grant_type`, `slow_down` (429) |
| `GET /api/v1/auth/me` | any | The caller: `{user: {id, login, github: {id, login}}, workspace: {id, name, role, permissions}}` for a person, `{client, workspace, workflow_id, expires_at}` for a client |
| `POST /api/v1/auth/logout` | any | Revokes the caller's access token, or ends the session |
| `POST /api/v1/auth/login-handoff` | `portal:handoff` | `{next?}` (a person: for themselves) or `{github_id, next?}` (a client: a member of its workspace) → `{url, expires_in: 60}`. `GET /auth/handoff?code=…` redeems it once, atomically, into a normal session (a client-made one stays in the client's workspace) and redirects to `next`, which must be a page of the portal |
| `GET /api/v1/github/repositories?q=&access=owner\|organization\|collaborator&page=&per_page=` | a person | Their GitHub repositories through the App, with `access`, `visibility`, `default_branch`, `connected` |
| `POST /api/v1/github/disconnect` | a person | Revoke the App's authorization on GitHub, forget the tokens, end every session |
| `GET /api/v1/workspaces/{id}/repositories` | `repository:read` | Connected repositories with metadata, `status` (`active`, `revoked`, `unknown`) and when they were checked |
| `POST /api/v1/workspaces/{id}/repositories` `{"repositories": [...]}` | `repository:write`, a person | Connect up to 50, each checked against the person's own GitHub access; one result each |
| `POST /api/v1/workspaces/{id}/repositories/refresh` | `repository:write`, a person | Re-check every connected repository (1.15) |
| `DELETE /api/v1/workspaces/{id}/repositories/{repositoryId}` | `repository:write` | Disconnect (its data stays) |
| `GET /api/v1/integrations` | `integration:read` | GG-Ops and gg-pulse status (never keys) and the linked workflows |
| `PUT` / `DELETE /api/v1/integrations/{gg-ops\|gg-pulse}` `{"base_url", "api_key"}` | `integration:write` | Connect or disconnect a service |
| `POST /api/v1/integrations/connections/verify` `{"service"}` | `integration:read` | Check it against the real service now: `{status, service, verifiedAt, detail}` |
| `POST /api/v1/integrations/workflows` `{"workflow_id", "name"?}` · `DELETE …/workflows/{id}` | `integration:write` | Link or unlink a GG-Ops project |
| `GET /api/v1/integrations/workflows/{workflowId}` | `workflow:read` | The project's status and task counts, read now through GG-Ops |
| `GET` / `POST /api/v1/clients` · `DELETE /api/v1/clients/{client_id}` | `client:write` (admins) | List, create (`{name, scopes, workflow_id?}`, the secret is shown once) and revoke API clients |

**API clients** (Settings → API clients) are registered by an owner or admin with explicit scopes:
`repository:read`, `repository:write`, `workflow:read`, `integration:read`, `mcp:read`, `portal:handoff`. A client may
be bound to one workflow, and then only sees and asks for that one. Client secrets and access tokens are 256-bit
random strings stored only as digests; tokens live an hour (expired ones are refused at once and swept by a TTL
index), and revoking a client ends its tokens immediately. Token requests are limited to 20 a minute per client and
60 per address.

```bash
TOKEN=$(curl -s https://<domain>/api/v1/auth/token -H 'Content-Type: application/json' \
  -d '{"grant_type":"client_credentials","client_id":"ggd_ci_…","client_secret":"ggd_cs_…","scope":"repository:read"}' | jq -r .access_token)
curl -s https://<domain>/api/v1/workspaces/1/repositories -H "Authorization: Bearer $TOKEN"
```

**GG-Ops** (the team's project-management app). An admin enters its address and a **GG-Ops API key** (GG-Ops →
Account → API keys, `ggops_sk_…`; read-only is enough). The portal calls GG-Ops's REST API v1 with it, server-side:
`GET /api/v1/workspaces` to verify the key, `GET /api/v1/projects/{id}/tasks` for a project. A **workflow** is a GG-Ops
project linked to the workspace by its id. The id is an identifier, not a credential: it is accepted only once GG-Ops
shows that project to the workspace's key, every read goes through that key (so GG-Ops's own membership rules decide),
and knowing an id gets a caller nothing unless it is linked to their workspace. Reads return the project's status and
task counts, not task contents. Timeouts, bounded retries (3 attempts on connection errors, 429 and 5xx), and a
revoked key shows up as status `unauthorized` instead of failing the page.

**Your own GG-Ops account.** With `GGOPS_URL` and `GGOPS_CLIENT_ID` set, the portal is an OAuth client of GG-Ops
(PKCE, scope `read write`, no client secret) and people connect their own account: Settings → Your GG-Ops account, or
GG-Ops's "Open in GG-Docs" link to `/connect/ggops`. A person's workflow reads then go out with their own token, so
they see what GG-Ops shows *them*, and someone who hasn't connected sees no GG-Ops data; API clients act as no person
and keep the workspace's key. Tokens are stored encrypted per person; an access token is refreshed a minute before it
expires, one refresh at a time per person and never retried blind (GG-Ops revokes a connection whose old refresh
token is used twice). Revoked in GG-Ops, or unused for 90 days, the person is asked to connect again. Register the
portal once in GG-Ops: `curl -X POST https://<ggops>/oauth/register -H 'content-type: application/json' -d
'{"client_name":"GG-Docs","redirect_uris":["https://<domain>/connect/ggops/callback"]}'`.

| Endpoint | Who | What |
|---|---|---|
| `GET /connect/ggops` · `GET /connect/ggops/callback` | a signed-in person | Connect (consent in GG-Ops); straight to Settings when connected |
| `GET /api/v1/integrations/gg-ops/me` | a signed-in person | Their connection and the GG-Ops workspaces it sees |
| `DELETE /api/v1/integrations/gg-ops/me` | a signed-in person | Disconnect: forgotten here and revoked in GG-Ops |

**gg-pulse.** Its only HTTP API is `GET /api/health` (no authentication), so the integration is a connectivity check:
`connected`, `degraded` (gg-pulse is up, its database isn't) or `unavailable`. It has no analytics API to read yet, so
the portal shows none.

---

## 2. Using the portal

- **Docs**: the generated documentation of every repository (1.5a), with search, history, and .md / .zip / PDF export.
- **Overview** (`/`): architecture views for each repo (module graph, module tree, call flow, import map, API reference, dependency rules, graph report), every route, live CI status of each default branch, and a Markdown download.
- **Activity**: a red banner for every CI check currently broken on a default branch (who and since when), then pushes (pusher, branch, force-push flag, commits, CI result on the head, "broke X" badges, review link), then commits. Filter by repo, period and person.
- **People**: the table from 1.7. Click a column to sort, or a person for their own page (`/people/person/?p=<login>`):
  a contribution calendar, commits per repository, their pull requests, CI breakages and review findings, and the
  tasks assigned to them in the workspace's linked GG-Ops projects (matched by the GG-Ops account they connected, else
  by name; read with your own GG-Ops access, so you see only tasks GG-Ops shows you).
- **Reviews**: every review with counts, and each finding with severity, category, file and line (linked to GitHub) and who wrote the line. Admins can **review a PR now** by number.
- **Ask**: type a question or click an example. Good questions:
  - "Who pushed to v2-accelerated this week, and what did each push change?"
  - "Whose push last broke v2-module-tests, and which commit fixed it?"
  - "Rank people by weighted review findings per 1k lines over the last 90 days, with the numbers."
  - "Which PRs had critical findings this month, and are they merged?"
  - "What does services/vmis depend on, and which routes use it?"
  - "Show me the slop findings in Shrinjay's last 3 PRs."
- **Settings**: LLM, PR posting, "Sync now" and "Rebuild views now", connection status, and the job log.

---

## 3. Setting it up

### 3.1 Create the GitHub App (once, by an org owner)

GitHub → **your-org** → Settings → Developer settings → GitHub Apps → **New GitHub App**:

| Field | Value |
|---|---|
| Name | `gg-pulse` (comments appear as `gg-pulse[bot]`) |
| Homepage URL | your `PUBLIC_URL` |
| Callback URL | `PUBLIC_URL/auth/callback` (the only one; sign-in uses PKCE and asks for no OAuth scopes) |
| Webhook → Active, URL | `PUBLIC_URL/webhook/github` |
| Webhook secret | a long random string (`openssl rand -hex 32`); the same value goes in `GITHUB_WEBHOOK_SECRET` |
| Repository permissions | Contents: **Read** · Metadata: **Read** · Pull requests: **Read and write** · Checks: **Read** |
| Account permissions | none needed |
| Subscribe to events | **Push**, **Pull request**, **Check run**, **Check suite** |
| Setup URL | `PUBLIC_URL/new/`, with **Redirect on update** ticked (people land back on Import after installing) |
| Where can this app be installed | **Any account**: every team installs it on its own repositories |

Then, on the app's page:

1. Copy the **App ID** and **Client ID**, and **generate a client secret**.
2. **Generate a private key**; save the downloaded `.pem` as `secrets/github-app.pem` next to `docker-compose.yml`.
3. **Install App** → your-org → *Only select repositories* → `backend` and `frontend`.

"Pull requests: write" is only used to post reviews, and only when an admin turns that on. Why each permission:
Contents (clone and read code), Metadata (repository details), Checks (CI results for breakages), Pull requests (PRs to
review, and posting reviews). Signing in reads only the public profile (`GET /user`), which needs no account
permission. Leave **Expire user authorization tokens** on (the default): user tokens then last 8 hours and are
refreshed, and **Settings → Your GitHub account → Disconnect** revokes them through `DELETE /applications/{client_id}/grant`.

### 3.2 Install on a cloud VM: one command

On a fresh Ubuntu or Debian VM (2 vCPU, 8 GB RAM, 40 GB disk), with TCP **80 and 443 open to everyone** in the cloud firewall:

```bash
git clone https://github.com/CR-8/gg-docs.git ~/gg-pulse && cd ~/gg-pulse
bash install.sh <vm-public-ip>          # e.g. bash install.sh 35.253.245.77
```

`install.sh` does everything, and is safe to re-run:

1. Installs Docker and the Compose plugin when they're missing.
2. Makes sure containers can resolve names (if they can't, points Docker at public DNS).
3. Pulls the latest code.
4. Writes `.env`:
   - `PUBLIC_URL` and `DOMAIN` use **`<ip-with-dashes>.sslip.io`** (e.g. `35-253-245-77.sslip.io`), a free DNS name that points at your IP, so Caddy can get a real HTTPS certificate without you owning a domain.
   - Port 7000 is bound to localhost.
   - `SESSION_SECRET`, `TOKEN_ENCRYPTION_KEY` and `GITHUB_WEBHOOK_SECRET` are generated once and kept on later runs.
5. Starts the stack with HTTPS and waits until it answers.
6. Prints the portal's address and anything still needed.

**Then add the GitHub App** (see "GitHub access" just below): people sign in with it, and it clones the repositories
they import and sends their webhooks.

#### GitHub access

| Set | What works |
|---|---|
| **GitHub App** (`GITHUB_APP_*` + key, 3.1) | Everything: Continue with GitHub, workspaces, importing any repository the App is installed on (each team installs it on its own), instant updates through webhooks, reviews posted on PRs |
| `GITHUB_TOKEN` only | One operator's setup or local development: nobody can sign in except `DEV_USER` on the machine itself; every repository is read with that token and polled every 5 minutes |
| Neither | The portal runs, `DEV_USER` can sign in locally and import public repositories or local folders |

#### Local model (optional)

Reviews and Ask can run on a coder model hosted on the VM itself, so no API key is needed and no code leaves the server:

```bash
LOCAL_LLM=auto bash install.sh <vm-public-ip>
```

This starts [Ollama](https://ollama.com) as the `llm` service (reachable only by the portal), downloads **Qwen2.5-Coder Instruct in Q8 GGUF** (skipped when it is already there, so re-runs never download it again; an interrupted download resumes on the next run), and points the portal at it (`http://llm:11434/v1`, OpenAI-compatible). `auto` picks the size by the VM's RAM, leaving room for the portal and MongoDB:

| VM RAM | Model | Model memory |
|---|---|---|
| 16 GB or more | `qwen2.5-coder:7b-instruct-q8_0` | about 8 GB |
| 12 GB | `qwen2.5-coder:3b-instruct-q8_0` | about 3.5 GB |
| less | `qwen2.5-coder:1.5b-instruct-q8_0` | about 2 GB |

Choose one yourself with `LOCAL_LLM=qwen2.5-coder:7b-instruct-q8_0` (any Ollama tag works), and switch it off with `LOCAL_LLM=off`.

How the portal adapts to a small local model:

- The context is set to 16k tokens (`LLM_CONTEXT`).
- Diffs are reviewed in requests of about 37k characters, at most about 150k characters per review (`REVIEW_CHUNK_CHARS`, `REVIEW_MAX_CHARS`). Anything larger is listed as "not reviewed".
- The chatbot's tool results are capped at 6,000 characters (`CHAT_TOOL_CHARS`).
- Requests may take up to an hour (`LLM_TIMEOUT`).

**Speed:** on CPU only, expect tens of seconds per chat answer and a few minutes per review. A small model also follows the chatbot's tool instructions less reliably than a hosted one: the 7B is noticeably better than the 1.5B.

**Already downloaded models.** Settings → Language model → **Local model** lists every model in that Ollama (name, size,
quantisation): pick one and save, no URL or key needed. If the VM already has Ollama installed on the host
(`/usr/share/ollama/.ollama` or `~/.ollama`), `install.sh` mounts that model store into the `llm` service instead of a
fresh volume (`OLLAMA_DIR`), so models you pulled before are used as they are. Any OpenAI-compatible server of your own
(llama.cpp, vLLM, LM Studio) works without an API key too.

Settings can override all of this at any time. Pointing it at a hosted model (Claude or OpenAI) or at your own llama.cpp server (`llama-server --jinja`, which serves the same OpenAI-compatible API) takes effect immediately.

#### Adding the GitHub App

1. Create the GitHub App (3.1) with the URLs the script printed.
2. Copy its private key to the VM: `scp <key>.pem <user>@<vm-ip>:~/gg-pulse/app.pem`
3. Re-run with the App's values:
   ```bash
   GITHUB_APP_ID=... GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=... \
   APP_KEY=~/gg-pulse/app.pem bash install.sh <vm-public-ip>
   rm ~/gg-pulse/app.pem      # the script copied it into secrets/
   ```

Keep the VM's public IP **static** (an Elastic IP on AWS, or "reserve static address" on GCP). If it changes, the sslip.io name changes with it; re-run the script and update the App's URLs. To use a real domain instead, point it at the VM and run `DOMAIN=portal.example.com bash install.sh <ip>`.

Step by step on a Google Cloud VM you reach over SSH, with GG-Ops on the same VM if you want it: [DEPLOY-GCP.md](DEPLOY-GCP.md).

The manual steps below do the same thing by hand.

### 3.2.0 Install on a cloud VM (manual steps)

Works on any Linux VM; the commands below are for **Ubuntu 24.04 LTS**. Size: **2 vCPU, 8 GB RAM, 40 GB disk** (the backend venv for the route export takes about 3 GB of disk). For example, AWS EC2 `t3.large` with a 40 GB gp3 volume, GCP `e2-standard-2`, or Azure `B2ms`.

**1. Create the VM, network and DNS**

- Inbound firewall / security group: **22** (SSH, your IP only), **80** and **443** (everyone, for HTTPS and GitHub webhooks). Don't open 7000.
- Give the VM a fixed public IP (an Elastic IP on AWS) and create a DNS **A record**, e.g. `portal.example.com` → that IP.
- Create the GitHub App (3.1) with `https://portal.example.com` as the base of its callback and webhook URLs.

**2. Install Docker** (SSH in as `ubuntu`)

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER    # then log out and back in, so docker works without sudo
```

**3. Get the code**

```bash
git clone https://github.com/CR-8/gg-docs.git ~/gg-pulse && cd ~/gg-pulse
```

If the repository is private, give the VM a read-only deploy key first:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gg-docs -N "" && cat ~/.ssh/gg-docs.pub
# add that public key under github.com/CR-8/gg-docs → Settings → Deploy keys (read-only), then:
GIT_SSH_COMMAND='ssh -i ~/.ssh/gg-docs' git clone git@github.com:CR-8/gg-docs.git ~/gg-pulse && cd ~/gg-pulse
git config core.sshCommand 'ssh -i ~/.ssh/gg-docs'   # so later `git pull` uses the key too
```

**4. Secrets and settings**

```bash
mkdir -p secrets && chmod 700 secrets
# from your own machine, copy the GitHub App private key up:
#   scp gg-pulse.*.private-key.pem ubuntu@<vm-ip>:~/gg-pulse/secrets/github-app.pem
chmod 600 secrets/github-app.pem
cp .env.example .env
openssl rand -hex 32    # paste as SESSION_SECRET
nano .env
```

Fill in at least:

```ini
PUBLIC_URL=https://portal.example.com
DOMAIN=portal.example.com
BIND_ADDR=127.0.0.1
GITHUB_APP_ID=...
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
GITHUB_WEBHOOK_SECRET=...          # same value as in the GitHub App
ADMINS=your-github-login          # upgrading a single-tenant install only: owners of its workspace (1.15)
SESSION_SECRET=...
```

The portal container runs as user id 1000, which is the default `ubuntu` user on most images. If its log shows "Permission denied" for the key, run `sudo chown 1000 secrets/github-app.pem`.

**5. Start**

```bash
docker compose --profile https up -d --build     # first build takes a few minutes
docker compose ps                                # portal, mongo, caddy: all "running"
docker compose logs -f portal                    # Ctrl-C stops following, not the portal
```

Caddy gets a Let's Encrypt certificate for `DOMAIN` on first start (DNS must already point at the VM, and ports 80/443 must be open).

**6. Check it**

- `https://portal.example.com` → **Sign in with GitHub**.
- GitHub App → Advanced → **Recent deliveries**: the ping and later pushes show `200`.
- Settings → Recent jobs: `sync` entries, then `rebuild` after the first build.

**7. Keep it running and up to date**

- Everything restarts on its own after a reboot (`restart: unless-stopped`, Docker enabled at boot).
- Update: `cd ~/gg-pulse && git pull && docker compose --profile https up -d --build`
- Back up the database and the data volume (clones, views, and the encryption key file if `TOKEN_ENCRYPTION_KEY`
  isn't set):
  ```bash
  docker compose exec -T mongo mongodump --archive --gzip --db gg-docs > gg-docs-$(date +%F).archive.gz
  docker run --rm -v gg-portal_portal_data:/d -v $PWD:/b alpine tar czf /b/portal-data-$(date +%F).tgz -C /d .
  ```

### 3.2.1 Access by IP address (no domain)

Use this until you have a domain. It's the same install as above, with these differences:

**Network.** In the security group, open **7000** (portal) instead of 80/443, **to your office or VPN IP ranges only**. These ports are plain HTTP, so anyone on the path can read login cookies. Set the rules in the cloud firewall (security group): Docker publishes ports around `ufw`, so `ufw` rules don't protect them. Give the VM an **Elastic/static IP**, because the GitHub App's URLs contain it.

**GitHub webhooks (optional).** To get pushes and PR reviews within seconds instead of within 5 minutes, also allow GitHub's webhook senders on port 7000:
```bash
curl -s https://api.github.com/meta | python3 -c "import json,sys; print('\n'.join(json.load(sys.stdin)['hooks']))"
```
Add each printed range as an inbound rule for TCP 7000. GitHub changes these ranges occasionally. Without them everything still works through the 5-minute poll (pushes, CI results and PR reviews); only instant updates are lost.

**GitHub App URLs** (3.1), with your VM's IP:

| Field | Value |
|---|---|
| Homepage URL | `http://<vm-ip>:7000` |
| Callback URL | `http://<vm-ip>:7000/auth/callback` |
| Webhook URL | `http://<vm-ip>:7000/webhook/github` (leave SSL verification as is; it only applies to https) |

**`.env`** differences:
```ini
PUBLIC_URL=http://<vm-ip>:7000
BIND_ADDR=0.0.0.0
# leave DOMAIN empty
```

**Start without the https profile:**
```bash
docker compose up -d --build
```

Open `http://<vm-ip>:7000`. Updates: `git pull && docker compose up -d --build`.

**Adding HTTPS later without buying a domain** (this is what `install.sh` sets up): the free wildcard DNS service sslip.io maps `<ip-with-dashes>.sslip.io` to your IP. For example, `203-0-113-5.sslip.io` resolves to `203.0.113.5`, so Caddy can get a real certificate for it. To switch:
1. Set `DOMAIN=203-0-113-5.sslip.io` and `PUBLIC_URL=https://203-0-113-5.sslip.io`, plus `BIND_ADDR=127.0.0.1`.
2. Open 80/443 and close 7000.
3. Update the three GitHub App URLs.
4. Run `docker compose --profile https up -d --build`.


### 3.3 First run

1. Open `PUBLIC_URL`, **Continue with GitHub**, and create a workspace (or open the invite link someone sent you).
2. **Import** a repository. The first sync loads 90 days of history and writes the docs; watch it under Settings → Recent jobs. An upgraded install (1.15) also builds the views, which takes a few minutes plus a one-time install of the backend's requirements for the route export (about 10 minutes).
3. **Settings → Team:** invite the people who should see it.
4. **Settings → LLM:** choose the API type, base URL (blank for the provider's own API), key and model → **Test connection** → **Save**.
5. Read a few reviews in the portal. When you trust them, turn on **Post reviews on GitHub PRs**.

---

## 4. Integrating it into the backend repository

The portal doesn't need to live anywhere special: it clones both repositories from GitHub through the App wherever it runs. What changes is where the **code** lives and how it is **deployed**.

### Recommended: a subfolder of the backend repo, run as its own service

The code lives in `backend/tools/portal/`, versioned and reviewed like the rest of the backend, while it runs in its own containers next to (not inside) the API.

1. **Branch** from the default branch:
   ```bash
   cd backend
   git switch v2-accelerated && git pull
   git switch -c feat/engineering-portal
   ```
2. **Copy the portal in**, without local state (Windows PowerShell shown; `rsync -a --exclude …` on Linux/macOS):
   ```powershell
   robocopy D:\src\gg-pulse D:\src\backend\tools\portal /E /XD data site work .venv node_modules __pycache__ .pytest_cache secrets /XF .env *.log
   ```
   The folder brings its own `.gitignore`, so its `data/`, `.env`, `secrets/`, `.venv/` and `node_modules/` stay out of git.
3. **Nothing else in the backend changes.**
   - The backend's `.dockerignore` is an allowlist, so `tools/` never enters the backend image.
   - `pytest.ini` only collects `tests/`, so the backend's test run is unaffected.
   - The portal builds from its own folder (`build: .` in its compose file).
4. **CI for the portal itself** (optional): add `.github/workflows/portal.yml`:
   ```yaml
   name: Portal
   on:
     pull_request:
       paths: ["tools/portal/**"]
     push:
       branches: [v2-accelerated]
       paths: ["tools/portal/**"]
   jobs:
     test:
       runs-on: ubuntu-latest
       defaults: {run: {working-directory: tools/portal}}
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with: {python-version: "3.11"}
         - run: pip install -r requirements.txt pytest && python -m pytest tests
         - run: docker build -t gg-pulse .
   ```
   To publish the image to ECR like the backend does, copy the login and push steps from `ecr-build.yml`. You'll need a new ECR repository (for example `gg-pulse`), and the `GitHubActions-ECRBuild` role's trust policy must allow this workflow.
5. **Deploy** on the portal server:
   ```bash
   git clone https://github.com/your-org/backend.git && cd backend/tools/portal
   cp .env.example .env && mkdir secrets   # fill in .env, add secrets/github-app.pem
   docker compose --profile https up -d --build
   ```
   Updating: `git pull && docker compose up -d --build` in that folder.
6. **Behind your existing nginx** instead of Caddy (if it runs on the same host as `api.example.com`): leave out `--profile https`, set `BIND_ADDR=127.0.0.1`, and add a server block (certificate via certbot as for the API):
   ```nginx
   server {
       listen 443 ssl;
       server_name portal.example.com;
       # ssl_certificate ... (same as the api.example.com block)
       client_max_body_size 10m;
       location / {
           proxy_pass http://127.0.0.1:7000;
           proxy_set_header Host $host;
           proxy_read_timeout 300s;   # chatbot answers can take a while
       }
   }
   ```

### Also possible: add it to the backend's docker-compose.yml

If one host runs everything, add the portal and mongo services from `tools/portal/docker-compose.yml` to the backend compose file, with `build: ./tools/portal` and the same volumes. Only do this when the host has memory to spare for the portal's clones and builds.

### Not recommended: mounting it inside the backend's FastAPI app

It is technically possible (`app.mount("/portal", portal_app)`), but:

- **Dependencies clash.** The backend pins FastAPI 0.104 and its own Pydantic/Starlette; the portal pins newer ones.
- **Heavy jobs** (git clones, graphify, npm, backend venv installs) would compete with clinical-report requests.
- **Secrets mix.** The GitHub App key and LLM keys would sit in the same process as patient-data handling.
- **Every portal change would need a backend deploy.**

Keep them as separate services. To make the portal easy to find, link to it from the frontend or nginx instead.

---

## 5. Configuration reference

Set in `.env` (Docker) or the environment (local).

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_URL` | `http://localhost:7000` | The address people use; also the base for the OAuth callback and the links in PR reviews |
| `DOMAIN` | | Certificate domain for the `https` profile |
| `BIND_ADDR` | `0.0.0.0` | Interface for port 7000; `127.0.0.1` behind Caddy or nginx |
| `MONGODB_URI` | `mongodb://mongo:27017/gg-docs` in Docker, `mongodb://127.0.0.1:27017/gg-docs` otherwise | The database, with its name. Atlas: `mongodb+srv://…/gg-docs?retryWrites=true`. Its own database, not GG-Ops's |
| `MONGODB_TIMEOUT_MS` | `10000` | How long to wait for MongoDB before a request or the start fails |
| `TOKEN_ENCRYPTION_KEY` | a key file in `DATA_DIR` | Encrypts stored GitHub tokens and keys (`openssl rand -hex 32`). Back it up; without it they can't be read (people sign in again, admins re-enter keys) |
| `GITHUB_ORG`, `BACKEND_REPO` / `FRONTEND_REPO`, `EXTRA_REPOS`, `MCP_TOKEN`, `ADMINS` | | Read once, by `python -m portal.migrate`, to turn a single-tenant install into a workspace (1.15): its name, repositories, MCP token and owners |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | port `587` | Email for invites (587 STARTTLS, 465 TLS). Blank host: links are copied by hand |
| `ALLOW_PRIVATE_LLM` / `ALLOW_PRIVATE_URLS` | | `1` lets workspaces point their LLM / their LLM and integrations at private addresses (one trusted team only) |
| `GITHUB_APP_ID` | | App ID |
| `GITHUB_APP_PRIVATE_KEY_FILE` | `/secrets/github-app.pem` in Docker | App private key file (or `GITHUB_APP_PRIVATE_KEY` with the PEM inline) |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | | The App's OAuth credentials, for "Continue with GitHub" (required to sign in) |
| `GITHUB_WEBHOOK_SECRET` | | Must match the App's webhook secret; webhooks are refused without it |
| `SESSION_SECRET` | random per start | Signs login cookies; set it, or everyone is logged out on restart |
| `POLL_SECONDS` | `300` | Poll interval (backup to webhooks) |
| `BACKFILL_DAYS` | `90` | History loaded and kept for commits |
| `DEFAULT_BRANCHES` | from GitHub | JSON override, e.g. `{"your-org/backend": "v2-accelerated"}` |
| `GIT_URL_TEMPLATE` | `https://github.com/{repo}.git` | Where to clone from (`{repo}` = owner/name, `{name}` = name); local development can use folders |
| `DATA_DIR` | `/data` | Storage: `workspaces/<id>/` (clones, views), the key file, migration reports; the old SQLite files until you remove them |
| `BACKEND_PYTHON` | venv in the workspace's `venvs/backend` | Interpreter with the backend's requirements, for the route export |
| `GITHUB_TOKEN` | | Without the App: one operator's token, used for every repository (local development) |
| `COMPOSE_PROFILES` | `https` (set by `install.sh`) | Optional services to run: `https` (Caddy), `llm` (local model) |
| `LLM_KIND`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_EFFORT` | Claude Opus 5.5, no key | Default LLM; Settings overrides it. `LOCAL_LLM` fills these for the local model |
| `LLM_TIMEOUT` | `600` | Seconds per LLM request (`3600` for a local model) |
| `LLM_CONTEXT` | `16384` | Local model context length (tokens) |
| `REVIEW_CHUNK_CHARS` / `REVIEW_MAX_CHARS` | `150000` / `600000` | Diff characters per review request / per review |
| `CHAT_TOOL_CHARS` | `20000` | Size cap for each chatbot tool result |
| `CHAT_TIMEOUT` | `300` (`900` for a local model) | Seconds for a whole Ask answer, all its lookups included |
| `LLM_PARALLEL` | `1` (`2` on 12 GB+ VMs) | Requests the local model serves at once; 2 keeps a slot free for Ask |
| `DOCS_CONTEXT_CHARS` | `48000` | Code facts per documentation request (lower for a local model; `install.sh` sets it) |
| `OLLAMA_URL` / `OLLAMA_DIR` | `http://llm:11434` / the `ollama` volume | Where the portal looks for local models / a host model store to reuse |
| `DEV_USER` | | Local development only: auto-login (see 1.11) |
| `DISABLE_WORKER` | | `1` runs the web app without the worker and poller |
| `GRAPHIFY_BIN` | graphify next to Python | Override the graphify executable |

---

## 6. Operating it

| Task | How |
|---|---|
| Logs | `docker compose logs -f portal` |
| Job history | Settings → Recent jobs (sync, push, checks, rebuild, review), with errors |
| Force a refresh | Settings → Sync from GitHub now / Rebuild views now |
| Re-review a PR | Reviews → "Review this PR now" (admins). The same head is never reviewed twice. |
| Update | `git pull && docker compose up -d --build` |
| Back up | `mongodump` of the database (3.2.0 step 7), the `portal_data` volume (clones, key file); keep `TOKEN_ENCRYPTION_KEY` with them |
| Health | `GET /healthz`: 200 `{"ok": true, "database": "up"}`, 503 when MongoDB is unreachable |
| Reset everything | `docker compose down -v` (deletes all volumes) |
| GitHub API use | About 20 requests per repo per poll, well under the App's 5,000/hour |

**Troubleshooting**

| Symptom | Cause and fix |
|---|---|
| "Sign-in needs the GitHub App" | Set the App's `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` (3.1) |
| The portal stops at start: "SQLite data that is not in MongoDB yet" | Migrate it (6.1) |
| The portal stops at start, or `/healthz` is 503, with a server selection timeout | MongoDB isn't reachable at `MONGODB_URI` (the `mongo` service, Atlas network access list, credentials) |
| A repository shows **revoked** | The App lost access (uninstalled or repository removed from it): install it again; the next sync restores it |
| GG-Ops shows **unauthorized** | Its API key was revoked or expired: make a new one in GG-Ops and save it in Settings → Integrations |
| Someone's Import lists only repositories they share with you | The App is installed only on your account. GitHub shows a person only repositories where the App is installed *and* they can read, so they install it on their own account or organizations (Import links it and says so). A **private** App can't be installed anywhere but its owner's account: make it public (App settings → Advanced) |
| Import says the App isn't installed, or you can't read the repository | Install the App on that account or repository (the button links there); private repositories also need your own access |
| An invite link says it was used or expired | Links work once, for 7 days: invite again from Settings → Team |
| Invites aren't emailed | `SMTP_HOST` is blank, or the error is shown under the invite form; the link shown there still works |
| Webhooks show 401 in the App's "Recent deliveries" | `GITHUB_WEBHOOK_SECRET` differs from the App's webhook secret |
| No pushes or CI on Activity | The App isn't installed on the repo, or lacks permissions; check Settings → status "GitHub App" |
| API reference stale or missing | The route export failed (usually the backend venv install); the error is in Recent jobs |
| "No LLM is configured" | Settings → LLM, then Test and Save |
| The model declined a request | A provider safety classifier refused; with Claude on Anthropic's API, fallback handles most of these, otherwise try another model |
| People shows emails instead of logins | Those authors haven't appeared on a default branch through the API yet; they merge automatically once they do |

### 6.1 Upgrading from SQLite to MongoDB

The release before this one kept everything in SQLite (`/data/control.db` and `/data/workspaces/<id>/portal.db`, or
`/data/portal.db` on older single-tenant installs). The migration is one command, run by you; the portal never
migrates by itself, and refuses to start while unmigrated SQLite data is there.

1. **Back up** the `portal_data` volume (3.2.0 step 7). The migration only reads the SQLite files and never deletes
   them, but keep the backup until you have checked the result.
2. Set `TOKEN_ENCRYPTION_KEY` in `.env` (LLM keys are re-encrypted with it), then start the database:
   `docker compose up -d mongo`.
3. **Dry run** (writes nothing, prints what would be inserted per table):
   `docker compose run --rm portal python -m portal.migrate`
4. **Apply**: `docker compose run --rm portal python -m portal.migrate --apply`. It writes in batches by each table's
   natural key and only inserts what is missing, so it can be re-run; an interrupted run resumes where it stopped
   (`--restart` reads everything again). It then **verifies**: row counts per table, 20 random rows per table compared
   field by field, every finding's review present, every pushed commit present. The report is in
   `/data/migration-report-<time>.json`; only a clean run lets the portal start.
5. `docker compose up -d`, sign in with GitHub, and check a workspace. Everyone signs in again (sessions and GitHub
   tokens are not carried over); members and owners are matched to their GitHub login at their first sign-in.
6. `python -m portal.migrate --verify` compares again at any time.

**Rolling back.** Nothing in SQLite changed, so: `docker compose down`, check out the previous release
(`git checkout ac8a6de`), `docker compose up -d --build`. Changes made in the new version after the migration are not
carried back. To retry the migration from scratch, drop the database (`docker compose exec mongo mongosh gg-docs
--eval 'db.dropDatabase()'`) and run it again. Delete the SQLite files only once you are sure (keep the backup).

---

## 7. Local development

You need a MongoDB 5.0 or newer: a local `mongod` (`mongodb://127.0.0.1:27017`, the default) or any other through
`MONGODB_URI`.

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt pytest      # .venv/bin/pip on Linux/macOS
npm ci
.venv/Scripts/python -m pytest tests                       # 30 checks on a throwaway database (gg_docs_test_*,
                                                           # dropped afterwards; TEST_MONGODB_URL picks the server):
                                                           # GitHub sign-in (state, PKCE, numeric ids), workspaces,
                                                           # invites, roles and isolation, repository discovery and
                                                           # access, API clients and scopes, login handoff, CSRF,
                                                           # GG-Ops and gg-pulse, the SQLite migration, MCP, docs,
                                                           # reviews, breakages, the read-only query tool
```

Run the whole portal without a GitHub App: `DEV_USER` signs in requests from this machine, you create a workspace,
and Import clones public repositories (or local folders through `GIT_URL_TEMPLATE`) with no access check. You get
commits, people, docs, reviews and chat; pushes and CI results need the App or `GITHUB_TOKEN`:

```bash
DATA_DIR=./data GIT_URL_TEMPLATE='D:/src/{name}' \
DEFAULT_BRANCHES='{"your-org/backend":"v2-accelerated","your-org/frontend":"main"}' \
BACKEND_PYTHON=<python with the backend requirements> DEV_USER=<your login> \
.venv/Scripts/python -m uvicorn portal.main:app --port 7000
```

The frontend lives in `web/` (Node 22.12+):

```bash
cd web && npm ci && npm run build      # writes web/dist, which the portal serves
cd web && npm run dev                  # http://localhost:4321 with hot reload; proxies /api, /auth, /site to :7000
```

`python build.py` on its own regenerates the views from `../backend` and `../frontend` (or `BACKEND_DIR`/`FRONTEND_DIR`) into `./site`. Serve that folder with any static server.

---

## 8. Limits and known gaps

- Breakages are judged on the commits CI ran on. If a push of several commits only gets CI on its head, the breakage is attributed to the head commit's author (the pusher is recorded too).
- Commit history covers `BACKFILL_DAYS`. The Events API only reaches back about 300 events, so pushes from before the portal existed are partial; from then on webhooks capture all of them.
- The review bot can be wrong. It is a second pair of eyes, not a gate; it doesn't block merges.
- Coverage from your CI isn't imported. Code churn (lines rewritten soon after being written) isn't measured yet.
- Docker is deployed on the server; the image wasn't built on the development machine.
- One worker thread runs every workspace's jobs in arrival order, so one team's big first sync delays the others'.
  Per-workspace fairness (or more workers) is the next step when that hurts.
- No quotas yet: workspaces per user, repositories per workspace and disk per workspace are unlimited. Removing a
  repository keeps its history and clone; there is no purge or workspace deletion yet.
- The same repository imported into two workspaces is cloned and fetched twice.
- gg-pulse has no analytics API yet, so its integration is a health check only. GG-Ops is read (project status and
  task counts); nothing is written to it.
- No MongoDB transactions (the default `mongo` service is a standalone server): changes that touch two documents are
  ordered so a failure in between is harmless (an invite is used up before the membership is made, never the reverse).
- Document search scans the workspace's documents with a regular expression: fine for hundreds of documents, a text
  index or Atlas Search when there are thousands.
- Rate limits count per client and per address; behind Caddy every request comes from Caddy's address unless uvicorn
  is told to trust its forwarded headers, so the per-address limits are shared.
- The legacy MCP token can still travel in a URL (for clients that only take one); turn it off once your agents use
  API client tokens.
