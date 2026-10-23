# GeneGenius engineering portal

A self-hosted dashboard that stays in sync with the GeneGenius GitHub repositories and answers questions about them:

| You want to know | Where |
|---|---|
| How the code is organised: modules, files, imports, call flow, every route | **Overview** (generated views) and `ARCHITECTURE.md` |
| Who pushed what, to which branch, and what CI said about it | **Activity** |
| Whose push broke a CI check that was passing before, and whether it is fixed | **Activity** (red banner), **People** |
| What the review bot found in each PR or direct push, and who wrote each flagged line | **Reviews**, and as comments on the PR when posting is on |
| Per person: commits, lines, pushes, CI breakages, review findings, "slop", SonarQube issues | **People** |
| Anything across all of the above, in plain language | **Ask** (the chatbot) |
| Bugs, code smells and security hotspots from static analysis | **SonarQube** at `/sonar` |

Tests stay in your CI. The portal reads the CI results (GitHub check runs) and never runs your test suites itself.

---

## Contents

1. [How it works](#1-how-it-works)
2. [Using the portal](#2-using-the-portal)
3. [Setting it up](#3-setting-it-up)
4. [Integrating it into the Genegenius-backend repository](#4-integrating-it-into-the-genegenius-backend-repository)
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
   │ PR reviews as genegenius-reviewer[bot]   ┌────────┴──────────────────────────────┐
   └──────────────────────────────────────────┤ portal container (:7000)              │
                                              │  FastAPI app ── pages + JSON API      │
 Browser ── "Sign in with GitHub" ──────────▶ │  worker thread ── job queue           │
                                              │  poller thread ── every 5 min         │
                                              │  /data: SQLite, git clones, views     │
                                              └────────┬──────────────────────────────┘
                                                       │ sonar-scanner, issues API
                                              ┌────────▼────────┐   ┌──────────────┐
                                              │ SonarQube :7010 │──▶│ Postgres     │
                                              └─────────────────┘   └──────────────┘
                                   optional:  Caddy (HTTPS on 443) in front of both
```

| Component | What it does |
|---|---|
| **portal** (one container) | Runs the web app, a single **worker thread** that executes jobs one at a time, and a **poller thread**. Holds everything in the `/data` volume. |
| **SonarQube** + **Postgres** | Static analysis. The portal pushes both repos into it with `sonar-scanner` and reads issues back per author. |
| **Caddy** (optional `https` profile) | Terminates TLS with an automatic certificate and routes `/sonar` to SonarQube and everything else to the portal. |
| **GitHub App** | The portal's identity on GitHub: it clones the repos, receives webhooks, reads CI results, posts reviews, and provides "Sign in with GitHub". |
| **LLM** (any OpenAI-compatible or Claude-compatible endpoint) | Writes the code reviews and powers the chatbot. Chosen by an admin in Settings. |

### 1.2 Repository layout

```
build.py              Generates the architecture views and ARCHITECTURE.md from two checkouts
depcruise.cjs         dependency-cruiser rules for the frontend (circular, orphan, unresolvable imports)
package.json/-lock    Node tools build.py uses (dependency-cruiser, typescript)
requirements.txt      Python dependencies of the portal (pinned)
portal/
  __init__.py         Configuration: every environment variable is read here
  db.py               SQLite schema and helpers
  github.py           GitHub App auth, REST helpers, OAuth login, webhook signature check
  sync.py             Worker + poller: clone/fetch, ingest, breakage detection, rebuild/sonar scheduling
  review.py           The review bot
  llm.py              One interface over Claude-compatible and OpenAI-compatible APIs
  chat.py             The chatbot's tools and loop
  main.py             Web app: login, webhook endpoint, JSON API, pages
  pages/              Activity, People, Reviews, Ask, Settings (plain HTML + JS) and the shared stylesheet
tests/test_portal.py  Checks for the logic that is easy to get subtly wrong
Dockerfile            The portal image (Python 3.11 + git + Node 22 + sonar-scanner with its own JRE)
docker-compose.yml    portal, sonarqube, sonar-db, optional caddy
Caddyfile             HTTPS routing for the https profile
.env.example          Every setting you need to fill in
```

### 1.3 Staying in sync with GitHub

Two triggers feed the same job queue:

- **Webhooks** (instant). GitHub calls `POST /webhook/github`. The portal rejects the request unless its `X-Hub-Signature-256` HMAC matches `GITHUB_WEBHOOK_SECRET`, then:
  - `push` → records the pusher, branch, `before` and `after` SHAs and whether it was forced, learns the email-to-login mapping from the payload's commits, and queues a **push** job.
  - `pull_request` with action `opened`, `synchronize`, `reopened` or `ready_for_review` (drafts skipped) → queues a **review_pr** job.
  - `check_run` / `check_suite` completed → queues a **checks** job.
- **Poller** (backup, every `POLL_SECONDS`, default 300). Queues a **sync** job per repo and a Sonar check, takes an hourly Sonar snapshot, and keeps the job log at 5,000 entries. Each sync also queues a review for every open, non-draft PR whose current head hasn't been reviewed yet (once an LLM is set). So if webhooks are missed or can't reach the portal, pushes, CI results and PR reviews still arrive within one poll. On the first poll after an LLM is set, every open PR gets reviewed once.

Jobs are de-duplicated while they wait, so a burst of pushes collapses into one rebuild. A failing job is logged (Settings → Recent jobs) and never stops the queue.

**A sync job** does, per repo:

1. **Clone or fetch.** On first run `git clone` into `/data/repos/<name>`, afterwards `git fetch --prune`. The installation token is passed as a one-off HTTP header (`git -c http.extraheader=…`), so it is never written to `.git/config`.
2. **Ingest commits.** `git log` over every remote branch for the last `BACKFILL_DAYS` (default 90): SHA, author, date, subject, lines added and deleted, files changed, and whether it is a merge. Commits reachable from the default branch get `on_default = 1`.
3. **Learn identities.** The commits API returns the GitHub login behind each author email on the default branch; noreply addresses (`123+login@users.noreply.github.com`) are decoded directly. Every table keyed by an email is then re-pointed to the login, which is how "Sagar V Gour" and "Sagar Vinod Gour" become one person.
4. **Record pushes** from the Events API (the latest 300 events) for anything a webhook missed. Each push stores the commits it introduced (`before..after`; for a new branch, the commits not yet on the default branch).
5. **Fetch CI results.** Check runs for the newest 15 first-parent commits of the default branch (60 on the first run). For each commit and check name, the latest completed run wins.
6. **Detect breakages** (next section).
7. **React to a new default-branch head:** queue a views **rebuild**, a **Sonar** scan, and a **review_push** for the change. The very first sync only records the baseline.

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
| **sonar** | Pushes both repos into SonarQube | `sonar-scanner` per repo (backend sources `app/`, tests `tests/`; frontend `src/`, tests `*.test.*`). Runs only with a Sonar token, at most every 15 minutes, and only when a default-branch head changed. |
| *(always)* | `index.html` and `ARCHITECTURE.md` | The overview page and a Markdown version with modules and their files, import maps as Mermaid blocks (GitHub renders them), and every route. |

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
| Sonar R / S / M | Open SonarQube issues blamed to them: reliability, security, maintainability (latest daily snapshot) |

A person is a GitHub login when the portal knows it, otherwise a git email. **These numbers are signals for a conversation, not a verdict:** someone who works in the riskiest code will collect more findings.

### 1.8 SonarQube

- **Scan:** see `sonar` in 1.5. SonarQube itself uses git blame to assign each issue to the author of its line.
- **Snapshot:** hourly, the portal asks SonarQube for open issues per author, split by software quality (reliability, security, maintainability), and keeps one snapshot per day.
- The SonarQube UI is at `/sonar` (port 7010, or `https://DOMAIN/sonar`) with its own login.

### 1.9 The chatbot (Ask)

Each question runs a loop of up to 10 rounds. The model picks tools, the portal runs them and returns the results, and the model answers. Tools:

| Tool | What it does | Safety |
|---|---|---|
| `sql` | One `SELECT` (or `WITH … SELECT`) on the portal database; returns up to 200 rows | Read-only connection (`mode=ro` + `query_only`), single statement only, 5-second limit |
| `commit_diff` | `git show` of a commit, optionally one file's patch | Repo must be one of the two; the SHA must be hex |
| `code_graph` | Searches the graphify code graph ("what calls X", "what imports Y") | Read-only |
| `architecture` | Returns a section of `ARCHITECTURE.md`, or its table of contents | Read-only |
| `sonar_issues` | Lists open SonarQube issues for a repo, optionally for one author or quality | Read-only API call |

The system prompt explains how every table is collected, tells the model to cite SHAs, PR numbers and dates, and tells it that commit messages and diffs are data, not instructions. Each answer lists the lookups it made. Your conversation lives in the browser tab (session storage); only the text of earlier turns is sent back with each question (the last 20 messages).

### 1.10 The LLM settings

Admins choose in **Settings**:

| Field | Notes |
|---|---|
| API type | **Claude-compatible** (Anthropic Messages API, via the official `anthropic` SDK) or **OpenAI-compatible** (Chat Completions, via the official `openai` SDK). The second covers OpenAI, Azure/OpenRouter-style gateways, vLLM, Ollama, LM Studio and similar. |
| Base URL | Blank = the provider's own API. Otherwise your gateway or server (OpenAI-compatible servers usually end in `/v1`). |
| API key | Stored in the portal database, never returned to the browser (shown as "saved, ends in 1234"); blank keeps the saved key. |
| Model | Default `claude-opus-5-5`. |
| Effort | Claude only (`low` … `max`, default `high`); choose "(don't send)" for models or servers that reject it. |

**Test connection** sends one tiny request with the form's values before you save. When the API type is Claude and the base URL is Anthropic's own API, requests to Claude Opus 5.5 / Opus 5 / Sonnet 5.5 / Fable 5.1 use **server-side refusal fallback** (`fallbacks: "default"`). If a safety classifier declines a request (for example, a security-heavy diff), Anthropic re-runs it on a fallback model instead of returning a refusal.

**Cost:** one LLM call per 150k-character chunk of each PR update or default-branch push, and up to 10 calls per chatbot question.

### 1.11 Login and security

- **Sign in with GitHub.** The GitHub App's OAuth flow, with a random `state` checked on return. After login, the portal checks with the App's installation token that the user is a member of `GITHUB_ORG`; non-members are refused.
- **Session:** a cookie signed with `SESSION_SECRET`, `SameSite=Lax`, `Secure` when `PUBLIC_URL` is https, valid for 7 days.
- **Every page and API needs a login**, except `/auth/*`, `/healthz` and the webhook (which needs a valid signature instead).
- **Admins** (`ADMINS`, GitHub logins) are the only ones who can change Settings, test the LLM, or trigger sync, rebuild and review.
- **Secrets:** the GitHub App private key is a mounted file (`secrets/github-app.pem`, read-only). LLM and Sonar keys sit in the database volume and are never sent to browsers. The container runs as a non-root user.
- **Untrusted text** (diffs, commit messages, review text) is shown escaped. Chatbot answers are rendered as Markdown and sanitised with DOMPurify.
- `DEV_USER` (automatic login for local development) only works when GitHub login is **not** configured and only for requests from the same machine.

### 1.12 Data model

SQLite at `/data/portal.db` (the chatbot sees this schema):

| Table | One row per | Key columns |
|---|---|---|
| `commits` | commit (last `BACKFILL_DAYS`) | `repo, sha, person, author_email, committed_at, message, additions, deletions, files, is_merge, on_default` |
| `identities` | git email | `email, login, name` |
| `pushes` / `push_commits` | push / commit in a push | `repo, branch, before, after, pusher, pushed_at, forced, commits, source` |
| `checks` | commit × CI check | `repo, sha, name, conclusion, completed_at, url` |
| `breakages` | check that broke on a commit | `check_name, broken_sha, last_good_sha, person, pusher, broken_at, fixed_sha, fixed_at` |
| `reviews` / `findings` | review / finding | `pr` (0 for a direct push), `branch`, `head_sha`, `summary`, `posted_url` / `path, line, severity, category, title, body, sha, person` |
| `sonar_issues` | day × repo × author × quality | `count` |
| `settings` | setting | LLM config, Sonar token, toggles, sync bookkeeping |
| `events` | job run | the log under Settings → Recent jobs |

### 1.13 JSON API

Everything the pages show comes from these endpoints (login required):

| Endpoint | Returns |
|---|---|
| `GET /api/summary` | CI status of each default-branch head, open breakages, worker queue, whether GitHub and the LLM are configured |
| `GET /api/activity?repo=&branch=&person=&days=30&limit=100` | Pushes (with their commits, CI checks, breakages, reviews) and commits |
| `GET /api/people?days=90` · `GET /api/people/{person}?days=90` | The People table · one person's commits, breakages, findings |
| `GET /api/breakages?days=90` | Breakages in the period plus every open one |
| `GET /api/reviews` · `GET /api/reviews/{id}` | Reviews with counts · one review with its findings |
| `GET /api/events` | The last 100 job runs |
| `POST /api/chat` `{"history": [...], "question": "..."}` | `{"answer", "steps"}` |
| `GET /api/settings` · `PUT /api/settings` · `POST /api/settings/test` | Settings (secrets masked) · save (admin) · test (admin) |
| `POST /api/admin/sync` · `/rebuild` · `/review` `{"repo", "pr"}` | Queue a job (admin) |

---

## 2. Using the portal

- **Overview** (`/`): architecture views for each repo (module graph, module tree, call flow, import map, API reference, dependency rules, graph report), every route, live CI status of each default branch, a Markdown download, and the SonarQube link.
- **Activity**: a red banner for every CI check currently broken on a default branch (who and since when), then pushes (pusher, branch, force-push flag, commits, CI result on the head, "broke X" badges, review link), then commits. Filter by repo, period and person.
- **People**: the table from 1.7. Click a column to sort, or a name for that person's commits, breakages and findings.
- **Reviews**: every review with counts, and each finding with severity, category, file and line (linked to GitHub) and who wrote the line. Admins can **review a PR now** by number.
- **Ask**: type a question or click an example. Good questions:
  - "Who pushed to v2-accelerated this week, and what did each push change?"
  - "Whose push last broke v2-module-tests, and which commit fixed it?"
  - "Rank people by weighted review findings per 1k lines over the last 90 days, with the numbers."
  - "Which PRs had critical findings this month, and are they merged?"
  - "What does services/vmis depend on, and which routes use it?"
  - "Show me the slop findings in Shrinjay's last 3 PRs."
- **Settings**: LLM, PR posting, Sonar token, "Sync now" and "Rebuild views now", connection status, and the job log.

---

## 3. Setting it up

### 3.1 Create the GitHub App (once, by an org owner)

GitHub → **GeneGeniusinc** → Settings → Developer settings → GitHub Apps → **New GitHub App**:

| Field | Value |
|---|---|
| Name | `genegenius-reviewer` (comments appear as `genegenius-reviewer[bot]`) |
| Homepage URL | your `PUBLIC_URL` |
| Callback URL | `PUBLIC_URL/auth/callback` |
| Webhook → Active, URL | `PUBLIC_URL/webhook/github` |
| Webhook secret | a long random string (`openssl rand -hex 32`); the same value goes in `GITHUB_WEBHOOK_SECRET` |
| Repository permissions | Contents: **Read** · Metadata: **Read** · Pull requests: **Read and write** · Checks: **Read** |
| Organization permissions | Members: **Read** |
| Subscribe to events | **Push**, **Pull request**, **Check run**, **Check suite** |
| Where can this app be installed | Only on this account |

Then, on the app's page:

1. Copy the **App ID** and **Client ID**, and **generate a client secret**.
2. **Generate a private key**; save the downloaded `.pem` as `secrets/github-app.pem` next to `docker-compose.yml`.
3. **Install App** → GeneGeniusinc → *Only select repositories* → `Genegenius-backend` and `Genegenius-phase-1`.

"Pull requests: write" is only used to post reviews, and only when an admin turns that on.

### 3.2 Install on a cloud VM (step by step)

Works on any Linux VM; the commands below are for **Ubuntu 24.04 LTS**. Size: **2 vCPU, 8 GB RAM, 40 GB disk** (SonarQube + Postgres use 2-3 GB of RAM; the backend venv for the route export takes about 3 GB of disk). For example, AWS EC2 `t3.large` with a 40 GB gp3 volume, GCP `e2-standard-2`, or Azure `B2ms`.

**1. Create the VM, network and DNS**

- Inbound firewall / security group: **22** (SSH, your IP only), **80** and **443** (everyone, for HTTPS and GitHub webhooks). Don't open 7000 or 7010.
- Give the VM a fixed public IP (an Elastic IP on AWS) and create a DNS **A record**, e.g. `portal.genegenius.ai` → that IP.
- Create the GitHub App (3.1) with `https://portal.genegenius.ai` as the base of its callback and webhook URLs.

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

**3. Kernel setting SonarQube needs**

```bash
echo 'vm.max_map_count=524288' | sudo tee /etc/sysctl.d/99-sonarqube.conf
sudo sysctl --system
```

**4. Get the code**

```bash
git clone https://github.com/CR-8/gg-docs.git ~/gg-portal && cd ~/gg-portal
```

If the repository is private, give the VM a read-only deploy key first:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gg-docs -N "" && cat ~/.ssh/gg-docs.pub
# add that public key under github.com/CR-8/gg-docs → Settings → Deploy keys (read-only), then:
GIT_SSH_COMMAND='ssh -i ~/.ssh/gg-docs' git clone git@github.com:CR-8/gg-docs.git ~/gg-portal && cd ~/gg-portal
git config core.sshCommand 'ssh -i ~/.ssh/gg-docs'   # so later `git pull` uses the key too
```

**5. Secrets and settings**

```bash
mkdir -p secrets && chmod 700 secrets
# from your own machine, copy the GitHub App private key up:
#   scp genegenius-reviewer.*.private-key.pem ubuntu@<vm-ip>:~/gg-portal/secrets/github-app.pem
chmod 600 secrets/github-app.pem
cp .env.example .env
openssl rand -hex 32    # paste as SESSION_SECRET
openssl rand -hex 24    # paste as SONAR_DB_PASSWORD
nano .env
```

Fill in at least:

```ini
PUBLIC_URL=https://portal.genegenius.ai
DOMAIN=portal.genegenius.ai
BIND_ADDR=127.0.0.1
GITHUB_APP_ID=...
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
GITHUB_WEBHOOK_SECRET=...          # same value as in the GitHub App
ADMINS=your-github-login,another-login
SESSION_SECRET=...
SONAR_DB_PASSWORD=...
SONAR_PUBLIC_URL=https://portal.genegenius.ai/sonar
```

The portal container runs as user id 1000, which is the default `ubuntu` user on most images. If its log shows "Permission denied" for the key, run `sudo chown 1000 secrets/github-app.pem`.

**6. Start**

```bash
docker compose --profile https up -d --build     # first build takes a few minutes
docker compose ps                                # portal, sonarqube, sonar-db, caddy: all "running"
docker compose logs -f portal                    # Ctrl-C stops following, not the portal
```

Caddy gets a Let's Encrypt certificate for `DOMAIN` on first start (DNS must already point at the VM, and ports 80/443 must be open).

**7. Check it**

- `https://portal.genegenius.ai` → **Sign in with GitHub**.
- GitHub App → Advanced → **Recent deliveries**: the ping and later pushes show `200`.
- Settings → Recent jobs: `sync` entries, then `rebuild` after the first build.

**8. Keep it running and up to date**

- Everything restarts on its own after a reboot (`restart: unless-stopped`, Docker enabled at boot).
- Update: `cd ~/gg-portal && git pull && docker compose --profile https up -d --build`
- Back up the data volume (SQLite with settings and keys, clones, views):
  ```bash
  docker run --rm -v gg-portal_portal_data:/d -v $PWD:/b alpine tar czf /b/portal-data-$(date +%F).tgz -C /d .
  ```
  Back up SonarQube's database too if you keep its history: the `gg-portal_sonar_db` volume, or `docker compose exec sonar-db pg_dump -U sonar sonar > sonar.sql`.

### 3.2.1 Access by IP address (no domain)

Use this until you have a domain. It's the same install as above, with these differences:

**Network.** In the security group, open **7000** (portal) and **7010** (SonarQube) instead of 80/443, **to your office or VPN IP ranges only**. These ports are plain HTTP, so anyone on the path can read login cookies. Set the rules in the cloud firewall (security group): Docker publishes ports around `ufw`, so `ufw` rules don't protect them. Give the VM an **Elastic/static IP**, because the GitHub App's URLs contain it.

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
SONAR_PUBLIC_URL=http://<vm-ip>:7010/sonar
BIND_ADDR=0.0.0.0
# leave DOMAIN empty
```

**Start without the https profile:**
```bash
docker compose up -d --build
```

Open `http://<vm-ip>:7000` (portal) and `http://<vm-ip>:7010/sonar` (SonarQube). Updates: `git pull && docker compose up -d --build`.

**Adding HTTPS later without buying a domain:** the free wildcard DNS service sslip.io maps `<ip-with-dashes>.sslip.io` to your IP. For example, `203-0-113-5.sslip.io` resolves to `203.0.113.5`, so Caddy can get a real certificate for it. To switch:
1. Set `DOMAIN=203-0-113-5.sslip.io` and `PUBLIC_URL=https://203-0-113-5.sslip.io` (and `SONAR_PUBLIC_URL=.../sonar`), plus `BIND_ADDR=127.0.0.1`.
2. Open 80/443 and close 7000/7010.
3. Update the three GitHub App URLs.
4. Run `docker compose --profile https up -d --build`.


### 3.3 First run

1. Open `PUBLIC_URL` and **sign in with GitHub**.
2. The first sync loads 90 days of history and builds the views. That takes a few minutes, plus a one-time install of the backend's requirements for the route export (about 10 minutes). Watch it under Settings → Recent jobs.
3. **Settings → LLM:** choose the API type, base URL (blank for the provider's own API), key and model → **Test connection** → **Save**.
4. **SonarQube:** open `/sonar`, log in as `admin` / `admin`, set a new password, then My Account → Security → **Generate token** (type *Global Analysis*) → paste it in Settings → Save. The next default-branch push (or Rebuild) scans both repos.
5. Read a few reviews in the portal. When you trust them, turn on **Post reviews on GitHub PRs**.

---

## 4. Integrating it into the Genegenius-backend repository

The portal doesn't need to live anywhere special: it clones both repositories from GitHub through the App wherever it runs. What changes is where the **code** lives and how it is **deployed**.

### Recommended: a subfolder of the backend repo, run as its own service

The code lives in `Genegenius-backend/tools/portal/`, versioned and reviewed like the rest of the backend, while it runs in its own containers next to (not inside) the API.

1. **Branch** from the default branch:
   ```bash
   cd Genegenius-backend
   git switch v2-accelerated && git pull
   git switch -c feat/engineering-portal
   ```
2. **Copy the portal in**, without local state (Windows PowerShell shown; `rsync -a --exclude …` on Linux/macOS):
   ```powershell
   robocopy D:\GeneGenius\arch-portal D:\GeneGenius\Genegenius-backend\tools\portal /E /XD data site work .venv node_modules __pycache__ .pytest_cache secrets /XF .env *.log
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
         - run: docker build -t genegenius-portal .
   ```
   To publish the image to ECR like the backend does, copy the login and push steps from `ecr-build.yml`. You'll need a new ECR repository (for example `genegenius-portal`), and the `GitHubActions-ECRBuild` role's trust policy must allow this workflow.
5. **Deploy** on the portal server:
   ```bash
   git clone https://github.com/GeneGeniusinc/Genegenius-backend.git && cd Genegenius-backend/tools/portal
   cp .env.example .env && mkdir secrets   # fill in .env, add secrets/github-app.pem
   docker compose --profile https up -d --build
   ```
   Updating: `git pull && docker compose up -d --build` in that folder.
6. **Behind your existing nginx** instead of Caddy (if it runs on the same host as `api.genegenius.ai`): leave out `--profile https`, set `BIND_ADDR=127.0.0.1`, and add a server block (certificate via certbot as for the API):
   ```nginx
   server {
       listen 443 ssl;
       server_name portal.genegenius.ai;
       # ssl_certificate ... (same as the api.genegenius.ai block)
       client_max_body_size 10m;
       location /sonar { proxy_pass http://127.0.0.1:7010; proxy_set_header Host $host; }
       location / {
           proxy_pass http://127.0.0.1:7000;
           proxy_set_header Host $host;
           proxy_read_timeout 300s;   # chatbot answers can take a while
       }
   }
   ```

### Also possible: add it to the backend's docker-compose.yml

If one host runs everything, add the portal, sonarqube and sonar-db services from `tools/portal/docker-compose.yml` to the backend compose file, with `build: ./tools/portal` and the same volumes. Only do this when the host has 3-4 GB of RAM to spare, because SonarQube is the heavy part.

### Not recommended: mounting it inside the backend's FastAPI app

It is technically possible (`app.mount("/portal", portal_app)`), but:

- **Dependencies clash.** The backend pins FastAPI 0.104 and its own Pydantic/Starlette; the portal pins newer ones.
- **Heavy jobs** (git clones, graphify, npm, sonar-scanner, backend venv installs) would compete with clinical-report requests.
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
| `BIND_ADDR` | `0.0.0.0` | Interface for ports 7000/7010; `127.0.0.1` behind Caddy or nginx |
| `GITHUB_ORG` | `GeneGeniusinc` | Org whose members may log in, and where the App is installed |
| `BACKEND_REPO` / `FRONTEND_REPO` | `GeneGeniusinc/Genegenius-backend` / `GeneGeniusinc/Genegenius-phase-1` | The two repositories |
| `GITHUB_APP_ID` | | App ID |
| `GITHUB_APP_PRIVATE_KEY_FILE` | `/secrets/github-app.pem` in Docker | App private key file (or `GITHUB_APP_PRIVATE_KEY` with the PEM inline) |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | | The App's OAuth credentials, for "Sign in with GitHub" |
| `GITHUB_WEBHOOK_SECRET` | | Must match the App's webhook secret; webhooks are refused without it |
| `ADMINS` | | Comma-separated GitHub logins who can change Settings |
| `SESSION_SECRET` | random per start | Signs login cookies; set it, or everyone is logged out on restart |
| `SONAR_DB_PASSWORD` | | Postgres password for SonarQube (required by compose) |
| `SONAR_PUBLIC_URL` | | The SonarQube link shown on the Overview |
| `SONAR_URL` | `http://sonarqube:9000/sonar` in Docker | How the portal reaches SonarQube |
| `SONAR_TOKEN` | | Alternative to the token in Settings |
| `POLL_SECONDS` | `300` | Poll interval (backup to webhooks) |
| `BACKFILL_DAYS` | `90` | History loaded and kept for commits |
| `DEFAULT_BRANCHES` | from GitHub | JSON override, e.g. `{"GeneGeniusinc/Genegenius-backend": "v2-accelerated"}` |
| `GIT_URL_TEMPLATE` | `https://github.com/{repo}.git` | Where to clone from (`{repo}` = owner/name, `{name}` = name); local development can use folders |
| `DATA_DIR` / `SITE_DIR` / `WORK_DIR` | `/data`, `/data/site`, `/data/work` | Storage locations |
| `BACKEND_PYTHON` | venv in `/data/venvs/backend` | Interpreter with the backend's requirements, for the route export |
| `GITHUB_TOKEN` | | Local development only: a plain token instead of the App |
| `DEV_USER` | | Local development only: auto-login (see 1.11) |
| `DISABLE_WORKER` | | `1` runs the web app without the worker and poller |
| `GRAPHIFY_BIN` | graphify next to Python | Override the graphify executable |

---

## 6. Operating it

| Task | How |
|---|---|
| Logs | `docker compose logs -f portal` (and `sonarqube`) |
| Job history | Settings → Recent jobs (sync, push, checks, rebuild, review, sonar), with errors |
| Force a refresh | Settings → Sync from GitHub now / Rebuild views now |
| Re-review a PR | Reviews → "Review this PR now" (admins). The same head is never reviewed twice. |
| Update | `git pull && docker compose up -d --build` |
| Back up | Docker volumes `portal_data` (the SQLite database holds settings and keys) and `sonar_db` |
| Reset everything | `docker compose down -v` (deletes all volumes) |
| GitHub API use | About 20 requests per repo per poll, well under the App's 5,000/hour |

**Troubleshooting**

| Symptom | Cause and fix |
|---|---|
| "GitHub login is not configured" | Set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` |
| "… is not a member of the GeneGeniusinc organisation" | The user isn't an org member, or the App lacks Organization → Members: Read |
| Webhooks show 401 in the App's "Recent deliveries" | `GITHUB_WEBHOOK_SECRET` differs from the App's webhook secret |
| No pushes or CI on Activity | The App isn't installed on the repo, or lacks permissions; check Settings → status "GitHub App" |
| API reference stale or missing | The route export failed (usually the backend venv install); the error is in Recent jobs |
| "No LLM is configured" | Settings → LLM, then Test and Save |
| The model declined a request | A provider safety classifier refused; with Claude on Anthropic's API, fallback handles most of these, otherwise try another model |
| SonarQube won't start | `vm.max_map_count` isn't set on the host (3.2) |
| People shows emails instead of logins | Those authors haven't appeared on a default branch through the API yet; they merge automatically once they do |

---

## 7. Local development

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt pytest      # .venv/bin/pip on Linux/macOS
npm ci
.venv/Scripts/python -m pytest tests                       # 9 checks: webhook HMAC, diff parsing, finding
                                                           # validation, chunking, breakage detection,
                                                           # LLM tool formats, read-only SQL
```

Run the whole portal against local checkouts, without a GitHub App. You get commits, people, views, reviews and chat; pushes, CI results and logins need the App or `GITHUB_TOKEN`:

```bash
DATA_DIR=./data GIT_URL_TEMPLATE='D:/GeneGenius/{name}' \
DEFAULT_BRANCHES='{"GeneGeniusinc/Genegenius-backend":"v2-accelerated","GeneGeniusinc/Genegenius-phase-1":"main"}' \
BACKEND_PYTHON=<python with the backend requirements> DEV_USER=<your login> ADMINS=<your login> \
.venv/Scripts/python -m uvicorn portal.main:app --port 7000
```

`python build.py` on its own regenerates the views from `../Genegenius-backend` and `../Genegenius-phase-1` (or `BACKEND_DIR`/`FRONTEND_DIR`) into `./site`. Serve that folder with any static server.

---

## 8. Limits and known gaps

- Breakages are judged on the commits CI ran on. If a push of several commits only gets CI on its head, the breakage is attributed to the head commit's author (the pusher is recorded too).
- Commit history covers `BACKFILL_DAYS`. The Events API only reaches back about 300 events, so pushes from before the portal existed are partial; from then on webhooks capture all of them.
- SonarQube Community analyses the main branch only (no PR decoration). PR feedback comes from the review bot.
- The review bot can be wrong. It is a second pair of eyes, not a gate; it doesn't block merges.
- Coverage from your CI isn't imported into SonarQube yet. Code churn (lines rewritten soon after being written) isn't measured yet.
- Docker is deployed on the server; the image wasn't built on the development machine.
