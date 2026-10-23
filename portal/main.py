"""Portal web app: GitHub login, workspaces and their members, repository import, webhook receiver, JSON API,
the API v1 for programs (API clients, integrations, login handoff), the generated views and the dynamic pages.

    uvicorn portal.main:app --host 0.0.0.0 --port 7000
"""
import difflib
import io
import json
import os
import re
import secrets
import time
import zipfile
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote

import httpx
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pymongo.errors import DuplicateKeyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import (PUBLIC_URL, ROOT, WORKSPACE, auth, chat, db, docgen, integrations, llm, mail, mcp,
               secret, site_dir, sync, workspace)
from .github import RateLimited, gh, pkce, repo_meta, verify_signature
from .review import SEVERITIES

WEB = ROOT / "web" / "dist"  # the Astro frontend: cd web && npm run build
# No session needed: /mcp has its own token, the webhook its signature, /invite/ sends people to sign in itself, the
# token endpoint takes client credentials, /auth/handoff a single-use code.
PUBLIC = ("/auth/", "/webhook/github", "/healthz", "/login", "/_astro/", "/favicon.svg", "/mcp", "/invite/",
          "/api/v1/auth/token")
NO_WORKSPACE = ("/api/me", "/api/workspaces", "/api/invites/", "/welcome", "/api/v1/",  # before creating or joining one
                "/connect/")
# owner/name; neither part "." or "..", which would put the clone outside the workspace's repos folder
REPO_NAME = re.compile(r"^(?!\.{1,2}/)[A-Za-z0-9_.-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+$")
# What people paste: https://github.com/o/n(.git)(/tree/...), git@github.com:o/n.git, or o/n. GitHub account names
# have no dots, so "gitlab.com/o/n" can't pass for an owner.
GITHUB_REPO = re.compile(r"(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)?"
                         r"([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/?#].*)?")
DEV_USER = os.environ.get("DEV_USER", "")  # local development only: honoured for loopback requests
ROLES = ("owner", "admin", "member")
INVITE_DAYS, INVITES_PER_DAY = 7, 50
OAUTH_STATE_SECONDS = 600
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SECRETS = ("llm_api_key",)
WEIGHT = {"critical": 8, "major": 3, "minor": 1, "nit": 0.5}
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
UNKNOWN = object()


@asynccontextmanager
async def lifespan(_app):
    db.init()
    if os.environ.get("DISABLE_WORKER") != "1":
        sync.start()
    yield


app = FastAPI(title="gg portal", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def current_user(request):
    """The signed-in person, or None: the session names a user id, and its session_version must still match (it goes
    up when they disconnect GitHub, which ends every session they have)."""
    user = getattr(request.state, "user", UNKNOWN)
    if user is not UNKNOWN:
        return user
    user = None
    # DEV_USER only works with GitHub sign-in not configured and for requests from this machine.
    if DEV_USER and not gh.oauth_enabled and request.client and request.client.host in ("127.0.0.1", "::1"):
        user = db.dev_user(DEV_USER)
    elif uid := request.session.get("uid"):
        try:
            u = db.users.find_one({"_id": ObjectId(uid)})
        except InvalidId:
            u = None
        if u and u.get("session_version", 0) == request.session.get("sv"):
            user = db.user_view(u)
    request.state.user = user
    return user


def cross_site(request):
    """A cookie-authenticated change requested by another site (CSRF). SameSite=Lax already keeps the cookie off
    cross-site POSTs; this refuses them outright when the browser says where they came from."""
    if request.method in SAFE_METHODS:
        return False
    site = request.headers.get("sec-fetch-site")
    if site:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    return bool(origin) and origin.rstrip("/") not in (PUBLIC_URL, f"{request.url.scheme}://{request.url.netloc}")


def client_ip(request):
    return request.client.host if request.client else "?"


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Signed in (or, under /api/v1/, an API client's bearer token), and inside a workspace: the one picked in the
    session while the user is still a member of it, else their first. Everything downstream reads WORKSPACE, so a
    request can only reach its own tenant."""
    path = request.url.path
    if path.startswith(PUBLIC):
        return await call_next(request)
    bearer = request.headers.get("authorization", "")
    if path.startswith("/api/v1/") and bearer[:7].lower() == "bearer ":
        p = await run_in_threadpool(auth.token_principal, bearer[7:].strip())
        if not p:
            return JSONResponse({"error": "invalid or expired access token"}, 401,
                                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        request.state.principal = p
        with workspace(p["workspace_id"]):
            return await call_next(request)
    if cross_site(request):
        return JSONResponse({"error": "cross-site request refused"}, status_code=403)
    user = await run_in_threadpool(current_user, request)
    if not user:
        if path.startswith("/api/"):
            return JSONResponse({"error": "login required"}, status_code=401)
        query = request.url.query  # kept: /connect/ggops?workspace=… must survive the sign-in
        return RedirectResponse(f"/auth/login?next={quote(path + ('?' + query if query else ''))}")
    pin = request.session.get("pin")  # a session an API client opened (login handoff) stays in its workspace
    rows = await run_in_threadpool(db.memberships, user["id"])
    request.state.workspaces = rows = [w for w in rows if not pin or w["id"] == pin]
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


@app.exception_handler(StarletteHTTPException)
async def errors(request: Request, exc: StarletteHTTPException):
    """API v1 answers {"error": ...}; everything older keeps FastAPI's {"detail": ...}."""
    if request.url.path.startswith("/api/v1/"):
        return JSONResponse({"error": exc.detail}, exc.status_code, headers=getattr(exc, "headers", None))
    return await http_exception_handler(request, exc)


def role(request):
    ws = getattr(request.state, "ws", None)
    return ws["role"] if ws else None


def require_admin(request):
    if role(request) not in ("owner", "admin"):
        raise HTTPException(403, "workspace owners and admins only")
    return current_user(request)


def limit(key, n, window):
    if wait := auth.over_limit(key, n, window):
        raise HTTPException(429, f"too many requests; try again in {wait} s", headers={"Retry-After": str(wait)})


@app.get("/healthz")
def healthz():
    """Liveness plus database reachability, for the load balancer; no data."""
    up = db.ping()
    return JSONResponse({"ok": up, "database": "up" if up else "down"}, status_code=200 if up else 503)


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
    """GitHub's authorization page, with a one-time state (login CSRF) and a PKCE challenge."""
    if not gh.oauth_enabled:
        return RedirectResponse("/auth/login")
    state, (verifier, challenge) = secrets.token_urlsafe(24), pkce()
    request.session["oauth"] = {"state": state, "verifier": verifier, "next": safe_next(next), "at": int(time.time())}
    return RedirectResponse(gh.oauth_url(state, f"{PUBLIC_URL}/auth/callback", challenge))


@app.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """The state is used up whatever happens; the session is only made once GitHub's answer is in and stored."""
    saved = request.session.pop("oauth", None) or {}
    nxt = saved.get("next") or "/"
    if error:
        return to_login(nxt, "You cancelled the GitHub sign-in." if error == "access_denied" else f"GitHub sign-in failed ({error[:60]}).")
    if not (code and saved.get("state") and secrets.compare_digest(state, saved["state"])) \
            or time.time() - saved.get("at", 0) > OAUTH_STATE_SECONDS:
        return to_login(error="The GitHub sign-in expired. Try again.")
    try:
        profile = gh.oauth_login(code, f"{PUBLIC_URL}/auth/callback", saved.get("verifier", ""))
        user = db.sign_in(profile)
        db.save_github_tokens(user, profile)
    except Exception as e:
        return to_login(nxt, f"GitHub sign-in failed: {e}")
    ws = request.session.get("ws")
    request.session.clear()  # nothing from before the sign-in carries over, except the workspace last open
    request.session.update(uid=str(user["id"]), sv=user["session_version"], **({"ws": ws} if ws else {}))
    db.audit("signin", user["login"], github_id=user["github_id"])
    return RedirectResponse(nxt)


@app.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return to_login()


@app.get("/auth/handoff")
def handoff(request: Request, code: str = ""):
    """Redeems a single-use login handoff (POST /api/v1/auth/login-handoff) into an ordinary session."""
    if auth.over_limit(f"handoff-ip:{client_ip(request)}", 30, 60):
        return to_login(error="Too many sign-in links at once; wait a minute.")
    h = auth.redeem_handoff(code)
    u = h and db.users.find_one({"_id": h["user_id"]})
    if not (u and db.members.find_one({"workspace_id": h["workspace_id"], "user_id": u["_id"]})):
        return to_login(error="That sign-in link was already used or has expired.")
    request.session.clear()
    request.session.update(uid=str(u["_id"]), sv=u.get("session_version", 0), ws=h["workspace_id"],
                           **({"pin": h["workspace_id"]} if h["created_by"].startswith("client:") else {}))
    db.audit("handoff.redeemed", u["login"], workspace=h["workspace_id"], created_by=h["created_by"])
    return RedirectResponse(h["next"], 303, headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})


# ---------- a person's own GG-Ops account ----------

def to_settings(error=""):
    """Settings shows `ggops_error` beside "Your GG-Ops account"."""
    return RedirectResponse("/settings/" + (f"?ggops_error={quote(error)}" if error else ""), 303)


@app.get("/connect/ggops")
def connect_ggops(request: Request, again: bool = False):
    """GG-Ops's "Open in GG-Docs" button. Connected already: straight in, unless `again` (a connection made read-only
    before, granting write now). Otherwise GG-Ops's consent screen, with a one-time state and a PKCE challenge (GG-Ops
    issues no client secrets, so PKCE is the proof); the new connection replaces and revokes the old one."""
    if not integrations.ggops_oauth():
        return to_settings("GG-Ops sign-in is not set up on this portal (GGOPS_URL and GGOPS_CLIENT_ID).")
    user = current_user(request)
    if not again and db.ggops_connections.find_one({"user_id": user["id"]}):
        return to_settings()
    state, (verifier, challenge) = secrets.token_urlsafe(24), pkce()
    request.session["ggops_oauth"] = {"state": state, "verifier": verifier, "at": int(time.time())}
    return RedirectResponse(integrations.authorize_url(state, challenge))


@app.get("/connect/ggops/callback")
def connect_ggops_callback(request: Request, code: str = "", state: str = "", iss: str = "", error: str = ""):
    """The state is used up whatever happens; the tokens are stored only once state, its age and the issuer check out
    and GG-Ops has swapped the code."""
    saved = request.session.pop("ggops_oauth", None) or {}
    if error:
        return to_settings("You didn't allow GG-Docs to read GG-Ops." if error == "access_denied"
                           else f"GG-Ops refused the connection ({error[:60]}).")
    if not (code and saved.get("state") and secrets.compare_digest(state, saved["state"])) \
            or time.time() - saved.get("at", 0) > OAUTH_STATE_SECONDS:
        return to_settings("The GG-Ops connection expired. Try again.")
    if iss.rstrip("/") != integrations.GGOPS_URL:  # RFC 9207: the answer comes from the GG-Ops we sent them to
        return to_settings("That answer did not come from this portal's GG-Ops.")
    user = current_user(request)
    try:
        integrations.connect(user["id"], code, saved["verifier"])
    except integrations.ServiceError as e:
        return to_settings(str(e))
    db.audit("ggops.connected", user["login"])
    return to_settings()


# ---------- workspaces ----------

@app.get("/api/me")
def me(request: Request):
    ws, user = request.state.ws, current_user(request)
    return {"login": user["login"], "id": str(user["id"]), "github_id": user["github_id"], "name": user["name"],
            "avatar_url": user["avatar_url"], "admin": role(request) in ("owner", "admin"), "role": role(request),
            "workspace": {"id": ws["id"], "name": ws["name"]} if ws else None, "workspaces": request.state.workspaces}


def clean_name(text):
    return " ".join(str(text).split())[:80]  # one line: it goes into email subjects


@app.post("/api/workspaces")
async def create_workspace(request: Request):
    name = clean_name((await request.json()).get("name", ""))
    if not name:
        raise HTTPException(400, "give the workspace a name")
    if request.session.get("pin"):
        raise HTTPException(403, "this session was opened for one workspace")
    user = current_user(request)
    request.session["ws"] = await run_in_threadpool(db.create_workspace, name, user)
    db.audit("workspace.created", user["login"], workspace=request.session["ws"])
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
    db.workspaces.update_one({"_id": WORKSPACE.get()}, {"$set": {"name": name}})
    return {"name": name}


# ---------- members and email invites ----------

def invite_cutoff():
    return (datetime.now(timezone.utc) - timedelta(days=INVITE_DAYS)).isoformat(timespec="seconds")


INVITE_FIELDS = {"_id": 0, "id": 1, "email": 1, "role": 1, "invited_by": 1, "created_at": 1}


@app.get("/api/workspace/members")
def members(request: Request):
    ws = WORKSPACE.get()
    rows = list(db.members.find({"workspace_id": ws}, sort=[("joined_at", 1), ("login_lower", 1)]))
    people = {u["_id"]: u for u in db.users.find({"_id": {"$in": [m["user_id"] for m in rows if m.get("user_id")]}},
                                                 {"name": 1, "avatar_url": 1})}
    out = {"members": [{"login": m["login"], "role": m["role"], "joined_at": m["joined_at"],
                        "name": people.get(m.get("user_id"), m).get("name"),
                        "avatar_url": people.get(m.get("user_id"), m).get("avatar_url"),
                        "pending": bool(m.get("pending"))} for m in rows],
           "invites": [], "email": bool(mail.HOST)}
    if role(request) in ("owner", "admin"):
        out["invites"] = list(db.invites.find({"workspace_id": ws, "accepted_at": None, "created_at": {"$gte": invite_cutoff()}},
                                              INVITE_FIELDS, sort=[("id", -1)]))
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
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    if db.invites.count_documents({"workspace_id": WORKSPACE.get(), "created_at": {"$gte": day_ago}}) >= INVITES_PER_DAY:
        raise HTTPException(429, f"at most {INVITES_PER_DAY} invites a day per workspace")  # this server's mail is not a spam relay
    token = secrets.token_urlsafe(32)
    db.invites.insert_one({"id": db.next_id("invites"), "workspace_id": WORKSPACE.get(), "email": email, "role": new_role,
                           "token_hash": secret.digest(token), "invited_by": inviter, "created_at": db.now(),
                           "accepted_by": None, "accepted_at": None})
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
    db.invites.delete_one({"id": iid, "workspace_id": WORKSPACE.get(), "accepted_at": None})
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
    inv = db.invites.find_one({"token_hash": secret.digest(token), "accepted_at": None, "created_at": {"$gte": invite_cutoff()}})
    w = inv and db.workspaces.find_one({"_id": inv["workspace_id"]}, {"name": 1})
    if not w:
        raise HTTPException(404, USED)
    return {"workspace": w["name"], "role": inv["role"], "invited_by": inv["invited_by"]}


@app.post("/api/invites/{token}/accept")
def accept_invite(token: str, request: Request):
    """Claiming the invite is atomic (it works once); joining follows. A failure in between leaves a used invite and
    no membership, never the other way round."""
    user = current_user(request)
    inv = db.invites.find_one_and_update(
        {"token_hash": secret.digest(token), "accepted_at": None, "created_at": {"$gte": invite_cutoff()}},
        {"$set": {"accepted_by": user["login"], "accepted_at": db.now()}})
    if not inv:
        raise HTTPException(404, USED)
    try:
        db.members.insert_one(db.member_doc(inv["workspace_id"], user, inv["role"]))
    except DuplicateKeyError:
        pass  # already a member: their role stays
    request.session["ws"] = inv["workspace_id"]
    return {"id": inv["workspace_id"]}


def member(login):
    m = db.members.find_one({"workspace_id": WORKSPACE.get(), "login_lower": login.lower()}, sort=[("pending", 1)])
    if not m:
        raise HTTPException(404, "no such member")
    return m


def owners():
    return db.members.count_documents({"workspace_id": WORKSPACE.get(), "role": "owner"})


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
    db.members.update_one({"_id": m["_id"]}, {"$set": {"role": new_role}})
    return {"login": m["login"], "role": new_role}


@app.delete("/api/workspace/members/{login}")
def remove_member(login: str, request: Request):
    m = member(login)
    leaving = m.get("user_id") == current_user(request)["id"]
    if not leaving:
        require_admin(request)
    if m["role"] == "owner" and not leaving and role(request) != "owner":
        raise HTTPException(403, "only owners can remove owners")
    if m["role"] == "owner" and owners() == 1:
        raise HTTPException(400, "a workspace needs at least one owner: make someone else owner first")
    db.members.delete_one({"_id": m["_id"]})
    if leaving:
        request.session.pop("ws", None)
    return {"ok": True}


# ---------- connecting repositories (paste a URL, or pick ones the GitHub App can read) ----------

def parse_repo(text):
    """owner/name from what people paste, or None."""
    m = GITHUB_REPO.fullmatch(str(text).strip())
    repo = m and f"{m[1]}/{m[2]}"
    return repo if repo and REPO_NAME.match(repo) else None


def user_token(user):
    """The user's GitHub token (stored encrypted), refreshed when it has expired (App user tokens last 8 hours)."""
    a = db.github_accounts.find_one({"user_id": user["id"]})
    token = secret.unseal(a["token"]) if a else ""
    if not token:
        raise HTTPException(401, "Sign in with GitHub again.")
    if not a.get("token_expires") or time.time() < a["token_expires"] - 60:
        return token
    try:
        t = gh.refresh(secret.unseal(a.get("refresh_token")) or "")
    except (httpx.HTTPError, PermissionError):
        raise HTTPException(401, "Your GitHub sign-in expired: sign in again.")
    db.save_github_tokens(user, t)
    return t["token"]


def check_access(user, repo):
    """(owner/name as GitHub spells it, its metadata) when this user may connect repo. The App may be installed on
    other tenants' private repositories, so what decides is the user's own GitHub access, plus the App being installed
    (it clones, reads CI and sends the webhooks). LookupError: not readable through the App, so the user should install it."""
    if gh.mode == "app":
        r = gh.request("GET", f"/repos/{repo}", check=False, token=user_token(user))
        if r.status_code == 401:
            raise HTTPException(401, "Your GitHub sign-in expired: sign in again.")
        if r.status_code != 200:
            raise LookupError(f"GitHub didn't let your account read {repo} through the App. Install the App on it "
                              "(or ask an owner of the repository to), then import again.")
        data = r.json()
        if not gh.installation(data["full_name"]):
            raise LookupError(f"The GitHub App isn't installed on {data['full_name']} yet. Install it, then import again.")
        return data["full_name"], repo_meta(data)
    if gh.mode == "token":  # one operator: the server's own token decides
        r = gh.request("GET", f"/repos/{repo}", check=False)
        if r.status_code != 200:
            raise HTTPException(404, f"GITHUB_TOKEN can't read {repo}.")
        return r.json()["full_name"], repo_meta(r.json())
    return repo, {}


def connect(user, text):
    """Verifies and connects one repository to the current workspace, then queues its first sync."""
    repo = parse_repo(text)
    if not repo:
        raise HTTPException(400, "Paste a GitHub repository: https://github.com/owner/name or owner/name.")
    repo, meta = check_access(user, repo)
    name = repo.split("/")[1].lower()
    if name in {r.split("/")[1].lower() for r in db.repos()}:  # one clone folder and docs URL per name
        raise HTTPException(400, f"this workspace already has a repository named {repo.split('/')[1]}")
    db.add_repo(WORKSPACE.get(), repo, user["login"], {**meta, "status": "active", "status_detail": None,
                                                       "checked_at": db.now()})
    sync.enqueue("sync", repo)  # clone, history, PRs, CI, then its documentation
    db.audit("repository.connected", user["login"], repo=repo)
    return repo


def github_call(fn, *args):
    try:
        return fn(*args)
    except RateLimited as e:
        raise HTTPException(429, str(e), headers={"Retry-After": str(e.retry_after)})
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(401, "Your GitHub sign-in expired: sign in again.")
        raise


@app.get("/api/import/repos")
def importable(request: Request):
    """Repositories the user can import: every one their GitHub account reaches through the App's installations."""
    user = require_admin(request)
    if gh.mode != "app":
        return {"repos": [], "install_url": None}
    found = github_call(gh.user_repos, user_token(user), user["login"])
    have = {r.lower() for r in db.repos()}
    found.sort(key=lambda r: r["pushed_at"] or "", reverse=True)
    return {"repos": [{**r, "imported": r["repo"].lower() in have} for r in found], "install_url": gh.install_url()}


@app.post("/api/import")
async def import_repo(request: Request):
    """Vercel-style: a GitHub URL in, the repository cloned into this workspace, then kept in sync by webhooks."""
    user = require_admin(request)
    text = (await request.json()).get("url", "")
    try:
        repo = await run_in_threadpool(connect, user, text)
    except LookupError as e:
        return JSONResponse({"error": str(e), "install_url": gh.install_url()}, status_code=400)
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


def is_open(field="$fixed_sha"):
    return {"$cond": [{"$eq": [{"$ifNull": [field, None]}, None]}, 1, 0]}


def count_if(field, value):
    return {"$sum": {"$cond": [{"$eq": [field, value]}, 1, 0]}}


@app.get("/api/summary")
def summary():
    ci = {}
    for side, repo in zip(("backend", "frontend"), sync.views()):
        head = db.setting(f"head:{repo}")
        rows = db.checks.find({"repo": repo, "sha": head}, fields=["conclusion"]) if head else []
        ci[side] = {"repo": repo, "head": head, "total": len(rows),
                    "passed": sum(r["conclusion"] in ("success", "skipped", "neutral") for r in rows),
                    "failed": sum(r["conclusion"] in ("failure", "timed_out") for r in rows)}
    last_sync = db.events.one({"kind": "sync"}, sort=[("id", -1)], fields=["ok", "detail"])
    return {"ci": ci, "open_breakages": db.breakages.count({"fixed_sha": None}),
            "worker": sync.status(), "github": gh.mode, "llm": llm.configured(), "llm_model": llm.config()["llm_model"],
            "repos": db.repos(), "views": bool(sync.views()), "docs_writing": docgen.writing(),
            "sync_error": last_sync["detail"] if last_sync and not last_sync["ok"] else None}


def review_counts(ids):
    return {a["_id"]: a for a in db.findings.aggregate([
        {"$match": {"review_id": {"$in": ids}}},
        {"$group": {"_id": "$review_id", "findings": {"$sum": 1}, "critical": count_if("$severity", "critical"),
                    "major": count_if("$severity", "major"), "slop": count_if("$category", "slop")}}])}


@app.get("/api/activity")
def activity(repo: str = "", branch: str = "", person: str = "", days: int = 30, limit: int = 100):
    limit = max(1, min(limit, 500))
    f = {"pushed_at": {"$gte": since(days)}}
    for col, val in (("repo", repo), ("branch", branch), ("pusher", person)):
        if val:
            f[col] = val
    pushes = db.pushes.find(f, sort=[("pushed_at", -1)], limit=limit)
    for p in pushes:
        shas = p.pop("commit_shas", None) or []
        p["commit_list"] = db.commits.find({"repo": p["repo"], "sha": {"$in": shas}}, sort=[("committed_at", -1)], limit=50,
                                           fields=["sha", "person", "message", "additions", "deletions"])
        p["checks"] = db.checks.find({"repo": p["repo"], "sha": p["after"]}, fields=["name", "conclusion", "url"])
        p["broke"] = [r["check_name"] for r in db.breakages.find({"repo": p["repo"], "broken_sha": {"$in": shas}},
                                                                 fields=["check_name"])]
        revs = db.reviews.find({"repo": p["repo"], "head_sha": p["after"]}, fields=["id", "pr"])
        counts = review_counts([r["id"] for r in revs])
        p["reviews"] = [{**r, "findings": counts[r["id"]]["findings"] if r["id"] in counts else 0} for r in revs]
    c = {"committed_at": {"$gte": since(days)}}
    for col, val in (("repo", repo), ("person", person)):
        if val:
            c[col] = val
    commits = db.commits.find(c, sort=[("committed_at", -1)], limit=limit, fields=[
        "repo", "sha", "person", "author_name", "committed_at", "message", "additions", "deletions", "is_merge", "on_default"])
    return {"pushes": pushes, "commits": commits}


@app.get("/api/people")
def people(days: int = 90):
    t = since(days)
    stats = defaultdict(lambda: defaultdict(float))
    for r in db.commits.aggregate([{"$match": {"committed_at": {"$gte": t}, "is_merge": 0}}, {"$group": {
            "_id": "$person", "commits": {"$sum": 1}, "adds": {"$sum": "$additions"}, "dels": {"$sum": "$deletions"}}}]):
        stats[r["_id"]].update(commits=r["commits"], adds=r["adds"] or 0, dels=r["dels"] or 0)
    for r in db.pushes.aggregate([{"$match": {"pushed_at": {"$gte": t}}}, {"$group": {"_id": "$pusher", "n": {"$sum": 1}}}]):
        stats[r["_id"]]["pushes"] = r["n"]
    for r in db.breakages.aggregate([{"$match": {"broken_at": {"$gte": t}}}, {"$group": {
            "_id": "$person", "n": {"$sum": 1}, "open": {"$sum": is_open()}}}]):
        stats[r["_id"]].update(breakages=r["n"], open_breakages=r["open"])
    recent = [r["id"] for r in db.reviews.find({"created_at": {"$gte": t}}, fields=["id"])]
    for r in db.findings.aggregate([{"$match": {"review_id": {"$in": recent}}}, {"$group": {
            "_id": {"person": "$person", "severity": "$severity", "category": "$category"}, "n": {"$sum": 1}}}]):
        k, s = r["_id"], stats[r["_id"].get("person")]
        s[k.get("severity")] += r["n"]
        s["findings"] += r["n"]
        if k.get("category") == "slop":
            s["slop"] += r["n"]
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
    found = db.findings.find({"person": person}, sort=[("id", -1)], limit=200)
    revs = {r["id"]: r for r in db.reviews.find({"id": {"$in": [f["review_id"] for f in found]}},
                                                fields=["id", "repo", "pr", "branch"])}
    return {
        "commits": db.commits.find({"person": person, "committed_at": {"$gte": since(days)}}, sort=[("committed_at", -1)],
                                   limit=200, fields=["repo", "sha", "committed_at", "message", "additions", "deletions",
                                                      "on_default"]),
        "breakages": db.breakages.find({"person": person}, sort=[("broken_at", -1)], limit=100),
        "findings": [{**f, **{k: revs[f["review_id"]][k] for k in ("repo", "pr", "branch")}}
                     for f in found if f["review_id"] in revs],
    }


@app.get("/api/breakages")
def breakages(days: int = 90):
    return db.breakages.find({"$or": [{"broken_at": {"$gte": since(days)}}, {"fixed_sha": None}]}, sort=[("broken_at", -1)])


@app.get("/api/reviews")
def reviews(limit: int = 100):
    rows = db.reviews.find(sort=[("id", -1)], limit=max(1, min(limit, 500)))
    counts = review_counts([r["id"] for r in rows])
    for r in rows:  # as SQL's SUM over no rows: null counts for a review without findings
        a = counts.get(r["id"])
        r.update(findings=a["findings"] if a else 0, critical=a and a["critical"], major=a and a["major"], slop=a and a["slop"])
    return rows


SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


@app.get("/api/reviews/{rid}")
def review_detail(rid: int):
    r = db.reviews.one({"id": rid})
    if not r:
        raise HTTPException(404)
    found = db.findings.find({"review_id": rid})
    found.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), f["path"] or "", f["line"] or 0))
    return {**r, "findings": found}


@app.get("/api/events")
def events():
    return db.events.find(sort=[("id", -1)], limit=100)


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
    current = {"llm_api_key": cfg["llm_api_key"]}  # saved, or from .env
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
    if cfg["llm_base_url"] and not integrations.outside(cfg["llm_base_url"]):
        raise HTTPException(400, "base URL must be a public address (or this server's local model)")
    if cfg["llm_effort"] not in ("", "low", "medium", "high", "xhigh", "max"):
        raise HTTPException(400, "effort must be blank, low, medium, high, xhigh or max")
    return cfg


@app.put("/api/settings")
async def put_settings(request: Request):
    require_admin(request)
    body = await request.json()
    cfg = merged_settings(body)
    # Store only what differs from .env, so later .env changes (install.sh LOCAL_LLM) still take effect.
    for key, default in {**llm.DEFAULTS, "llm_api_key": os.environ.get("LLM_API_KEY", "")}.items():
        if cfg[key] == default:
            db.del_setting(key)
        else:
            db.set_setting(key, cfg[key])
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
    """The workspace's legacy MCP token (stored encrypted, shown to admins) unless it was turned off."""
    require_admin(request)
    w = db.workspaces.find_one({"_id": WORKSPACE.get()})
    legacy = w.get("mcp_legacy", True)
    token = secret.unseal(w.get("mcp_token")) if legacy else ""
    return {"url": f"{PUBLIC_URL}/mcp", "token_url": f"{PUBLIC_URL}/mcp/{token}" if token else None,
            "token": token or None, "legacy": legacy}


@app.post("/api/settings/mcp/rotate")
def rotate_mcp(request: Request):
    """A new legacy MCP token; the old one stops working at once."""
    user = require_admin(request)
    db.workspaces.update_one({"_id": WORKSPACE.get()}, {"$set": db.mcp_fields(secrets.token_urlsafe(24))})
    db.audit("mcp_token.rotated", user["login"])
    return mcp_settings(request)


def my_mcp_view(token, user_id):
    c = db.ggops_connections.find_one({"user_id": user_id})
    return {"url": f"{PUBLIC_URL}/mcp", "token_url": f"{PUBLIC_URL}/mcp/{token}", "token": token,
            "ggops": integrations.ggops_oauth(), "ggops_connected": bool(c), "ggops_write": bool(c) and integrations.can_write(c)}


def my_membership(request):
    user = current_user(request)
    m = user and db.members.find_one({"workspace_id": WORKSPACE.get(), "user_id": user["id"]})
    if not m:
        raise HTTPException(403, "members of this workspace only")
    return user, m


@app.get("/api/settings/mcp/me")
def my_mcp(request: Request):
    """This person's own MCP token for this workspace, made on first use. Agents connected with it act as them, which
    GG-Ops's tools need; any member can have one, and leaving the workspace ends it."""
    user, m = my_membership(request)
    token = secret.unseal(m.get("mcp_token"))
    if not token:
        token = secrets.token_urlsafe(24)
        db.members.update_one({"_id": m["_id"]}, {"$set": db.mcp_fields(token, personal=True)})
    return my_mcp_view(token, user["id"])


@app.post("/api/settings/mcp/me/rotate")
def rotate_my_mcp(request: Request):
    """A new personal MCP token; agents using the old one stop working at once."""
    user, m = my_membership(request)
    token = secrets.token_urlsafe(24)
    db.members.update_one({"_id": m["_id"]}, {"$set": db.mcp_fields(token, personal=True)})
    db.audit("mcp_token.personal_rotated", user["login"])
    return my_mcp_view(token, user["id"])


@app.put("/api/settings/mcp")
async def legacy_mcp(request: Request):
    """Turns the legacy MCP token off (agents then connect with an API client's mcp:read token) or back on."""
    user = require_admin(request)
    on = bool((await request.json()).get("legacy"))
    db.workspaces.update_one({"_id": WORKSPACE.get()}, {"$set": {"mcp_legacy": on}})
    db.audit("mcp_token.legacy", user["login"], enabled=on)
    return mcp_settings(request)


# ---------- generated docs (portal/docgen.py) ----------

def repo_named(name):
    repo = next((r for r in db.repos() if r.split("/")[1] == name), None)
    if not repo:
        raise HTTPException(404, "unknown repository")
    return repo


DOC_RANK = {k: i for i, k in enumerate(("overview", "architecture", "data-flow", "system-design", "ml-system", "api"))}
DOC_FIELDS = ["slug", "kind", "title", "summary", "status", "error", "model", "head_sha", "generated_at"]


def ordered(docs):
    return sorted(docs, key=lambda d: (DOC_RANK.get(d["kind"], 6), d["title"] or ""))


@app.get("/api/docs")
def docs_index():
    return [{"repo": r, "name": r.split("/")[1], "status": docgen.status(r),
             "docs": ordered(db.docs.find({"repo": r}, fields=DOC_FIELDS))} for r in db.repos()]


@app.get("/api/docs/search")
def docs_search(q: str = ""):
    return docgen.search(q)


@app.get("/api/docs/{name}/zip")
def docs_zip(name: str):
    repo = repo_named(name)
    rows = ordered(db.docs.find({"repo": repo}, fields=["slug", "kind", "title", "summary", "content"]))
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
    return db.doc_versions.aggregate([
        {"$match": {"repo": repo_named(name), "slug": slug}}, {"$sort": {"id": -1}}, {"$limit": 100},
        {"$project": {"_id": 0, "id": 1, "head_sha": 1, "generated_at": 1, "model": 1, "size": {"$strLenCP": "$content"}}}])


@app.get("/api/docs/{name}/diff/{vid}")
def doc_diff(name: str, vid: int):
    """What version `vid` changed compared with the version before it."""
    v = db.doc_versions.one({"id": vid, "repo": repo_named(name)})
    if not v:
        raise HTTPException(404)
    prev = db.doc_versions.one({"repo": v["repo"], "slug": v["slug"], "id": {"$lt": vid}}, sort=[("id", -1)],
                               fields=["content", "head_sha"])
    before = prev["content"] if prev else ""
    return {"diff": "".join(difflib.unified_diff(before.splitlines(True), v["content"].splitlines(True),
                                                 prev["head_sha"][:8] if prev else "(new)", v["head_sha"][:8]))}


@app.get("/api/docs/{name}/{slug:path}")
def doc(name: str, slug: str, download: bool = False):
    r = db.docs.one({"repo": repo_named(name), "slug": slug})
    if not r:
        raise HTTPException(404, "no such document yet")
    if download:
        return Response(r["content"], media_type="text/markdown; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{name}-{slug.replace("/", "-")}.md"'})
    return r


# ---------- MCP server (portal/mcp.py) ----------

def mcp_principal(token, header):
    """(workspace, person) for an MCP request, or None: a person's own MCP token (header or URL) is them in one
    workspace; the legacy workspace token (header or URL, unless turned off) and an API client's access token with
    mcp:read (header only: bearer tokens never go in URLs) are the workspace, with no person."""
    given = token or header
    if not given:
        return None
    digest = secret.digest(given)
    m = db.members.find_one({"mcp_token_hash": digest, "pending": False}, {"workspace_id": 1, "user_id": 1})
    if m:
        return m["workspace_id"], m["user_id"]
    w = db.workspaces.find_one({"mcp_token_hash": digest, "mcp_legacy": {"$ne": False}}, {"_id": 1})
    if w:
        return w["_id"], None
    p = None if token else auth.token_principal(given)
    return (p["workspace_id"], None) if p and "mcp:read" in p["scopes"] else None


@app.post("/mcp")
@app.post("/mcp/{token}")
async def mcp_endpoint(request: Request, token: str = ""):
    header = request.headers.get("authorization", "")
    who = await run_in_threadpool(mcp_principal, token, header[7:].strip() if header[:7].lower() == "bearer " else "")
    if who is None:
        return JSONResponse({"error": "missing or unknown MCP token: copy it from Settings → Connect a coding agent"}, 401)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(mcp.error(None, -32700, "Parse error"), 400)
    ws, person = who
    with workspace(ws):
        if isinstance(body, list):  # batches (protocol 2025-03-26)
            out = [r for r in [await run_in_threadpool(mcp.handle, m, person) for m in body] if r]
            return JSONResponse(out) if out else Response(status_code=202)
        out = await run_in_threadpool(mcp.handle, body, person)
    return JSONResponse(out) if out else Response(status_code=202)


@app.api_route("/mcp", methods=["GET", "DELETE"])
@app.api_route("/mcp/{token}", methods=["GET", "DELETE"])
def mcp_no_stream(token: str = ""):
    return Response(status_code=405, headers={"Allow": "POST"})  # stateless: no server stream, no sessions


@app.post("/api/admin/{action}")
async def admin_action(action: str, request: Request):
    user = require_admin(request)
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
        db.remove_repo(body["repo"])
        db.audit("repository.disconnected", user["login"], repo=body["repo"])
    else:
        raise HTTPException(400, "unknown action")
    return {"queued": action, **sync.status()}


# ---------- API v1: people and programs (README 1.16) ----------

def principal(request):
    """Who is calling: an API client (bearer token) or a signed-in person, with the scopes they hold in the current
    workspace."""
    p = getattr(request.state, "principal", None)
    if p:
        return p
    ws = getattr(request.state, "ws", None)
    return {"type": "user", **current_user(request), "workspace_id": ws and ws["id"], "role": ws and ws["role"],
            "scopes": auth.user_scopes(ws and ws["role"])}


def need(request, scope, ws=None):
    """The principal when it holds `scope` in workspace `ws` (default: the current one). A workspace it has no part in
    is a 404, so ids can't be probed."""
    p = principal(request)
    if ws is not None:
        if p["type"] == "client":
            if ws != p["workspace_id"]:
                raise HTTPException(404, "no such workspace")
        else:
            m = next((w for w in request.state.workspaces if w["id"] == ws), None)
            if not m:
                raise HTTPException(404, "no such workspace")
            p = {**p, "workspace_id": ws, "role": m["role"], "scopes": auth.user_scopes(m["role"])}
    elif p["workspace_id"] is None:
        raise HTTPException(409, "create or join a workspace first")
    if scope not in p["scopes"]:
        raise HTTPException(403, f"this needs the {scope} scope" if p["type"] == "client" else
                            "workspace owners and admins only")
    return p


def need_person(request):
    p = principal(request)
    if p["type"] != "user":
        raise HTTPException(403, "this needs a person signed in with GitHub, not an API client")
    return p


def actor(p):
    return p["login"] if p["type"] == "user" else f"client:{p['client_id']}"


async def body_of(request):
    """JSON, or a form (OAuth clients send token requests form-encoded)."""
    if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        return dict(parse_qsl((await request.body()).decode()))
    try:
        body = await request.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@app.post("/api/v1/auth/token")
async def issue_token(request: Request):
    """OAuth 2.0 client credentials: client_id + client_secret (+ scope, + workflow_id) -> a 1-hour bearer token."""
    form = await body_of(request)
    if form.get("grant_type") != "client_credentials":
        return JSONResponse({"error": "unsupported_grant_type", "error_description": "Only client_credentials."}, 400)
    cid = str(form.get("client_id") or "")[:64]
    wait = auth.over_limit(f"token-ip:{client_ip(request)}", 60, 60) or auth.over_limit(f"token-client:{cid}", 20, 60)
    if wait:
        return JSONResponse({"error": "slow_down", "error_description": "Too many token requests."}, 429,
                            headers={"Retry-After": str(wait)})
    try:
        out = await run_in_threadpool(auth.issue_token, cid, form.get("client_secret"), form.get("scope"),
                                      form.get("workflow_id"))
    except auth.OAuthError as e:
        db.audit("token.denied", f"client:{cid}", reason=e.error)
        return JSONResponse({"error": e.error, "error_description": e.description}, e.status,
                            headers={"Cache-Control": "no-store"})
    db.audit("token.issued", f"client:{cid}", scope=out["scope"], workflow_id=out["workflow_id"])
    return JSONResponse(out, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@app.get("/api/v1/auth/me")
def whoami(request: Request):
    p = principal(request)
    w = p["workspace_id"] and db.workspaces.find_one({"_id": p["workspace_id"]}, {"name": 1})
    ws = {"id": w["_id"], "name": w["name"], "role": p.get("role"), "permissions": list(p["scopes"])} if w else None
    if p["type"] == "client":
        return {"client": {"id": p["client_id"], "name": p["name"]}, "workspace": ws, "workflow_id": p["workflow_id"],
                "expires_at": p["expires_at"]}
    return {"user": {"id": str(p["id"]), "login": p["login"], "name": p["name"],
                     "github": {"id": p["github_id"], "login": p["login"]}}, "workspace": ws}


@app.post("/api/v1/auth/logout")
def api_logout(request: Request):
    """An API client's token is revoked; a person's session ends."""
    p = principal(request)
    if p["type"] == "client":
        auth.revoke_token(p["token_hash"])
    else:
        request.session.clear()
    return {"ok": True}


@app.post("/api/v1/auth/login-handoff")
async def login_handoff(request: Request):
    """A single-use link that opens the portal signed in, for 60 seconds: for yourself (session), or, for an API client
    with portal:handoff, for a member of its workspace named by GitHub id (that session stays in the workspace)."""
    p = need(request, "portal:handoff")
    body = await body_of(request)
    nxt = safe_next(str(body.get("next") or "/"))
    if nxt.startswith(("/auth/", "/api/", "/mcp", "/webhook", "/invite/")):
        nxt = "/"  # pages only
    limit(f"handoff:{actor(p)}", 20, 60)
    if p["type"] == "client":
        try:
            u = db.users.find_one({"github_id": int(body.get("github_id"))})
        except (TypeError, ValueError):
            raise HTTPException(400, "github_id (a number) names the member to sign in")
        if not (u and db.members.find_one({"workspace_id": p["workspace_id"], "user_id": u["_id"]})):
            raise HTTPException(404, "no member of this workspace has that GitHub id")
        uid = u["_id"]
    else:
        uid = p["id"]
    code = auth.create_handoff(uid, p["workspace_id"], nxt, actor(p))
    db.audit("handoff.created", actor(p), user_id=str(uid), next=nxt)
    return {"url": f"{PUBLIC_URL}/auth/handoff?code={code}", "expires_in": auth.HANDOFF_TTL}


@app.get("/api/v1/github/repositories")
def github_repositories(request: Request, q: str = "", access: str = "", page: int = 1, per_page: int = 50):
    """Repositories the signed-in person's GitHub account can reach through the App, every page of them, with whether
    each is owned, an organization's, or shared with them as a collaborator, and whether it is connected here."""
    p = need_person(request)
    limit(f"gh-list:{p['id']}", 30, 60)
    if gh.mode != "app":
        return {"repositories": [], "total": 0, "page": 1, "per_page": per_page, "install_url": None}
    found = github_call(gh.user_repos, user_token(p), p["login"])
    have = {r.lower() for r in db.repos()} if WORKSPACE.get(None) else set()
    q = q.strip().lower()
    found = [r for r in found if (not q or q in r["repo"].lower() or q in (r["description"] or "").lower())
             and (not access or r["access"] == access)]
    found.sort(key=lambda r: r["pushed_at"] or "", reverse=True)
    per_page, page = max(1, min(per_page, 100)), max(1, page)
    return {"repositories": [{**r, "connected": r["repo"].lower() in have}
                             for r in found[(page - 1) * per_page:page * per_page]],
            "total": len(found), "page": page, "per_page": per_page, "install_url": gh.install_url()}


@app.post("/api/v1/github/disconnect")
def github_disconnect(request: Request):
    """Revokes the App's authorization on GitHub, forgets the tokens, and ends every session of this person."""
    p = need_person(request)
    a = db.github_accounts.find_one({"user_id": p["id"]})
    revoked = False
    if a and gh.oauth_enabled:
        try:
            revoked = gh.revoke_grant(secret.unseal(a["token"]))
        except httpx.HTTPError:
            pass
    db.github_accounts.delete_one({"user_id": p["id"]})
    db.users.update_one({"_id": p["id"]}, {"$inc": {"session_version": 1}})
    request.session.clear()
    db.audit("github.disconnected", p["login"], revoked_on_github=revoked)
    return {"ok": True, "revoked_on_github": revoked}


def repo_view(r):
    return {"id": str(r["_id"]), "repo": r["repo"], **{k: r.get(k) for k in (
        "github_id", "owner", "description", "visibility", "default_branch", "html_url", "status_detail", "checked_at",
        "added_by", "added_at")}, "status": r.get("status", "unknown")}


@app.get("/api/v1/workspaces/{ws}/repositories")
def workspace_repositories(ws: int, request: Request):
    need(request, "repository:read", ws)
    return {"repositories": [repo_view(r) for r in db.repo_links.find({"workspace_id": ws}, sort=[("added_at", 1), ("_id", 1)])]}


@app.post("/api/v1/workspaces/{ws}/repositories")
async def connect_repositories(ws: int, request: Request):
    """{"repositories": ["owner/name" or a GitHub URL, ...]}: each is checked against the person's own GitHub access
    and the App's installation, then connected; one result per repository."""
    p = need(request, "repository:write", ws)
    if p["type"] != "user":
        raise HTTPException(403, "connecting a repository is checked against a person's own GitHub access, so a person "
                                 "signed in with GitHub does it")
    body = await body_of(request)
    items = body.get("repositories") or ([body["repository"]] if body.get("repository") else [])
    if not isinstance(items, list) or not 0 < len(items) <= 50 or not all(isinstance(i, str) for i in items):
        raise HTTPException(400, "repositories: a list of 1 to 50 owner/name strings")

    def run():
        results = []
        with workspace(ws):
            for text in items:
                try:
                    results.append({"repository": text, "ok": True, "repo": connect(p, text)})
                except LookupError as e:
                    results.append({"repository": text, "ok": False, "error": str(e), "install_url": gh.install_url()})
                except HTTPException as e:
                    if e.status_code == 401:
                        raise
                    results.append({"repository": text, "ok": False, "error": e.detail})
            return {"results": results, **sync.status()}
    return await run_in_threadpool(run)


@app.post("/api/v1/workspaces/{ws}/repositories/refresh")
def refresh_repositories(ws: int, request: Request):
    """Re-reads every connected repository with the person's GitHub access: metadata, and whether they and the App
    can still reach it. Nothing is deleted; a repository that lost access shows why, and how to restore it."""
    p = need(request, "repository:write", ws)
    if p["type"] != "user":
        raise HTTPException(403, "refreshing checks a person's own GitHub access")
    with workspace(ws):
        for repo in db.repos():
            try:
                full, meta = github_call(check_access, p, repo)
                db.update_repo(ws, repo, {**meta, "status": "active", "status_detail": None, "checked_at": db.now()})
            except (LookupError, HTTPException) as e:
                if isinstance(e, HTTPException) and e.status_code in (401, 429):
                    raise
                db.update_repo(ws, repo, {"status": "revoked", "checked_at": db.now(),
                                          "status_detail": str(e) if isinstance(e, LookupError) else e.detail})
    return workspace_repositories(ws, request)


@app.delete("/api/v1/workspaces/{ws}/repositories/{rid}")
def disconnect_repository(ws: int, rid: str, request: Request):
    """Stops syncing it; its history, reviews and documents stay."""
    p = need(request, "repository:write", ws)
    try:
        r = db.repo_links.find_one({"_id": ObjectId(rid), "workspace_id": ws})
    except InvalidId:
        r = None
    if not r:
        raise HTTPException(404, "no such repository in this workspace")
    with workspace(ws):
        db.remove_repo(r["repo"])
        db.audit("repository.disconnected", actor(p), repo=r["repo"])
    return {"ok": True, "repo": r["repo"]}


# not 401/409 for a person's own GG-Ops account: the web app reads those as "sign in" and "no workspace"
SERVICE_ERRORS = {"invalid": 400, "not_configured": 404, "not_found": 404, "not_connected": 403, "reconnect": 403}


def service_call(fn, *args):
    try:
        return fn(*args)
    except integrations.ServiceError as e:
        raise HTTPException(SERVICE_ERRORS.get(e.status, 502), str(e))


@app.get("/api/v1/integrations")
def list_integrations(request: Request):
    p = need(request, "integration:read")
    saved = {i["service"]: i for i in db.integrations.find()}
    flows = db.workflows.find(sort=[("added_at", 1)])
    if p["type"] == "client" and p.get("workflow_id"):  # a client bound to one workflow sees that one
        flows = [w for w in flows if w["workflow_id"] == p["workflow_id"]]
    return {"integrations": [integrations.view(saved[s]) if s in saved else {"service": s, "status": "not_configured"}
                             for s in integrations.SERVICES],
            "workflows": [integrations.workflow_view(w) for w in flows]}


@app.put("/api/v1/integrations/{service}")
async def save_integration(service: str, request: Request):
    """{"base_url", "api_key"}: the key is stored encrypted and never returned; blank keeps the saved one."""
    p = need(request, "integration:write")
    body = await body_of(request)
    return service_call(integrations.save, service, body.get("base_url"), body.get("api_key"), actor(p))


@app.delete("/api/v1/integrations/{service}")
def remove_integration(service: str, request: Request):
    p = need(request, "integration:write")
    integrations.remove(service, actor(p))
    return {"ok": True}


@app.post("/api/v1/integrations/connections/verify")
async def verify_connection(request: Request):
    """{"service": "gg-ops" | "gg-pulse"} -> {status, service, verifiedAt, detail}, checked against the real service."""
    p = need(request, "integration:read")
    service = str((await body_of(request)).get("service") or "")
    if service not in integrations.SERVICES:
        raise HTTPException(400, f"service: one of {', '.join(integrations.SERVICES)}")
    limit(f"verify:{p['workspace_id']}", 20, 60)
    return await run_in_threadpool(service_call, integrations.verify, service, actor(p))


@app.post("/api/v1/integrations/workflows")
async def add_workflow(request: Request):
    """{"workflow_id": "<GG-Ops project id>", "name"?}: linked only once GG-Ops shows it to the workspace's key."""
    p = need(request, "integration:write")
    body = await body_of(request)
    return await run_in_threadpool(service_call, integrations.add_workflow, body.get("workflow_id"), body.get("name"), actor(p))


@app.delete("/api/v1/integrations/workflows/{workflow_id}")
def remove_workflow(workflow_id: str, request: Request):
    p = need(request, "integration:write")
    if not integrations.remove_workflow(workflow_id, actor(p)):
        raise HTTPException(404, "no such workflow in this workspace")
    return {"ok": True}


@app.get("/api/v1/integrations/workflows/{workflow_id}")
def workflow(workflow_id: str, request: Request):
    """A linked workflow's GG-Ops project status and task counts, read now: as the person (their own GG-Ops account)
    when GG-Ops sign-in is set up, else and for API clients with the workspace's GG-Ops key. Knowing a workflow id
    grants nothing: it must be linked to the caller's workspace (and to the client, if it is bound)."""
    p = need(request, "workflow:read")
    w = db.workflows.one({"workflow_id": workflow_id.lower()})
    if not w or (p["type"] == "client" and p.get("workflow_id") and p["workflow_id"] != w["workflow_id"]):
        raise HTTPException(404, "no such workflow in this workspace")
    if p["type"] == "user" and integrations.ggops_oauth():  # a person reads as themselves; programs keep the key
        board = service_call(integrations.user_board, p["id"], w["workflow_id"])
        return {"workflow": integrations.workflow_view(w), "project": integrations.summarize(board)}
    board = service_call(integrations.check_workflow, w["workflow_id"])
    return {"workflow": integrations.workflow_view(db.workflows.one({"workflow_id": w["workflow_id"]})),
            "project": integrations.summarize(board)}


@app.get("/api/v1/integrations/gg-ops/me")
def ggops_account(request: Request):
    """The caller's own GG-Ops connection and the GG-Ops workspaces it sees, read now through GG-Ops."""
    p = need_person(request)
    if not integrations.ggops_oauth():
        return {"enabled": False, "connected": False}
    try:
        return {"enabled": True, **integrations.account(p["id"])}
    except integrations.ServiceError as e:
        return {"enabled": True, "connected": e.status not in ("not_connected", "reconnect"), "error": str(e)}


@app.delete("/api/v1/integrations/gg-ops/me")
def ggops_disconnect(request: Request):
    p = need_person(request)
    revoked = integrations.disconnect(p["id"])
    db.audit("ggops.disconnected", p["login"], revoked_in_ggops=revoked)
    return {"ok": True, "revoked_in_ggops": revoked}


@app.get("/api/v1/clients")
def list_clients(request: Request):
    p = need(request, "client:write")
    return {"clients": [auth.client_view(c) for c in db.api_clients.find({"workspace_id": p["workspace_id"]}, sort=[("created_at", -1)])],
            "scopes": auth.CLIENT_SCOPES}


@app.post("/api/v1/clients")
async def create_client(request: Request):
    """{"name", "scopes": [...], "workflow_id"?} -> the client and its secret, shown this once."""
    p = need(request, "client:write")
    body = await body_of(request)
    name, scopes, wf = clean_name(body.get("name", "")), body.get("scopes"), body.get("workflow_id") or None
    if not name:
        raise HTTPException(400, "give the client a name")
    if not isinstance(scopes, list) or not scopes or not set(scopes) <= set(auth.CLIENT_SCOPES):
        raise HTTPException(400, f"scopes: some of {', '.join(auth.CLIENT_SCOPES)}")
    if wf and not db.workflows.one({"workflow_id": str(wf).lower()}):
        raise HTTPException(400, "no such workflow in this workspace")
    view, client_secret = auth.create_client(p["workspace_id"], name, scopes, wf and str(wf).lower(), actor(p))
    db.audit("client.created", actor(p), client_id=view["client_id"], scopes=view["scopes"])
    return {"client": view, "client_secret": client_secret}


@app.delete("/api/v1/clients/{client_id}")
def revoke_client(client_id: str, request: Request):
    p = need(request, "client:write")
    if not auth.revoke_client(p["workspace_id"], client_id):
        raise HTTPException(404, "no such client")
    db.audit("client.revoked", actor(p), client_id=client_id)
    return {"ok": True}


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
