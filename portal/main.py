"""Portal web app: GitHub login, workspaces and their members, repository import, webhook receiver, JSON API,
the generated views and the dynamic pages.

    uvicorn portal.main:app --host 0.0.0.0 --port 7000
"""
import difflib
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import time
import zipfile
from collections import defaultdict
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import PUBLIC_URL, ROOT, SONAR_PUBLIC_URL, WORKSPACE, chat, db, docgen, llm, mail, mcp, site_dir, sync, workspace
from .github import gh, verify_signature
from .review import SEVERITIES

WEB = ROOT / "web" / "dist"  # the Astro frontend: cd web && npm run build
# No session needed: /mcp has its own token, the webhook its signature, /invite/ sends people to sign in itself.
PUBLIC = ("/auth/", "/webhook/github", "/healthz", "/login", "/_astro/", "/favicon.svg", "/mcp", "/invite/")
NO_WORKSPACE = ("/api/me", "/api/workspaces", "/api/invites/", "/welcome")  # before creating or joining one
# owner/name; neither part "." or "..", which would put the clone outside the workspace's repos folder
REPO_NAME = re.compile(r"^(?!\.{1,2}/)[A-Za-z0-9_.-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+$")
# What people paste: https://github.com/o/n(.git)(/tree/...), git@github.com:o/n.git, or o/n. GitHub account names
# have no dots, so "gitlab.com/o/n" can't pass for an owner.
GITHUB_REPO = re.compile(r"(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)?"
                         r"([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/?#].*)?")
DEV_USER = os.environ.get("DEV_USER", "")  # local development only: honoured for loopback requests
ALLOW_PRIVATE_LLM = os.environ.get("ALLOW_PRIVATE_LLM") == "1"  # self-hosted, one tenant: LLM servers on the LAN
ROLES = ("owner", "admin", "member")
INVITE_DAYS, INVITES_PER_DAY = 7, 50
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
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
    # DEV_USER only works with GitHub sign-in not configured and for requests from this machine.
    if DEV_USER and not gh.oauth_enabled and request.client and request.client.host in ("127.0.0.1", "::1"):
        return {"login": DEV_USER}
    login = request.session.get("login")
    return {"login": login} if login else None


def memberships(login):
    return db.cq("SELECT w.id, w.name, m.role FROM members m JOIN workspaces w ON w.id = m.workspace_id "
                 "WHERE m.login = ? ORDER BY w.name, w.id", login)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Signed in, and inside a workspace: the one picked in the session while the user is still a member of it,
    else their first. Everything downstream reads WORKSPACE, so a request can only reach its own tenant."""
    path = request.url.path
    if path.startswith(PUBLIC):
        return await call_next(request)
    user = current_user(request)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"error": "login required"}, status_code=401)
        return RedirectResponse(f"/auth/login?next={quote(path)}")
    request.state.workspaces = rows = memberships(user["login"])
    request.state.ws = ws = next((w for w in rows if w["id"] == request.session.get("ws")), rows[0] if rows else None)
    if ws:
        with workspace(ws["id"]):
            return await call_next(request)
    if path.startswith(NO_WORKSPACE):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "create or join a workspace first"}, status_code=409)
    return RedirectResponse("/welcome/")


# Added after the auth middleware, so it wraps it and the session exists by the time the check runs.
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
                   https_only=PUBLIC_URL.startswith("https://"), same_site="lax", max_age=7 * 24 * 3600)


def role(request):
    ws = getattr(request.state, "ws", None)
    return ws["role"] if ws else None


def require_admin(request):
    if role(request) not in ("owner", "admin"):
        raise HTTPException(403, "workspace owners and admins only")
    return current_user(request)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- login ----------

def safe_next(path):  # same-site paths only: "//host" and "/\host" both leave the site in browsers
    return path if path.startswith("/") and not path.startswith(("//", "/\\")) else "/"


def to_login(nxt="/", error=""):
    """The sign-in page (web/src/pages/login.astro) shows `error` as text."""
    return RedirectResponse(f"/login/?next={quote(safe_next(nxt))}" + (f"&error={quote(error)}" if error else ""), 303)


@app.get("/auth/methods")
def methods():
    return {"github": gh.oauth_enabled}


@app.get("/auth/login")
def login(next: str = "/"):
    if not gh.oauth_enabled:
        return HTMLResponse("Sign-in needs the GitHub App: set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET (README 3.1). "
                            "For local development, DEV_USER signs in requests from this machine.", status_code=503)
    return to_login(next)


@app.get("/auth/github")
def github_login(request: Request, next: str = "/"):
    if not gh.oauth_enabled:
        return RedirectResponse("/auth/login")
    state = secrets.token_urlsafe(24)
    request.session["oauth"] = {"state": state, "next": safe_next(next)}
    return RedirectResponse(gh.oauth_url(state, f"{PUBLIC_URL}/auth/callback"))


@app.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = ""):
    saved = request.session.pop("oauth", None) or {}
    if not code or not secrets.compare_digest(state, saved.get("state", "")):
        return to_login(error="The GitHub sign-in expired. Try again.")
    try:
        u = gh.oauth_login(code, f"{PUBLIC_URL}/auth/callback")
    except Exception as e:
        return to_login(error=f"GitHub sign-in failed: {e}")
    db.cx("INSERT INTO users(login, name, avatar_url, token, refresh_token, token_expires, created_at, last_login) "
          "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(login) DO UPDATE SET name=excluded.name, avatar_url=excluded.avatar_url, "
          "token=excluded.token, refresh_token=excluded.refresh_token, token_expires=excluded.token_expires, "
          "last_login=excluded.last_login", u["login"], u["name"], u["avatar_url"], u["token"], u["refresh_token"],
          u["token_expires"], db.now(), db.now())
    request.session["login"] = u["login"]
    return RedirectResponse(saved.get("next") or "/")


@app.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return to_login()


# ---------- workspaces ----------

@app.get("/api/me")
def me(request: Request):
    ws, login = request.state.ws, current_user(request)["login"]
    avatar = db.cq("SELECT avatar_url FROM users WHERE login=?", login)
    return {"login": login, "avatar_url": avatar[0]["avatar_url"] if avatar else None,
            "admin": role(request) in ("owner", "admin"), "role": role(request),
            "workspace": {"id": ws["id"], "name": ws["name"]} if ws else None, "workspaces": request.state.workspaces}


def clean_name(text):
    return " ".join(str(text).split())[:80]  # one line: it goes into email subjects


@app.post("/api/workspaces")
async def create_workspace(request: Request):
    name = clean_name((await request.json()).get("name", ""))
    if not name:
        raise HTTPException(400, "give the workspace a name")
    request.session["ws"] = db.create_workspace(name, current_user(request)["login"])
    return {"id": request.session["ws"], "name": name}


@app.post("/api/workspaces/{ws}/switch")
def switch_workspace(ws: int, request: Request):
    if not any(w["id"] == ws for w in request.state.workspaces):
        raise HTTPException(403, "you are not a member of that workspace")
    request.session["ws"] = ws
    return {"id": ws}


@app.put("/api/workspace")
async def rename_workspace(request: Request):
    require_admin(request)
    name = clean_name((await request.json()).get("name", ""))
    if not name:
        raise HTTPException(400, "give the workspace a name")
    db.cx("UPDATE workspaces SET name=? WHERE id=?", name, WORKSPACE.get())
    return {"name": name}


# ---------- members and email invites ----------

def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def invite_cutoff():
    return (datetime.now(timezone.utc) - timedelta(days=INVITE_DAYS)).isoformat(timespec="seconds")


@app.get("/api/workspace/members")
def members(request: Request):
    ws = WORKSPACE.get()
    out = {"members": db.cq("SELECT m.login, m.role, m.joined_at, u.name, u.avatar_url FROM members m LEFT JOIN users u "
                            "ON u.login = m.login WHERE m.workspace_id=? ORDER BY m.joined_at, m.login", ws),
           "invites": [], "email": bool(mail.HOST)}
    if role(request) in ("owner", "admin"):
        out["invites"] = db.cq("SELECT id, email, role, invited_by, created_at FROM invites WHERE workspace_id=? "
                               "AND accepted_at IS NULL AND created_at >= ? ORDER BY id DESC", ws, invite_cutoff())
    return out


@app.post("/api/workspace/invites")
async def invite(request: Request):
    inviter = require_admin(request)["login"]
    body = await request.json()
    email, new_role = str(body.get("email", "")).strip().lower(), body.get("role", "member")
    if len(email) > 254 or not EMAIL.match(email):
        raise HTTPException(400, "that doesn't look like an email address")
    if new_role not in ROLES or (new_role == "owner" and role(request) != "owner"):
        raise HTTPException(400 if new_role not in ROLES else 403, "role must be member or admin (owners can invite owners)")
    if db.cq("SELECT COUNT(*) n FROM invites WHERE workspace_id=? AND created_at >= ?", WORKSPACE.get(),
             (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds"))[0]["n"] >= INVITES_PER_DAY:
        raise HTTPException(429, f"at most {INVITES_PER_DAY} invites a day per workspace")  # this server's mail is not a spam relay
    token = secrets.token_urlsafe(32)
    db.cx("INSERT INTO invites(workspace_id, email, role, token_hash, invited_by, created_at) VALUES(?,?,?,?,?,?)",
          WORKSPACE.get(), email, new_role, digest(token), inviter, db.now())
    link = f"{PUBLIC_URL}/invite/{token}"
    try:
        sent, error = await run_in_threadpool(mail.send_invite, email, request.state.ws["name"], inviter, link,
                                              INVITE_DAYS), None
    except Exception as e:  # the invite stands; the inviter can still copy the link
        sent, error = False, f"The email could not be sent ({e}). Copy the link instead."
    return {"link": link, "emailed": sent, "error": error}


@app.delete("/api/workspace/invites/{iid}")
def revoke_invite(iid: int, request: Request):
    require_admin(request)
    db.cx("DELETE FROM invites WHERE id=? AND workspace_id=? AND accepted_at IS NULL", iid, WORKSPACE.get())
    return {"ok": True}


USED = "This invite link was already used, was revoked or has expired. Ask for a new one."


@app.get("/invite/{token}")
def open_invite(token: str, request: Request):
    """The link in the email: sign in first, then the welcome page asks before joining. Joining on this GET would let
    any site navigate a signed-in user into an attacker's workspace."""
    if not current_user(request):
        return to_login(f"/invite/{token}")
    return RedirectResponse(f"/welcome/?invite={quote(token)}", 303)


@app.get("/api/invites/{token}")
def invite_preview(token: str):
    inv = db.cq("SELECT w.name workspace, i.role, i.invited_by FROM invites i JOIN workspaces w ON w.id = i.workspace_id "
                "WHERE i.token_hash=? AND i.accepted_at IS NULL AND i.created_at >= ?", digest(token), invite_cutoff())
    if not inv:
        raise HTTPException(404, USED)
    return inv[0]


@app.post("/api/invites/{token}/accept")
def accept_invite(token: str, request: Request):
    login = current_user(request)["login"]
    with closing(db.connect(path=db.CONTROL)) as con, con:  # claiming the invite and joining are one transaction
        claimed = con.execute("UPDATE invites SET accepted_by=?, accepted_at=? WHERE token_hash=? AND accepted_at IS NULL "
                              "AND created_at >= ?", (login, db.now(), digest(token), invite_cutoff())).rowcount
        inv = con.execute("SELECT workspace_id, role FROM invites WHERE token_hash=?", (digest(token),)).fetchone()
        if claimed:
            con.execute("INSERT OR IGNORE INTO members(workspace_id, login, role, joined_at) VALUES(?,?,?,?)",
                        (inv["workspace_id"], login, inv["role"], db.now()))
    if not claimed:
        raise HTTPException(404, USED)
    request.session["ws"] = inv["workspace_id"]
    return {"id": inv["workspace_id"]}


def member(login):
    m = db.cq("SELECT login, role FROM members WHERE workspace_id=? AND login=?", WORKSPACE.get(), login)
    if not m:
        raise HTTPException(404, "no such member")
    return m[0]


def owners():
    return db.cq("SELECT COUNT(*) n FROM members WHERE workspace_id=? AND role='owner'", WORKSPACE.get())[0]["n"]


@app.put("/api/workspace/members/{login}")
async def set_role(login: str, request: Request):
    require_admin(request)
    new_role, m = (await request.json()).get("role"), member(login)
    if new_role not in ROLES:
        raise HTTPException(400, "role must be owner, admin or member")
    if "owner" in (new_role, m["role"]) and role(request) != "owner":
        raise HTTPException(403, "only owners can make or unmake owners")
    if m["role"] == "owner" and new_role != "owner" and owners() == 1:
        raise HTTPException(400, "a workspace needs at least one owner")
    db.cx("UPDATE members SET role=? WHERE workspace_id=? AND login=?", new_role, WORKSPACE.get(), m["login"])
    return {"login": m["login"], "role": new_role}


@app.delete("/api/workspace/members/{login}")
def remove_member(login: str, request: Request):
    m = member(login)
    leaving = m["login"].lower() == current_user(request)["login"].lower()
    if not leaving:
        require_admin(request)
    if m["role"] == "owner" and not leaving and role(request) != "owner":
        raise HTTPException(403, "only owners can remove owners")
    if m["role"] == "owner" and owners() == 1:
        raise HTTPException(400, "a workspace needs at least one owner: make someone else owner first")
    db.cx("DELETE FROM members WHERE workspace_id=? AND login=?", WORKSPACE.get(), m["login"])
    if leaving:
        request.session.pop("ws", None)
    return {"ok": True}


# ---------- importing repositories (paste a URL, or pick one the GitHub App can read) ----------

def parse_repo(text):
    """owner/name from what people paste, or None."""
    m = GITHUB_REPO.fullmatch(str(text).strip())
    repo = m and f"{m[1]}/{m[2]}"
    return repo if repo and REPO_NAME.match(repo) else None


def user_token(login):
    """The user's GitHub token, refreshed when it has expired (App user tokens last 8 hours)."""
    u = db.cq("SELECT token, refresh_token, token_expires FROM users WHERE login=?", login)
    if not (u and u[0]["token"]):
        raise HTTPException(401, "Sign in with GitHub again.")
    u = u[0]
    if not u["token_expires"] or time.time() < u["token_expires"] - 60:
        return u["token"]
    try:
        t = gh.refresh(u["refresh_token"] or "")
    except (httpx.HTTPError, PermissionError):
        raise HTTPException(401, "Your GitHub sign-in expired: sign in again.")
    db.cx("UPDATE users SET token=?, refresh_token=?, token_expires=? WHERE login=?",
          t["token"], t["refresh_token"], t["token_expires"], login)
    return t["token"]


def check_access(login, repo):
    """The canonical owner/name when this user may import repo. The App may be installed on other tenants'
    private repositories, so what decides is the user's own GitHub access, plus the App being installed (it clones,
    reads CI and sends the webhooks). LookupError: not readable through the App, so the user should install it."""
    if gh.mode == "app":
        r = gh.request("GET", f"/repos/{repo}", check=False, token=user_token(login))
        if r.status_code == 401:
            raise HTTPException(401, "Your GitHub sign-in expired: sign in again.")
        if r.status_code != 200:
            raise LookupError(f"GitHub didn't let your account read {repo} through the App. Install the App on it "
                              "(or ask an owner of the repository to), then import again.")
        repo = r.json()["full_name"]
        if not gh.installation(repo):
            raise LookupError(f"The GitHub App isn't installed on {repo} yet. Install it, then import again.")
    elif gh.mode == "token":  # one operator: the server's own token decides
        r = gh.request("GET", f"/repos/{repo}", check=False)
        if r.status_code != 200:
            raise HTTPException(404, f"GITHUB_TOKEN can't read {repo}.")
        repo = r.json()["full_name"]
    return repo


@app.get("/api/import/repos")
def importable(request: Request):
    """Repositories the user can import: every one their GitHub account reaches through the App's installations."""
    login = require_admin(request)["login"]
    if gh.mode != "app":
        return {"repos": [], "install_url": None}
    try:
        found = gh.user_repos(user_token(login))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(401, "Your GitHub sign-in expired: sign in again.")
        raise
    have = {r.lower() for r in db.repos()}
    found.sort(key=lambda r: r["pushed_at"] or "", reverse=True)
    return {"repos": [{**r, "imported": r["repo"].lower() in have} for r in found], "install_url": gh.install_url()}


@app.post("/api/import")
async def import_repo(request: Request):
    """Vercel-style: a GitHub URL in, the repository cloned into this workspace, then kept in sync by webhooks."""
    login = require_admin(request)["login"]
    repo = parse_repo((await request.json()).get("url", ""))
    if not repo:
        raise HTTPException(400, "Paste a GitHub repository: https://github.com/owner/name or owner/name.")
    try:
        repo = await run_in_threadpool(check_access, login, repo)
    except LookupError as e:
        return JSONResponse({"error": str(e), "install_url": gh.install_url()}, status_code=400)
    name = repo.split("/")[1].lower()
    if name in {r.split("/")[1].lower() for r in db.repos()}:  # one clone folder and docs URL per name
        raise HTTPException(400, f"this workspace already has a repository named {repo.split('/')[1]}")
    db.add_repo(WORKSPACE.get(), repo, login)
    sync.enqueue("sync", repo)  # clone, history, PRs, CI, then its documentation
    return {"repo": repo, **sync.status()}


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


@app.get("/api/summary")
def summary():
    ci = {}
    for side, repo in zip(("backend", "frontend"), sync.views()):
        head = db.setting(f"head:{repo}")
        rows = db.q("SELECT conclusion FROM checks WHERE repo=? AND sha=?", repo, head) if head else []
        ci[side] = {"repo": repo, "head": head, "total": len(rows),
                    "passed": sum(r["conclusion"] in ("success", "skipped", "neutral") for r in rows),
                    "failed": sum(r["conclusion"] in ("failure", "timed_out") for r in rows)}
    last_sync = db.q("SELECT ok, detail FROM events WHERE kind='sync' ORDER BY id DESC LIMIT 1")
    return {"ci": ci, "sonar_url": SONAR_PUBLIC_URL or None,
            "open_breakages": db.q("SELECT COUNT(*) n FROM breakages WHERE fixed_sha IS NULL")[0]["n"],
            "worker": sync.status(), "github": gh.mode, "llm": llm.configured(), "llm_model": llm.config()["llm_model"],
            "repos": db.repos(), "views": bool(sync.views()), "docs_writing": docgen.writing(),
            "sync_error": last_sync[0]["detail"] if last_sync and not last_sync[0]["ok"] else None}


@app.get("/api/activity")
def activity(repo: str = "", branch: str = "", person: str = "", days: int = 30, limit: int = 100):
    limit = max(1, min(limit, 500))  # SQLite reads LIMIT -1 as "no limit"
    where, args = ["p.pushed_at >= ?"], [since(days)]
    for col, val in (("p.repo", repo), ("p.branch", branch), ("p.pusher", person)):
        if val:
            where.append(f"{col} = ?")
            args.append(val)
    pushes = db.q(f"SELECT * FROM pushes p WHERE {' AND '.join(where)} ORDER BY p.pushed_at DESC LIMIT ?", *args, limit)
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
                   *c_args, limit)
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
                "GROUP BY r.id ORDER BY r.id DESC LIMIT ?", max(1, min(limit, 500)))


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
    history = body.get("history")
    history = [m for m in history if isinstance(m, dict)] if isinstance(history, list) else []
    try:
        return await run_in_threadpool(chat.answer, history, question)
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
    if cfg["llm_base_url"] and not outside(cfg["llm_base_url"]):
        raise HTTPException(400, "base URL must be a public address (or this server's local model)")
    if cfg["llm_effort"] not in ("", "low", "medium", "high", "xhigh", "max"):
        raise HTTPException(400, "effort must be blank, low, medium, high, xhigh or max")
    return cfg


def outside(url):
    """True for a public host. Tenants share this server, so their LLM URL must not reach its insides (cloud
    metadata, SonarQube, other containers). ponytail: checked when saved; a per-request check if DNS rebinding matters."""
    if ALLOW_PRIVATE_LLM or url.rstrip("/").startswith(llm.OLLAMA_URL):
        return True
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(urlsplit(url).hostname or "", None)}
    except (OSError, UnicodeError):
        return False
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


@app.put("/api/settings")
async def put_settings(request: Request):
    require_admin(request)
    body = await request.json()
    cfg = merged_settings(body)
    # Store only what differs from .env, so later .env changes (install.sh LOCAL_LLM) still take effect.
    for key, default in {**llm.DEFAULTS, "llm_api_key": os.environ.get("LLM_API_KEY", "")}.items():
        if cfg[key] == default:
            db.x("DELETE FROM settings WHERE key=?", key)
        else:
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
        return {"ok": True, **await run_in_threadpool(llm.ping, cfg)}
    except llm.LLMError as e:
        return {"ok": False, "reply": str(e)}


@app.post("/api/settings/models")
async def list_models(request: Request):
    """Model ids the endpoint in the form offers (NVIDIA, OpenRouter and the like have hundreds)."""
    require_admin(request)
    cfg = merged_settings(await request.json())
    try:
        return {"models": await run_in_threadpool(llm.models, cfg)}
    except llm.LLMError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/settings/local")
def local_llm():
    """Models already downloaded into the local Ollama, for the one-click "Local model" choice in Settings."""
    return {"base_url": f"{llm.OLLAMA_URL}/v1", "models": llm.local_models()}


@app.get("/api/settings/mcp")
def mcp_settings(request: Request):
    require_admin(request)
    token = db.cq("SELECT mcp_token FROM workspaces WHERE id=?", WORKSPACE.get())[0]["mcp_token"]
    return {"url": f"{PUBLIC_URL}/mcp", "token_url": f"{PUBLIC_URL}/mcp/{token}", "token": token}


# ---------- generated docs (portal/docgen.py) ----------

def repo_named(name):
    repo = next((r for r in db.repos() if r.split("/")[1] == name), None)
    if not repo:
        raise HTTPException(404, "unknown repository")
    return repo


DOC_ORDER = ("CASE kind WHEN 'overview' THEN 0 WHEN 'architecture' THEN 1 WHEN 'data-flow' THEN 2 "
             "WHEN 'system-design' THEN 3 WHEN 'ml-system' THEN 4 WHEN 'api' THEN 5 ELSE 6 END, title")


@app.get("/api/docs")
def docs_index():
    return [{"repo": r, "name": r.split("/")[1], "status": docgen.status(r),
             "docs": db.q("SELECT slug, kind, title, summary, status, error, model, head_sha, generated_at FROM docs "
                          f"WHERE repo=? ORDER BY {DOC_ORDER}", r)} for r in db.repos()]


@app.get("/api/docs/search")
def docs_search(q: str = ""):
    return docgen.search(q)


@app.get("/api/docs/{name}/zip")
def docs_zip(name: str):
    repo = repo_named(name)
    rows = db.q(f"SELECT slug, title, summary, content FROM docs WHERE repo=? ORDER BY {DOC_ORDER}", repo)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        index = [f"# {repo} documentation", ""] + [
            f"- [{r['title']}]({r['slug']}.md){' - ' + r['summary'] if r['summary'] else ''}" for r in rows]
        z.writestr(f"{name}-docs/README.md", "\n".join(index) + "\n")
        for r in rows:
            z.writestr(f"{name}-docs/{r['slug']}.md", r["content"])
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}-docs.zip"'})


@app.get("/api/docs/{name}/history/{slug:path}")
def doc_history(name: str, slug: str):
    return db.q("SELECT id, head_sha, generated_at, model, LENGTH(content) size FROM doc_versions "
                "WHERE repo=? AND slug=? ORDER BY id DESC LIMIT 100", repo_named(name), slug)


@app.get("/api/docs/{name}/diff/{vid}")
def doc_diff(name: str, vid: int):
    """What version `vid` changed compared with the version before it."""
    v = db.q("SELECT * FROM doc_versions WHERE id=? AND repo=?", vid, repo_named(name))
    if not v:
        raise HTTPException(404)
    prev = db.q("SELECT content, head_sha FROM doc_versions WHERE repo=? AND slug=? AND id<? ORDER BY id DESC LIMIT 1",
                v[0]["repo"], v[0]["slug"], vid)
    before = prev[0]["content"] if prev else ""
    return {"diff": "".join(difflib.unified_diff(before.splitlines(True), v[0]["content"].splitlines(True),
                                                 prev[0]["head_sha"][:8] if prev else "(new)", v[0]["head_sha"][:8]))}


@app.get("/api/docs/{name}/{slug:path}")
def doc(name: str, slug: str, download: bool = False):
    r = db.q("SELECT * FROM docs WHERE repo=? AND slug=?", repo_named(name), slug)
    if not r:
        raise HTTPException(404, "no such document yet")
    if download:
        return Response(r[0]["content"], media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{name}-{slug.replace("/", "-")}.md"'})
    return r[0]


# ---------- MCP server (portal/mcp.py) ----------

@app.post("/mcp")
@app.post("/mcp/{token}")
async def mcp_endpoint(request: Request, token: str = ""):
    given = token or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    ws = db.cq("SELECT id FROM workspaces WHERE mcp_token=?", given) if given else []
    if not ws:
        return JSONResponse({"error": "missing or unknown MCP token: copy it from Settings → Connect a coding agent"}, 401)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(mcp.error(None, -32700, "Parse error"), 400)
    with workspace(ws[0]["id"]):
        if isinstance(body, list):  # batches (protocol 2025-03-26)
            out = [r for r in [await run_in_threadpool(mcp.handle, m) for m in body] if r]
            return JSONResponse(out) if out else Response(status_code=202)
        out = await run_in_threadpool(mcp.handle, body)
    return JSONResponse(out) if out else Response(status_code=202)


@app.api_route("/mcp", methods=["GET", "DELETE"])
@app.api_route("/mcp/{token}", methods=["GET", "DELETE"])
def mcp_no_stream(token: str = ""):
    return Response(status_code=405, headers={"Allow": "POST"})  # stateless: no server stream, no sessions


@app.post("/api/admin/{action}")
async def admin_action(action: str, request: Request):
    require_admin(request)
    body = await request.json() if await request.body() else {}
    repos = db.repos()
    if action == "sync":
        for repo in repos:
            sync.enqueue("sync", repo)
    elif action == "rebuild":
        sync.enqueue("rebuild")
    elif action == "review" and body.get("repo") in repos and str(body.get("pr", "")).isdigit():
        sync.enqueue("review_pr", body["repo"], int(body["pr"]))
    elif action == "docs" and body.get("repo") in repos:  # rewrite every document, or one (slug)
        docgen.enqueue(body["repo"], body.get("slug") or None, force=True)
    elif action == "remove_repo" and body.get("repo") in repos:
        # ponytail: its history and clone stay (re-importing picks them up); purge them when tenants ask
        db.cx("DELETE FROM repos WHERE workspace_id=? AND repo=?", WORKSPACE.get(), body["repo"])
        if body["repo"] in sync.views():
            db.x("DELETE FROM settings WHERE key='views'")
    else:
        raise HTTPException(400, "unknown action")
    return {"queued": action, **sync.status()}


# ---------- pages ----------

@app.get("/site/{path:path}")
def site(path: str):
    """The workspace's architecture views (build.py writes them)."""
    root = site_dir().resolve()
    f = (root / path).resolve()
    if not (f.is_relative_to(root) and f.is_file()):
        raise HTTPException(404)
    return FileResponse(f)


app.mount("/", StaticFiles(directory=WEB, html=True, check_dir=False), name="web")
