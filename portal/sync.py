"""Git sync worker: clone/fetch, ingest commits, pushes and CI results, detect breakages, and schedule
view rebuilds and reviews. One worker thread runs jobs in order; a poller backs up the webhooks.
Every job carries the workspace it was queued from and runs inside it."""
import hashlib
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx
from pymongo import UpdateMany

from . import (BACKFILL_DAYS, DEFAULT_BRANCHES, GIT_URL, POLL_SECONDS, ROOT, WORKSPACE, db, llm,
               repo_dir, site_dir, work_dir, workspace, ws_dir)
from .github import RateLimited, gh, repo_meta

ZERO = "0" * 40
WIN = os.name == "nt"
FAIL, PASS = {"failure", "timed_out"}, {"success"}
NOREPLY = re.compile(r"^(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$", re.I)
LOG_FMT = "%x1e%H%x1f%P%x1f%ae%x1f%an%x1f%aI%x1f%s"

jobs = queue.Queue()
_pending, _lock = set(), threading.Lock()
_defaults = {}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(kind, detail, ok=True):
    print(f"[{kind}] {detail}", flush=True)
    db.events.insert({"at": now(), "kind": kind, "detail": str(detail)[:4000], "ok": int(ok)}, with_id=True)


def enqueue(*job):
    job = (WORKSPACE.get(), *job)
    with _lock:
        if job in _pending:
            return
        _pending.add(job)
    jobs.put(job)


# ---------- git ----------

def git(repo, *args, auth=False):
    r = subprocess.run(["git", *(gh.git_auth(repo) if auth else []), *args], cwd=repo_dir(repo),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError(f"git {args[0]} ({repo}): {r.stderr.strip()[-500:]}")
    return r.stdout


def git_ok(repo, *args):
    return subprocess.run(["git", *args], cwd=repo_dir(repo), capture_output=True).returncode == 0


def fetch(repo):
    d = repo_dir(repo)
    if (d / ".git").exists():
        git(repo, "fetch", "--quiet", "--prune", "origin", auth=True)
        return
    d.parent.mkdir(parents=True, exist_ok=True)
    url = GIT_URL.format(repo=repo, name=repo.split("/")[1])
    r = subprocess.run(["git", *gh.git_auth(repo), "clone", "--quiet", url, str(d)], capture_output=True, text=True)
    if r.returncode:
        hint = "" if gh.enabled else (" | The repo is private: set the GitHub App (or GITHUB_TOKEN, a read-only "
                                      "token) in .env, then re-run install.sh")
        raise RuntimeError(f"git clone {repo}: {r.stderr.strip()[-500:]}{hint}")


def default_branch(repo):
    if repo in DEFAULT_BRANCHES:
        return DEFAULT_BRANCHES[repo]
    if repo not in _defaults:
        _defaults[repo] = (gh.get(f"/repos/{repo}")["default_branch"] if gh.enabled else
                           git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip().removeprefix("origin/"))
    return _defaults[repo]


def views():
    """(backend, frontend): the pair the architecture views are built from, or () for none.
    ponytail: only a workspace migrated from the single-tenant install has one (build.py expects a FastAPI backend
    and a Next.js frontend); add a picker in Settings when other tenants want it."""
    v = db.setting("views")
    return (v["backend"], v["frontend"]) if v else ()


def checkout_defaults():
    for repo in views():
        branch = default_branch(repo)
        git(repo, "checkout", "--quiet", "--force", "-B", branch, f"origin/{branch}")


# ---------- people ----------

def person_for(email, ids=None):
    """GitHub login for a git author email when we know it, else the email itself."""
    email = (email or "").lower()
    if m := NOREPLY.match(email):
        return m.group(1)
    if ids is None:
        r = db.identities.one({"email": email}, fields=["login"])
        return r["login"] if r else email
    return ids.get(email, email)


def remember_identities(rows):
    """rows of (email, login, name); then re-point anything still keyed by those emails."""
    learned = {e.lower(): (login, name) for e, login, name in rows if e and login}
    if not learned:
        return
    db.identities.upsert_many([{"email": e, "login": login, "name": name} for e, (login, name) in learned.items()],
                              ["email"])
    for table in (db.commits, db.findings, db.breakages):
        table.c.bulk_write([UpdateMany(table.scope({"person": e}), {"$set": {"person": login}})
                            for e, (login, _) in learned.items()], ordered=False)


def learn_identities(repo):
    since = db.setting(f"identities_since:{repo}") or (datetime.now(timezone.utc) - timedelta(days=BACKFILL_DAYS)).isoformat()
    started = now()
    rows = [(c["commit"]["author"]["email"], c["author"]["login"], c["commit"]["author"]["name"])
            for c in gh.paginate(f"/repos/{repo}/commits", limit=5000, sha=default_branch(repo), since=since)
            if c.get("author") and c["commit"]["author"].get("email")]
    remember_identities(rows)
    db.set_setting(f"identities_since:{repo}", started)


# ---------- commits, pushes, checks ----------

def ingest_commits(repo):
    since = (date.today() - timedelta(days=BACKFILL_DAYS)).isoformat()
    known = {r["sha"] for r in db.commits.find({"repo": repo}, fields=["sha"])}
    ids = {r["email"]: r["login"] for r in db.identities.find(fields=["email", "login"])}
    rows = []
    for rec in git(repo, "log", "--remotes=origin", f"--since={since}", f"--format={LOG_FMT}", "--numstat").split("\x1e")[1:]:
        head, *stats = rec.strip("\n").split("\n")
        sha, parents, email, name, at, subject = head.split("\x1f", 5)
        if sha in known:
            continue
        adds = dels = files = 0
        for s in stats:
            parts = s.split("\t")
            if len(parts) == 3:
                files += 1
                if parts[0] != "-":  # binary files show "-"
                    adds, dels = adds + int(parts[0]), dels + int(parts[1])
        rows.append({"repo": repo, "sha": sha, "person": person_for(email, ids), "author_name": name,
                     "author_email": email.lower(), "committed_at": at, "message": subject, "additions": adds,
                     "deletions": dels, "files": files, "is_merge": int(len(parents.split()) > 1), "on_default": 0})
    db.commits.upsert_many(rows, ["repo", "sha"], only_new=True)
    on_default = git(repo, "rev-list", f"--since={since}", f"origin/{default_branch(repo)}").split()
    db.commits.update({"repo": repo, "sha": {"$in": on_default}, "on_default": 0}, {"$set": {"on_default": 1}})
    return len(rows)


def record_push(repo, branch, before, after, pusher, pushed_at, forced, source):
    if after == ZERO or db.pushes.one({"repo": repo, "branch": branch, "before": before, "after": after}, fields=["id"]):
        return
    if not git_ok(repo, "cat-file", "-e", f"{after}^{{commit}}"):
        return  # not fetched yet; the next sync records it from the Events API
    if before != ZERO and git_ok(repo, "cat-file", "-e", f"{before}^{{commit}}"):
        shas = git(repo, "rev-list", "--max-count=250", f"{before}..{after}").split()
        if forced is None:
            forced = not git_ok(repo, "merge-base", "--is-ancestor", before, after)
    else:  # new branch: the commits that are not on the default branch yet
        shas = git(repo, "rev-list", "--max-count=250", after, "--not", f"origin/{default_branch(repo)}").split()
    db.pushes.insert({"repo": repo, "branch": branch, "before": before, "after": after, "pusher": pusher,
                      "pushed_at": pushed_at, "forced": int(bool(forced)), "commits": len(shas), "source": source,
                      "commit_shas": list(dict.fromkeys(shas))}, with_id=True)


def ingest_push_events(repo):
    for ev in gh.paginate(f"/repos/{repo}/events", limit=300):
        p = ev.get("payload") or {}
        if ev["type"] == "PushEvent" and p.get("ref", "").startswith("refs/heads/"):
            record_push(repo, p["ref"][11:], p.get("before") or ZERO, p["head"], ev["actor"]["login"],
                        ev["created_at"], None, "events")


def detect_breakages(order, results):
    """order: commit shas oldest -> newest on one branch; results: {sha: {check name: conclusion}}.
    A check that passed earlier and fails on a commit is a breakage until a later commit passes it again.
    Returns [(check, broken_sha, last_good_sha, fixed_sha or None)]."""
    last_pass, open_, out = {}, {}, []
    for sha in order:
        for check, conclusion in results.get(sha, {}).items():
            if conclusion in PASS:
                if check in open_:
                    out[open_.pop(check)][3] = sha
                last_pass[check] = sha
            elif conclusion in FAIL and check not in open_ and check in last_pass:
                open_[check] = len(out)
                out.append([check, sha, last_pass[check], None])
    return [tuple(b) for b in out]


def ingest_checks(repo):
    branch = default_branch(repo)
    first = not db.setting(f"checks_backfilled:{repo}")
    shas = git(repo, "rev-list", "--first-parent", f"--max-count={60 if first else 15}", f"origin/{branch}").split()
    rows = []
    for sha in shas:
        for r in gh.paginate(f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs", limit=300):
            if r["status"] == "completed":
                rows.append({"repo": repo, "sha": sha, "name": r["name"], "conclusion": r["conclusion"],
                             "completed_at": r["completed_at"], "url": r["html_url"]})
    have = {(c["sha"], c["name"]): c["completed_at"] or "" for c in
            db.checks.find({"repo": repo, "sha": {"$in": shas}}, fields=["sha", "name", "completed_at"])}
    db.checks.upsert_many([r for r in rows if (r["completed_at"] or "") >= have.get((r["sha"], r["name"]), "")],
                          ["repo", "sha", "name"])  # a re-run that finished later wins
    db.set_setting(f"checks_backfilled:{repo}", True)
    detect(repo, branch)


def detect(repo, branch, window=80):
    order = git(repo, "rev-list", "--first-parent", f"--max-count={window}", f"origin/{branch}").split()[::-1]
    if not order:
        return
    results, done_at = defaultdict(dict), {}
    for r in db.checks.find({"repo": repo, "sha": {"$in": order}}, fields=["sha", "name", "conclusion", "completed_at"]):
        results[r["sha"]][r["name"]] = r["conclusion"]
        done_at[r["sha"], r["name"]] = r["completed_at"]
    for check, broken, good, fixed in detect_breakages(order, results):
        who = db.commits.one({"repo": repo, "sha": broken}, fields=["person"])
        pushed = db.pushes.one({"repo": repo, "commit_shas": broken}, sort=[("pushed_at", 1)], fields=["pusher"])
        pusher = pushed["pusher"] if pushed else None
        key = {"repo": repo, "branch": branch, "check_name": check, "broken_sha": broken}
        fix = {"fixed_sha": fixed, "fixed_at": done_at.get((fixed, check)) if fixed else None}
        old = db.breakages.one(key, fields=["pusher"])
        if old:
            db.breakages.update(key, {"$set": {**fix, "pusher": old["pusher"] or pusher}})
        else:
            db.breakages.insert({**key, "last_good_sha": good, "person": who["person"] if who else None, "pusher": pusher,
                                 "broken_at": done_at.get((broken, check)), **fix}, with_id=True)


# ---------- jobs ----------

def check_head(repo):
    """A new default-branch head schedules a views rebuild and a review of the change."""
    branch = default_branch(repo)
    head = git(repo, "rev-parse", f"origin/{branch}").strip()
    seen = db.setting(f"head:{repo}")
    if head == seen:
        return
    db.set_setting(f"head:{repo}", head)
    if repo in views():
        enqueue("rebuild")
    if seen:  # the first sync only sets the baseline
        enqueue("review_push", repo, branch, seen, head)


def save_pulls(repo, prs):
    rows = [{"repo": repo, "number": p["number"], "title": p["title"], "author": p["user"]["login"],
             "state": "merged" if p.get("merged_at") else p["state"], "draft": int(bool(p.get("draft"))),
             "base": p["base"]["ref"], "head": p["head"]["ref"], "head_sha": p["head"]["sha"], "created_at": p["created_at"],
             "updated_at": p["updated_at"], "merged_at": p.get("merged_at"), "url": p["html_url"]} for p in prs]
    have = {r["number"]: r["updated_at"] or "" for r in
            db.pulls.find({"repo": repo, "number": {"$in": [r["number"] for r in rows]}}, fields=["number", "updated_at"])}
    db.pulls.upsert_many([r for r in rows if r["updated_at"] >= have.get(r["number"], "")], ["repo", "number"])


def ingest_pulls(repo):
    """Every open PR plus the 50 most recently closed, for Ask, the MCP server and poll_prs."""
    save_pulls(repo, gh.paginate(f"/repos/{repo}/pulls", limit=100, state="open")
               + gh.paginate(f"/repos/{repo}/pulls", limit=50, state="closed", sort="updated", direction="desc"))


def poll_prs(repo):
    """Webhooks normally start PR reviews; this catches open PRs whose events never arrived
    (e.g. the portal's port is firewalled to office IPs)."""
    reviewed = {(r["pr"], r["head_sha"]) for r in db.reviews.find({"repo": repo, "pr": {"$gt": 0}}, fields=["pr", "head_sha"])}
    for pr in db.pulls.find({"repo": repo, "state": "open", "draft": 0}, fields=["number", "head_sha"]):
        if (pr["number"], pr["head_sha"]) not in reviewed:
            enqueue("review_pr", repo, pr["number"])


def still_reachable(repo):
    """Re-checks a connected repository with the App's own token on every sync: refreshes its metadata, and marks it
    revoked (and skips it, keeping its data) when the App lost access. Installing the App again restores it."""
    if not gh.enabled:
        return True
    try:
        r = gh.get(f"/repos/{repo}")
    except RateLimited:
        raise
    except (PermissionError, httpx.HTTPStatusError) as e:
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code not in (401, 403, 404):
            raise
        db.update_repo(WORKSPACE.get(), repo, {"status": "revoked", "checked_at": now(),
                                               "status_detail": "The GitHub App can no longer read it."})
        log("sync", f"{repo}: the GitHub App can no longer read it; not synced until access is restored", False)
        return False
    _defaults[repo] = r["default_branch"]
    db.update_repo(WORKSPACE.get(), repo, {**repo_meta(r), "status": "active", "status_detail": None, "checked_at": now()})
    return True


def sync(repo):
    if not still_reachable(repo):
        return
    fetch(repo)
    n = ingest_commits(repo)
    if gh.enabled:
        # Each API step is optional: a token without, say, Checks access must not stop the views rebuilding.
        steps = [learn_identities, ingest_push_events, ingest_checks, ingest_pulls] + ([poll_prs] if llm.configured() else [])
        for step in steps:
            try:
                step(repo)
            except Exception as e:
                log(step.__name__, f"{repo}: skipped, {e!r}"[:500], False)
    check_head(repo)
    from . import docgen  # docgen imports this module
    docgen.enqueue(repo)  # cheap when nothing changed: it compares fingerprints
    log("sync", f"{repo}: {n} new commits")


def on_push(repo, branch, before, after, pusher, pushed_at, forced):
    fetch(repo)
    ingest_commits(repo)
    record_push(repo, branch, before, after, pusher, pushed_at, forced, "webhook")
    if branch == default_branch(repo):
        check_head(repo)
    log("push", f"{pusher} pushed {after[:8]} to {repo}@{branch}")


def on_checks(repo):
    fetch(repo)
    ingest_checks(repo)


def backend_python():
    """A venv with the backend's requirements, rebuilt when requirements.txt changes (for the OpenAPI export)."""
    if os.environ.get("BACKEND_PYTHON"):
        return os.environ["BACKEND_PYTHON"]
    req = repo_dir(views()[0]) / "requirements.txt"
    venv = ws_dir() / "venvs" / "backend"
    py = venv / ("Scripts/python.exe" if WIN else "bin/python")
    digest = hashlib.sha256(req.read_bytes()).hexdigest() if req.exists() else ""
    if digest != db.setting("backend_requirements") or not py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(req),
                            "--extra-index-url", "https://download.pytorch.org/whl/cpu"], capture_output=True, text=True)
        if r.returncode:
            log("venv", r.stderr[-2000:], False)
        else:
            db.set_setting("backend_requirements", digest)
    return str(py)


def frontend_modules():
    """npm ci in the frontend clone when its lockfile changes (dependency-cruiser resolves through node_modules)."""
    d = repo_dir(views()[1])
    lock = d / "package-lock.json"
    digest = hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else ""
    if digest and (digest != db.setting("frontend_lock") or not (d / "node_modules").exists()):
        r = subprocess.run(["npm.cmd" if WIN else "npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=d,
                           capture_output=True, text=True, env=dict(os.environ, npm_config_legacy_peer_deps="false"))
        if r.returncode:
            log("npm", r.stderr[-2000:], False)
        else:
            db.set_setting("frontend_lock", digest)


def build_env(**extra):
    backend, frontend = views()
    return dict(os.environ, BACKEND_DIR=str(repo_dir(backend)), FRONTEND_DIR=str(repo_dir(frontend)),
                SITE_DIR=str(site_dir()), WORK_DIR=str(work_dir()), **extra)


def run_build(*steps, **env):
    r = subprocess.run([sys.executable, str(ROOT / "build.py"), *steps], env=build_env(**env),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    failed = [line for line in r.stdout.splitlines() if line.startswith("!!!")]
    return r.returncode == 0 and not failed, "\n".join(failed) or r.stderr[-2000:]


def rebuild():
    if not views():
        return
    checkout_defaults()
    py = backend_python()
    frontend_modules()
    ok, detail = run_build("graphs", "routes", "deps", BACKEND_PYTHON=py)
    log("rebuild", "views rebuilt" if ok else detail, ok)


def handle_webhook(event, payload):
    """One GitHub App delivers the webhooks of every tenant: each workspace that imported the repository gets it."""
    repo = (payload.get("repository") or {}).get("full_name")
    targets = list(db.repo_links.find({"repo_lower": repo.lower()}, {"workspace_id": 1, "repo": 1})) if repo else []
    results = set()
    for t in targets:
        with workspace(t["workspace_id"]):
            results.add(on_webhook(event, payload, t["repo"]))
    return "queued" if "queued" in results else "ignored"


def on_webhook(event, payload, repo):
    if event == "push" and payload.get("ref", "").startswith("refs/heads/"):
        remember_identities([(c["author"].get("email"), c["author"].get("username"), c["author"].get("name"))
                             for c in payload.get("commits", []) if c.get("author")])
        pusher = (payload.get("sender") or {}).get("login") or (payload.get("pusher") or {}).get("name")
        enqueue("push", repo, payload["ref"][11:], payload["before"], payload["after"], pusher, now(), bool(payload.get("forced")))
    elif event == "pull_request" and payload.get("pull_request"):
        save_pulls(repo, [payload["pull_request"]])  # every action: opened, edited, closed, merged, ...
        if payload.get("action") in ("opened", "synchronize", "reopened", "ready_for_review") \
                and not payload["pull_request"].get("draft"):
            enqueue("review_pr", repo, payload["number"])
    elif event in ("check_run", "check_suite") and payload.get("action") == "completed":
        enqueue("checks", repo)
    else:
        return "ignored"
    return "queued"


def handlers():
    from . import review  # review imports this module
    return {"sync": sync, "push": on_push, "checks": on_checks, "rebuild": rebuild,
            "review_pr": review.review_pr, "review_push": review.review_push}


def worker():
    # ponytail: one worker for every workspace, in arrival order; a pool with per-workspace fairness when
    # one tenant's big syncs keep the others waiting.
    run = handlers()
    while True:
        job = jobs.get()
        with _lock:
            _pending.discard(job)
        ws, kind, *args = job
        with workspace(ws):
            try:
                run[kind](*args)
            except Exception as e:  # one bad job must not stop the queue
                log(kind, f"{tuple(args)}: {e!r}", False)
                traceback.print_exc()


def poller():
    while True:
        for w in db.workspaces.find({}, {"_id": 1}):
            with workspace(w["_id"]):
                for repo in db.repos():
                    enqueue("sync", repo)
                last = db.events.one(sort=[("id", -1)], fields=["id"])
                if last:
                    db.events.delete({"id": {"$lt": last["id"] - 5000}})
        time.sleep(POLL_SECONDS)


def start():
    from . import docgen
    for w in db.workspaces.find({}, {"_id": 1}):
        with workspace(w["_id"]):
            if views() and not (site_dir() / "overview.json").exists() \
                    and all((repo_dir(r) / ".git").exists() for r in views()):
                enqueue("rebuild")  # views built before the Overview page read overview.json
    docgen.start()
    for target in (worker, poller):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()


def status():
    """The current workspace's waiting jobs."""
    ws = WORKSPACE.get()
    with _lock:
        mine = [job[1] for job in _pending if job[0] == ws]
    return {"queued": len(mine), "pending": sorted(set(mine))}
