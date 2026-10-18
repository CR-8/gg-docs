"""Git sync worker: clone/fetch, ingest commits, pushes and CI results, detect breakages, and schedule
view rebuilds, reviews and Sonar scans. One worker thread runs jobs in order; a poller backs up the webhooks."""
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

from . import (BACKEND_REPO, BACKFILL_DAYS, DATA, DEFAULT_BRANCHES, FRONTEND_REPO, GIT_URL, POLL_SECONDS, REPOS,
               ROOT, SITE, SONAR_KEYS, SONAR_URL, WORK, db, llm, repo_dir)
from .github import gh

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
    db.x("INSERT INTO events(at, kind, detail, ok) VALUES(?, ?, ?, ?)", now(), kind, str(detail)[:4000], int(ok))


def enqueue(*job):
    with _lock:
        if job in _pending:
            return
        _pending.add(job)
    jobs.put(job)


# ---------- git ----------

def git(repo, *args, auth=False):
    r = subprocess.run(["git", *(gh.git_auth() if auth else []), *args], cwd=repo_dir(repo),
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
    r = subprocess.run(["git", *gh.git_auth(), "clone", "--quiet", url, str(d)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git clone {repo}: {r.stderr.strip()[-500:]}")


def default_branch(repo):
    if repo in DEFAULT_BRANCHES:
        return DEFAULT_BRANCHES[repo]
    if repo not in _defaults:
        _defaults[repo] = (gh.get(f"/repos/{repo}")["default_branch"] if gh.enabled else
                           git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip().removeprefix("origin/"))
    return _defaults[repo]


def checkout_defaults():
    for repo in REPOS:
        branch = default_branch(repo)
        git(repo, "checkout", "--quiet", "--force", "-B", branch, f"origin/{branch}")


# ---------- people ----------

def person_for(email, ids=None):
    """GitHub login for a git author email when we know it, else the email itself."""
    email = (email or "").lower()
    if m := NOREPLY.match(email):
        return m.group(1)
    if ids is None:
        r = db.q("SELECT login FROM identities WHERE email=?", email)
        return r[0]["login"] if r else email
    return ids.get(email, email)


def remember_identities(rows):
    """rows of (email, login, name); then re-point anything still keyed by those emails."""
    rows = [(e.lower(), login, name) for e, login, name in rows if e and login]
    if not rows:
        return
    db.xs("INSERT INTO identities(email, login, name) VALUES(?, ?, ?) "
          "ON CONFLICT(email) DO UPDATE SET login=excluded.login, name=excluded.name", rows)
    for table in ("commits", "findings", "breakages"):
        db.x(f"UPDATE {table} SET person=(SELECT login FROM identities i WHERE i.email={table}.person) "
             f"WHERE person LIKE '%@%' AND person IN (SELECT email FROM identities)")


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
    known = {r["sha"] for r in db.q("SELECT sha FROM commits WHERE repo=?", repo)}
    ids = {r["email"]: r["login"] for r in db.q("SELECT email, login FROM identities")}
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
        rows.append((repo, sha, person_for(email, ids), name, email.lower(), at, subject, adds, dels, files,
                     int(len(parents.split()) > 1)))
    db.xs("INSERT OR IGNORE INTO commits(repo, sha, person, author_name, author_email, committed_at, message, "
          "additions, deletions, files, is_merge) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    on_default = git(repo, "rev-list", f"--since={since}", f"origin/{default_branch(repo)}").split()
    db.xs("UPDATE commits SET on_default=1 WHERE repo=? AND sha=? AND on_default=0", [(repo, s) for s in on_default])
    return len(rows)


def record_push(repo, branch, before, after, pusher, pushed_at, forced, source):
    if after == ZERO or db.q("SELECT 1 FROM pushes WHERE repo=? AND branch=? AND before=? AND after=?",
                             repo, branch, before, after):
        return
    if not git_ok(repo, "cat-file", "-e", f"{after}^{{commit}}"):
        return  # not fetched yet; the next sync records it from the Events API
    if before != ZERO and git_ok(repo, "cat-file", "-e", f"{before}^{{commit}}"):
        shas = git(repo, "rev-list", "--max-count=250", f"{before}..{after}").split()
        if forced is None:
            forced = not git_ok(repo, "merge-base", "--is-ancestor", before, after)
    else:  # new branch: the commits that are not on the default branch yet
        shas = git(repo, "rev-list", "--max-count=250", after, "--not", f"origin/{default_branch(repo)}").split()
    pid = db.x("INSERT INTO pushes(repo, branch, before, after, pusher, pushed_at, forced, commits, source) "
               "VALUES(?,?,?,?,?,?,?,?,?)", repo, branch, before, after, pusher, pushed_at, int(bool(forced)), len(shas), source)
    db.xs("INSERT OR IGNORE INTO push_commits(push_id, sha) VALUES(?, ?)", [(pid, s) for s in shas])


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
                rows.append((repo, sha, r["name"], r["conclusion"], r["completed_at"], r["html_url"]))
    db.xs("INSERT INTO checks(repo, sha, name, conclusion, completed_at, url) VALUES(?,?,?,?,?,?) "
          "ON CONFLICT(repo, sha, name) DO UPDATE SET conclusion=excluded.conclusion, "
          "completed_at=excluded.completed_at, url=excluded.url WHERE excluded.completed_at >= checks.completed_at", rows)
    db.set_setting(f"checks_backfilled:{repo}", True)
    detect(repo, branch)


def detect(repo, branch, window=80):
    order = git(repo, "rev-list", "--first-parent", f"--max-count={window}", f"origin/{branch}").split()[::-1]
    if not order:
        return
    results, done_at = defaultdict(dict), {}
    marks = ",".join("?" * len(order))
    for r in db.q(f"SELECT sha, name, conclusion, completed_at FROM checks WHERE repo=? AND sha IN ({marks})", repo, *order):
        results[r["sha"]][r["name"]] = r["conclusion"]
        done_at[r["sha"], r["name"]] = r["completed_at"]
    for check, broken, good, fixed in detect_breakages(order, results):
        who = db.q("SELECT person FROM commits WHERE repo=? AND sha=?", repo, broken)
        pusher = db.q("SELECT p.pusher FROM pushes p JOIN push_commits pc ON pc.push_id=p.id "
                      "WHERE p.repo=? AND pc.sha=? ORDER BY p.pushed_at LIMIT 1", repo, broken)
        db.x("INSERT INTO breakages(repo, branch, check_name, broken_sha, last_good_sha, person, pusher, broken_at, "
             "fixed_sha, fixed_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(repo, branch, check_name, broken_sha) DO UPDATE "
             "SET fixed_sha=excluded.fixed_sha, fixed_at=excluded.fixed_at, pusher=COALESCE(breakages.pusher, excluded.pusher)",
             repo, branch, check, broken, good, who[0]["person"] if who else None, pusher[0]["pusher"] if pusher else None,
             done_at.get((broken, check)), fixed, done_at.get((fixed, check)) if fixed else None)


# ---------- jobs ----------

def check_head(repo):
    """A new default-branch head schedules a views rebuild, a Sonar scan and a review of the change."""
    branch = default_branch(repo)
    head = git(repo, "rev-parse", f"origin/{branch}").strip()
    seen = db.setting(f"head:{repo}")
    if head == seen:
        return
    db.set_setting(f"head:{repo}", head)
    enqueue("rebuild")
    enqueue("sonar")
    if seen:  # the first sync only sets the baseline
        enqueue("review_push", repo, branch, seen, head)


def poll_prs(repo):
    """Webhooks normally start PR reviews; this catches open PRs whose events never arrived
    (e.g. the portal's port is firewalled to office IPs)."""
    for pr in gh.paginate(f"/repos/{repo}/pulls", limit=100, state="open"):
        if not pr["draft"] and not db.q("SELECT 1 FROM reviews WHERE repo=? AND pr=? AND head_sha=?",
                                         repo, pr["number"], pr["head"]["sha"]):
            enqueue("review_pr", repo, pr["number"])


def sync(repo):
    fetch(repo)
    n = ingest_commits(repo)
    if gh.enabled:
        learn_identities(repo)
        ingest_push_events(repo)
        ingest_checks(repo)
        if llm.configured():
            poll_prs(repo)
    check_head(repo)
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
    req = repo_dir(BACKEND_REPO) / "requirements.txt"
    venv = DATA / "venvs" / "backend"
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
    d = repo_dir(FRONTEND_REPO)
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
    return dict(os.environ, BACKEND_DIR=str(repo_dir(BACKEND_REPO)), FRONTEND_DIR=str(repo_dir(FRONTEND_REPO)),
                SITE_DIR=str(SITE), WORK_DIR=str(WORK), **extra)


def run_build(*steps, **env):
    r = subprocess.run([sys.executable, str(ROOT / "build.py"), *steps], env=build_env(**env),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    failed = [line for line in r.stdout.splitlines() if line.startswith("!!!")]
    return r.returncode == 0 and not failed, "\n".join(failed) or r.stderr[-2000:]


def rebuild():
    checkout_defaults()
    py = backend_python()
    frontend_modules()
    ok, detail = run_build("graphs", "routes", "deps", BACKEND_PYTHON=py)
    log("rebuild", "views rebuilt" if ok else detail, ok)


def sonar_token():
    return db.setting("sonar_token") or os.environ.get("SONAR_TOKEN", "")


def sonar():
    token = sonar_token()
    heads = {r: git(r, "rev-parse", f"origin/{default_branch(r)}").strip() for r in REPOS}
    if not token or heads == db.setting("sonar_heads") or time.time() - db.setting("sonar_at", 0) < 900:
        return  # no token, nothing new, or scanned in the last 15 min (the poller retries)
    checkout_defaults()
    ok, detail = run_build("sonar", SONAR_TOKEN=token, SONAR_HOST_URL=SONAR_URL)
    db.set_setting("sonar_heads", heads)
    db.set_setting("sonar_at", time.time())
    log("sonar", "scanned" if ok else detail, ok)


def sonar_snapshot():
    """Open SonarQube issues per SCM author (Sonar blames each issue to the line's author)."""
    token = sonar_token()
    if not token:
        return
    today, rows = date.today().isoformat(), []
    for repo, key in SONAR_KEYS.items():
        for quality in ("RELIABILITY", "SECURITY", "MAINTAINABILITY"):
            r = httpx.get(f"{SONAR_URL}/api/issues/search", timeout=30, headers={"Authorization": f"Bearer {token}"},
                          params={"components": key, "impactSoftwareQualities": quality,
                                  "issueStatuses": "OPEN,CONFIRMED", "facets": "author", "ps": 1})
            if r.status_code == 404:
                break  # project not analysed yet
            r.raise_for_status()
            for facet in r.json().get("facets", []):
                if facet["property"] == "author":
                    rows += [(today, repo, v["val"].lower(), quality.lower(), v["count"]) for v in facet["values"]]
    db.xs("INSERT OR REPLACE INTO sonar_issues(taken_on, repo, author_email, quality, count) VALUES(?,?,?,?,?)", rows)


def handle_webhook(event, payload):
    repo = (payload.get("repository") or {}).get("full_name")
    if repo not in REPOS:
        return "ignored"
    if event == "push" and payload.get("ref", "").startswith("refs/heads/"):
        remember_identities([(c["author"].get("email"), c["author"].get("username"), c["author"].get("name"))
                             for c in payload.get("commits", []) if c.get("author")])
        pusher = (payload.get("sender") or {}).get("login") or (payload.get("pusher") or {}).get("name")
        enqueue("push", repo, payload["ref"][11:], payload["before"], payload["after"], pusher, now(), bool(payload.get("forced")))
    elif event == "pull_request" and payload.get("action") in ("opened", "synchronize", "reopened", "ready_for_review"):
        if not payload["pull_request"].get("draft"):
            enqueue("review_pr", repo, payload["number"])
    elif event in ("check_run", "check_suite") and payload.get("action") == "completed":
        enqueue("checks", repo)
    else:
        return "ignored"
    return "queued"


def handlers():
    from . import review  # review imports this module
    return {"sync": sync, "push": on_push, "checks": on_checks, "rebuild": rebuild, "sonar": sonar,
            "sonar_snapshot": sonar_snapshot, "review_pr": review.review_pr, "review_push": review.review_push}


def worker():
    run = handlers()
    while True:
        job = jobs.get()
        with _lock:
            _pending.discard(job)
        try:
            run[job[0]](*job[1:])
        except Exception as e:  # one bad job must not stop the queue
            log(job[0], f"{job[1:]}: {e!r}", False)
            traceback.print_exc()


def poller():
    last_snapshot = 0.0
    while True:
        for repo in REPOS:
            enqueue("sync", repo)
        enqueue("sonar")
        if time.time() - last_snapshot > 3600:
            enqueue("sonar_snapshot")
            last_snapshot = time.time()
        db.x("DELETE FROM events WHERE id < (SELECT MAX(id) - 5000 FROM events)")
        time.sleep(POLL_SECONDS)


def start():
    for target in (worker, poller):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()


def status():
    with _lock:
        pending = sorted({job[0] for job in _pending})
    return {"queued": jobs.qsize(), "pending": pending}
