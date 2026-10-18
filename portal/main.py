"""Portal web app: GitHub login, webhook receiver, JSON API, the generated views and the dynamic pages.

    uvicorn portal.main:app --host 0.0.0.0 --port 7000
"""
import asyncio
import html
import json
import os
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import ADMINS, ORG, PUBLIC_URL, REPOS, ROOT, SITE, SONAR_PUBLIC_URL, chat, db, llm, sync
from .github import gh, verify_signature
from .review import SEVERITIES

PAGES = ROOT / "portal" / "pages"
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' "
           "height='32' rx='8' fill='%235bbfa8'/%3E%3Ctext x='16' y='21' font-family='monospace' font-weight='700' "
           "font-size='14' text-anchor='middle' fill='%2306211b'%3Egg%3C/text%3E%3C/svg%3E")
PUBLIC = ("/auth/", "/webhook/github", "/healthz", "/static/style.css")
DEV_USER = os.environ.get("DEV_USER", "")  # local development only: honoured for loopback requests
# Shared passwords, for running without the GitHub App: admin can change Settings, team can see everything.
PASSWORDS = {role: pw for role, pw in (("admin", os.environ.get("ADMIN_PASSWORD", "")),
                                       ("team", os.environ.get("PORTAL_PASSWORD", ""))) if pw}
SECRETS = ("llm_api_key", "sonar_token")
WEIGHT = {"critical": 8, "major": 3, "minor": 1, "nit": 0.5}

@asynccontextmanager
async def lifespan(_app):
    db.init()
    if os.environ.get("DISABLE_WORKER") != "1":
        sync.start()
    yield


app = FastAPI(title="GeneGenius portal", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def current_user(request):
    # DEV_USER only works with no login method configured and for requests from this machine.
    if (DEV_USER and not gh.oauth_enabled and not PASSWORDS
            and request.client and request.client.host in ("127.0.0.1", "::1")):
        return {"login": DEV_USER, "admin": True}
    return request.session.get("user")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC) or current_user(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "login required"}, status_code=401)
    return RedirectResponse(f"/auth/login?next={quote(path)}")


# Added after the auth middleware, so it wraps it and the session exists by the time the check runs.
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
                   https_only=PUBLIC_URL.startswith("https://"), same_site="lax", max_age=7 * 24 * 3600)


def require_admin(request):
    user = current_user(request)
    if not (user and user.get("admin")):
        raise HTTPException(403, "admins only")
    return user


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- login ----------

def safe_next(path):
    return path if path.startswith("/") and not path.startswith("//") else "/"


def login_page(nxt, error="", status_code=200):
    github = (f'<a class="card" href="/auth/github?next={quote(nxt)}" style="margin-bottom:12px"><b>Sign in with GitHub</b>'
              f'<span>{ORG} members</span></a>') if gh.oauth_enabled else ""
    form = ('<form method="post" action="/auth/password" class="panel">'
            f'<input type="hidden" name="next" value="{quote(nxt)}">'
            '<label for="pw" style="margin-top:0">Password</label>'
            '<div class="row" style="margin:0"><input id="pw" name="password" type="password" autofocus required '
            'autocomplete="current-password" style="flex:1"><button class="primary">Sign in</button></div>'
            f'{f"<p class=error>{error}</p>" if error else ""}'
            '<p class="hint">The viewer password shows everything; the admin password can also change Settings.</p>'
            '</form>') if PASSWORDS else ""
    return HTMLResponse('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in</title>'
                        f'<link rel="icon" href="{FAVICON}"><link rel="stylesheet" href="/static/style.css"></head>'
                        '<body><main class="login"><div class="brand-lg"><span class="mark">gg</span>GeneGenius</div>'
                        '<h1>Engineering portal</h1><p class="lede">Architecture, activity, reviews and code quality '
                        f'for the GeneGenius repositories.</p>{github}{form}</main></body></html>', status_code=status_code)


@app.get("/auth/login")
def login(request: Request, next: str = "/"):
    if not (gh.oauth_enabled or PASSWORDS):
        return HTMLResponse("No login method is configured: set PORTAL_PASSWORD / ADMIN_PASSWORD, "
                            "or the GitHub App's GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET.", status_code=503)
    if gh.oauth_enabled and not PASSWORDS:
        return github_login(request, next)
    return login_page(safe_next(next))


@app.get("/auth/github")
def github_login(request: Request, next: str = "/"):
    if not gh.oauth_enabled:
        return RedirectResponse("/auth/login")
    state = secrets.token_urlsafe(24)
    request.session["oauth"] = {"state": state, "next": safe_next(next)}
    return RedirectResponse(gh.oauth_url(state, f"{PUBLIC_URL}/auth/callback"))


@app.post("/auth/password")
async def password_login(request: Request):
    form = parse_qs((await request.body()).decode(), keep_blank_values=True)
    given, nxt = form.get("password", [""])[0], safe_next(unquote(form.get("next", ["/"])[0]))
    role = next((role for role, secret in PASSWORDS.items()
                 if secrets.compare_digest(given.encode(), secret.encode())), None)
    if not role:
        await asyncio.sleep(1)  # slows down guessing
        return login_page(nxt, "Wrong password.", 401)
    request.session["user"] = {"login": role, "admin": role == "admin"}
    return RedirectResponse(nxt, status_code=303)


@app.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = ""):
    saved = request.session.pop("oauth", None) or {}
    if not code or not secrets.compare_digest(state, saved.get("state", "")):
        return HTMLResponse("Login expired or invalid; <a href='/auth/login'>try again</a>.", status_code=400)
    try:
        login_name = gh.oauth_login(code, f"{PUBLIC_URL}/auth/callback")
    except Exception as e:
        return HTMLResponse(f"GitHub login failed: {e}", status_code=400)
    if not gh.is_member(login_name):
        return HTMLResponse(f"{login_name} is not a member of the {ORG} GitHub organisation.", status_code=403)
    request.session["user"] = {"login": login_name, "admin": login_name.lower() in ADMINS}
    return RedirectResponse(saved.get("next") or "/")


@app.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return HTMLResponse("Signed out. <a href='/auth/login'>Sign in again</a>.")


# ---------- GitHub webhook ----------

@app.post("/webhook/github")
async def webhook(request: Request):
    body = await request.body()
    if not verify_signature(os.environ.get("GITHUB_WEBHOOK_SECRET", ""), body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "bad signature")
    result = await run_in_threadpool(sync.handle_webhook, request.headers.get("X-GitHub-Event", ""), json.loads(body))
    return {"result": result}


# ---------- read API ----------

def since(days):
    return (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 3650)))).isoformat()


@app.get("/api/me")
def me(request: Request):
    return current_user(request)


@app.get("/api/summary")
def summary():
    ci = {}
    for side, repo in zip(("backend", "frontend"), REPOS):
        head = db.setting(f"head:{repo}")
        rows = db.q("SELECT conclusion FROM checks WHERE repo=? AND sha=?", repo, head) if head else []
        ci[side] = {"repo": repo, "head": head, "total": len(rows),
                    "passed": sum(r["conclusion"] in ("success", "skipped", "neutral") for r in rows),
                    "failed": sum(r["conclusion"] in ("failure", "timed_out") for r in rows)}
    return {"ci": ci, "sonar_url": SONAR_PUBLIC_URL or None,
            "open_breakages": db.q("SELECT COUNT(*) n FROM breakages WHERE fixed_sha IS NULL")[0]["n"],
            "worker": sync.status(), "github": gh.mode, "llm": llm.configured()}


@app.get("/api/activity")
def activity(repo: str = "", branch: str = "", person: str = "", days: int = 30, limit: int = 100):
    where, args = ["p.pushed_at >= ?"], [since(days)]
    for col, val in (("p.repo", repo), ("p.branch", branch), ("p.pusher", person)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    pushes = db.q(f"SELECT * FROM pushes p WHERE {' AND '.join(where)} ORDER BY p.pushed_at DESC LIMIT ?",
                  *args, min(limit, 500))
    for p in pushes:
        p["commit_list"] = db.q("SELECT c.sha, c.person, c.message, c.additions, c.deletions FROM push_commits pc "
                                "JOIN commits c ON c.repo=? AND c.sha=pc.sha WHERE pc.push_id=? "
                                "ORDER BY c.committed_at DESC LIMIT 50", p["repo"], p["id"])
        p["checks"] = db.q("SELECT name, conclusion, url FROM checks WHERE repo=? AND sha=?", p["repo"], p["after"])
        p["broke"] = [r["check_name"] for r in db.q(
            "SELECT check_name FROM breakages WHERE repo=? AND broken_sha IN "
            "(SELECT sha FROM push_commits WHERE push_id=?)", p["repo"], p["id"])]
        p["reviews"] = db.q("SELECT r.id, r.pr, COUNT(f.id) findings FROM reviews r LEFT JOIN findings f "
                            "ON f.review_id=r.id WHERE r.repo=? AND r.head_sha=? GROUP BY r.id", p["repo"], p["after"])
    c_where, c_args = ["committed_at >= ?"], [since(days)]
    for col, val in (("repo", repo), ("person", person)):
        if val:
            c_where.append(f"{col} = ?")
            c_args.append(val)
    commits = db.q(f"SELECT repo, sha, person, author_name, committed_at, message, additions, deletions, is_merge, "
                   f"on_default FROM commits WHERE {' AND '.join(c_where)} ORDER BY committed_at DESC LIMIT ?",
                   *c_args, min(limit, 500))
    return {"pushes": pushes, "commits": commits}


@app.get("/api/people")
def people(days: int = 90):
    t = since(days)
    stats = defaultdict(lambda: defaultdict(float))
    for r in db.q("SELECT person, COUNT(*) commits, SUM(additions) adds, SUM(deletions) dels FROM commits "
                  "WHERE committed_at >= ? AND is_merge = 0 GROUP BY person", t):
        stats[r["person"]].update(commits=r["commits"], adds=r["adds"] or 0, dels=r["dels"] or 0)
    for r in db.q("SELECT pusher person, COUNT(*) n FROM pushes WHERE pushed_at >= ? GROUP BY pusher", t):
        stats[r["person"]]["pushes"] = r["n"]
    for r in db.q("SELECT person, COUNT(*) n, SUM(fixed_sha IS NULL) open FROM breakages "
                  "WHERE broken_at >= ? GROUP BY person", t):
        stats[r["person"]].update(breakages=r["n"], open_breakages=r["open"])
    for r in db.q("SELECT f.person, f.severity, f.category, COUNT(*) n FROM findings f JOIN reviews r "
                  "ON r.id = f.review_id WHERE r.created_at >= ? GROUP BY 1, 2, 3", t):
        s = stats[r["person"]]
        s[r["severity"]] += r["n"]
        s["findings"] += r["n"]
        if r["category"] == "slop":
            s["slop"] += r["n"]
    latest = db.q("SELECT MAX(taken_on) d FROM sonar_issues")[0]["d"]
    if latest:
        ids = {r["email"]: r["login"] for r in db.q("SELECT email, login FROM identities")}
        for r in db.q("SELECT author_email, quality, SUM(count) n FROM sonar_issues WHERE taken_on=? GROUP BY 1, 2", latest):
            stats[sync.person_for(r["author_email"], ids)][f"sonar_{r['quality']}"] += r["n"]
    out = []
    for person, s in stats.items():
        if not person:
            continue
        weighted = sum(WEIGHT[sev] * s[sev] for sev in SEVERITIES)
        out.append({"person": person, **{k: int(v) for k, v in s.items()},
                    "weighted_per_kloc": round(weighted * 1000 / s["adds"], 2) if s["adds"] and s["findings"] else None})
    return sorted(out, key=lambda p: -p.get("commits", 0))


@app.get("/api/people/{person}")
def person_detail(person: str, days: int = 90):
    t = since(days)
    return {
        "commits": db.q("SELECT repo, sha, committed_at, message, additions, deletions, on_default FROM commits "
                        "WHERE person=? AND committed_at >= ? ORDER BY committed_at DESC LIMIT 200", person, t),
        "breakages": db.q("SELECT * FROM breakages WHERE person=? ORDER BY broken_at DESC LIMIT 100", person),
        "findings": db.q("SELECT f.*, r.repo, r.pr, r.branch FROM findings f JOIN reviews r ON r.id=f.review_id "
                         "WHERE f.person=? ORDER BY f.id DESC LIMIT 200", person),
    }


@app.get("/api/breakages")
def breakages(days: int = 90):
    return db.q("SELECT * FROM breakages WHERE broken_at >= ? OR fixed_sha IS NULL ORDER BY broken_at DESC", since(days))


@app.get("/api/reviews")
def reviews(limit: int = 100):
    return db.q("SELECT r.*, COUNT(f.id) findings, SUM(f.severity='critical') critical, SUM(f.severity='major') major, "
                "SUM(f.category='slop') slop FROM reviews r LEFT JOIN findings f ON f.review_id=r.id "
                "GROUP BY r.id ORDER BY r.id DESC LIMIT ?", min(limit, 500))


@app.get("/api/reviews/{rid}")
def review_detail(rid: int):
    r = db.q("SELECT * FROM reviews WHERE id=?", rid)
    if not r:
        raise HTTPException(404)
    return {**r[0], "findings": db.q("SELECT * FROM findings WHERE review_id=? ORDER BY "
                                     "CASE severity WHEN 'critical' THEN 0 WHEN 'major' THEN 1 WHEN 'minor' THEN 2 "
                                     "ELSE 3 END, path, line", rid)}


@app.get("/api/events")
def events():
    return db.q("SELECT * FROM events ORDER BY id DESC LIMIT 100")


@app.post("/api/chat")
async def ask(request: Request):
    body = await request.json()
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "question is required")
    try:
        return await run_in_threadpool(chat.answer, body.get("history") or [], question)
    except llm.LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ---------- settings and admin actions ----------

def settings_view():
    cfg = llm.config()
    out = {k: cfg[k] for k in llm.DEFAULTS}
    current = {"llm_api_key": cfg["llm_api_key"], "sonar_token": sync.sonar_token()}  # saved, or from .env
    for key in SECRETS:
        value = current[key]
        out[key] = f"saved, ends in {value[-4:]}" if value else ""
    out["post_reviews"] = db.setting("post_reviews", False)
    return out


@app.get("/api/settings")
def get_settings():
    return settings_view()


def merged_settings(body):
    """Settings from a form: blank secret fields keep the stored secret."""
    cfg = llm.config()
    for key in llm.DEFAULTS:
        if key in body:
            cfg[key] = str(body[key]).strip()
    if body.get("llm_api_key"):
        cfg["llm_api_key"] = str(body["llm_api_key"]).strip()
    if cfg["llm_kind"] not in ("anthropic", "openai"):
        raise HTTPException(400, "llm_kind must be anthropic or openai")
    if cfg["llm_base_url"] and not cfg["llm_base_url"].startswith(("https://", "http://")):
        raise HTTPException(400, "base URL must start with https:// or http://")
    if cfg["llm_effort"] not in ("", "low", "medium", "high", "xhigh", "max"):
        raise HTTPException(400, "effort must be blank, low, medium, high, xhigh or max")
    return cfg


@app.put("/api/settings")
async def put_settings(request: Request):
    require_admin(request)
    body = await request.json()
    cfg = merged_settings(body)
    for key in (*llm.DEFAULTS, "llm_api_key"):
        db.set_setting(key, cfg[key])
    if body.get("sonar_token"):
        db.set_setting("sonar_token", str(body["sonar_token"]).strip())
    if "post_reviews" in body:
        db.set_setting("post_reviews", bool(body["post_reviews"]))
    return settings_view()


@app.post("/api/settings/test")
async def test_settings(request: Request):
    require_admin(request)
    cfg = merged_settings(await request.json())
    try:
        return {"ok": True, "reply": await run_in_threadpool(llm.ping, cfg)}
    except llm.LLMError as e:
        return {"ok": False, "reply": str(e)}


@app.post("/api/admin/{action}")
async def admin_action(action: str, request: Request):
    require_admin(request)
    body = await request.json() if await request.body() else {}
    if action == "sync":
        for repo in REPOS:
            sync.enqueue("sync", repo)
    elif action == "rebuild":
        sync.enqueue("rebuild")
    elif action == "review" and body.get("repo") in REPOS and str(body.get("pr", "")).isdigit():
        sync.enqueue("review_pr", body["repo"], int(body["pr"]))
    else:
        raise HTTPException(400, "unknown action")
    return {"queued": action, **sync.status()}


# ---------- pages ----------

def page_route(file):
    return lambda: FileResponse(file)  # no parameters, so FastAPI reads no query args


for name in ("activity", "people", "reviews", "chat", "settings"):
    app.add_api_route(f"/{name}", page_route(PAGES / f"{name}.html"), methods=["GET"], include_in_schema=False)
app.mount("/static", StaticFiles(directory=PAGES), name="static")


@app.get("/", include_in_schema=False)
def index():
    if (SITE / "index.html").exists():
        return FileResponse(SITE / "index.html")
    failed = db.q("SELECT detail FROM events WHERE kind='sync' AND ok=0 ORDER BY id DESC LIMIT 1")
    problem = (f"<p class='error'>The last sync failed:</p><pre style='white-space:pre-wrap'>"
               f"{html.escape(failed[0]['detail'])}</pre>") if failed else ""
    return HTMLResponse("<!doctype html><html><head><meta charset='utf-8'><title>GeneGenius portal</title>"
                        f"<link rel=\"icon\" href=\"{FAVICON}\"><link rel='stylesheet' href='/static/style.css'></head><body>"
                        "<main class='login' style='max-width:640px'><div class='brand-lg'><span class='mark'>gg</span>GeneGenius</div>"
                        "<h1>Getting things ready</h1><p class='lede'>The first sync and build are running. This page fills in "
                        "when they finish, usually a few minutes. <a href='/settings'>Settings</a> shows progress, and "
                        "<a href='/activity'>Activity</a> and <a href='/chat'>Ask</a> work already.</p>" + problem
                        + "</main></body></html>")


app.mount("/", StaticFiles(directory=SITE, html=True, check_dir=False), name="site")
