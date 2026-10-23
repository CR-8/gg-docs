"""External services, called only by the server, with their credentials kept encrypted (secret.py).

GG-Ops (the team's project-management app, github.com/Ghostfreak-cypher/GG-ops): its REST API v1, authenticated with
a GG-Ops personal API key (`Authorization: Bearer ggops_sk_…`, made in GG-Ops under Account → API keys; a read-only
key is enough) and answering in its {ok, data, error} envelope. Used here:
  GET /api/v1/workspaces                 the key works (the workspaces its user is an active member of)
  GET /api/v1/projects/{projectId}/tasks a project's board; GG-Ops answers 404 unless the key's user is an active
                                         member of the project's workspace
A GG-Docs *workflow* is a GG-Ops project linked to a workspace. Its id (the project's id) is an identifier, never a
credential: every read goes out with a GG-Ops credential, so GG-Ops's own access control decides.

People connect their own GG-Ops account (GGOPS_URL + GGOPS_CLIENT_ID: GG-Docs is an OAuth client of GG-Ops's
authorization server, PKCE, scope read). With that set, a person's reads go out with their own token and they see
what GG-Ops shows them; API clients act as no person and keep the workspace's key.

gg-pulse ("Pulse", github.com/CR-8/gg-pulse): the only HTTP API it has is GET /api/health, unauthenticated,
{"ok": true} when it and its database are up. It has no analytics API to read yet, so this integration is a
connectivity check and nothing more is invented on top of it.
"""
import ipaddress
import os
import re
import socket
import threading
import time
from collections import Counter
from urllib.parse import urlencode, urlsplit

import httpx

from . import PUBLIC_URL, db, llm, secret

SERVICES = ("gg-ops", "gg-pulse")
HTTP = httpx.Client(timeout=10, follow_redirects=False, headers={"Accept": "application/json", "User-Agent": "gg-docs"})
ATTEMPTS, BACKOFF = 3, 0.5
PROJECT_ID = re.compile(r"^[0-9a-fA-F]{24}$")  # GG-Ops ids are MongoDB ObjectIds
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_URLS") == "1" or os.environ.get("ALLOW_PRIVATE_LLM") == "1"
GGOPS_URL = os.environ.get("GGOPS_URL", "").rstrip("/")  # the GG-Ops people connect their own accounts to
GGOPS_CLIENT_ID = os.environ.get("GGOPS_CLIENT_ID", "")  # what GG-Ops's POST /oauth/register returned
REFRESH_EARLY = 60  # seconds before it expires that an access token is refreshed


class ServiceError(Exception):
    """status: not_configured | invalid | unauthorized | not_found | rate_limited | degraded | unavailable, and for a
    person's own GG-Ops account not_connected | reconnect"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def outside(url):
    """True for a public host. Tenants share this server, so a URL they configure must not reach its insides (cloud
    metadata, SonarQube, other containers). ponytail: checked when saved; a per-request check if DNS rebinding matters."""
    if ALLOW_PRIVATE or url.rstrip("/").startswith(llm.OLLAMA_URL):
        return True
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(urlsplit(url).hostname or "", None)}
    except (OSError, UnicodeError):
        return False
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


def call(base, path, key=None):
    """GET with a timeout and bounded retries (connection errors, 429, 502-504), never following redirects."""
    for attempt in range(ATTEMPTS):
        last = attempt == ATTEMPTS - 1
        try:
            r = HTTP.get(base.rstrip("/") + path, headers={"Authorization": f"Bearer {key}"} if key else {})
        except httpx.HTTPError as e:
            if last:
                raise ServiceError("unavailable", f"Could not reach {urlsplit(base).netloc} ({type(e).__name__}).")
            time.sleep(BACKOFF * 2 ** attempt)
            continue
        if r.status_code in (429, 502, 503, 504) and not last:
            time.sleep(min(float(r.headers.get("retry-after") or BACKOFF * 2 ** attempt), 5))
            continue
        return r


def envelope(r, what):
    """GG-Ops's {ok, data, error} -> data, or a ServiceError that says what to do."""
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code == 401:
        raise ServiceError("unauthorized", "GG-Ops refused the API key (wrong, revoked or expired). Create a new one "
                                           "in GG-Ops → Account → API keys.")
    if r.status_code == 404:
        raise ServiceError("not_found", f"GG-Ops has no {what} this key's user can see.")
    if r.status_code == 429:
        raise ServiceError("rate_limited", "GG-Ops is rate limiting this key; try again later.")
    if r.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
        raise ServiceError("unavailable", f"GG-Ops answered {r.status_code} for {what}.")
    return body.get("data")


def ggops_verify(base, key):
    data = envelope(call(base, "/api/v1/workspaces", key), "workspace list")
    if not isinstance(data, list):
        raise ServiceError("unavailable", "GG-Ops sent an unexpected workspace list.")
    return f"{len(data)} GG-Ops workspace(s) visible to this key"


def ggops_board(get, project_id):
    """get(path, what): GG-Ops's data for path, read with the workspace's key or a person's token."""
    if not PROJECT_ID.match(project_id or ""):
        raise ServiceError("invalid", "A workflow id is a GG-Ops project id: 24 hexadecimal characters.")
    board = get(f"/api/v1/projects/{project_id}/tasks", "project with that id")
    if not isinstance(board, dict):
        raise ServiceError("unavailable", "GG-Ops sent an unexpected project board.")
    return board


def summarize(board):
    """Only what a workflow reader needs: the project's status and its task counts, no task contents or people."""
    tasks = [t for s in board.get("sections") or [] for t in s.get("tasks") or []] + list(board.get("unsectioned") or [])
    return {"name": board.get("projectName"), "status": board.get("projectStatusColor"),
            "status_note": board.get("projectStatusNote"), "members": board.get("memberCount"),
            "tasks": {"total": len(tasks), "blocked": sum(1 for t in tasks if t.get("blockedByCount")),
                      "by_status": dict(Counter(t.get("status") for t in tasks))}}


def pulse_verify(base):
    r = call(base, "/api/health")
    try:
        ok = r.json().get("ok")
    except (ValueError, AttributeError):
        ok = None
    if r.status_code == 200 and ok is True:
        return "gg-pulse and its database are up"
    if r.status_code == 503 and ok is False:
        raise ServiceError("degraded", "gg-pulse answers, but its database is down.")
    raise ServiceError("unavailable", f"{urlsplit(base).netloc} answered {r.status_code} on /api/health; is it gg-pulse?")


# ---------- the current workspace's connections ----------

def view(ig):
    """What the API and Settings show: never the key."""
    return {"service": ig["service"], "base_url": ig.get("base_url"), "has_key": bool(ig.get("credential")),
            "status": ig.get("status", "unverified"), "verified_at": ig.get("verified_at"),
            "last_error": ig.get("last_error"), "updated_by": ig.get("updated_by"), "updated_at": ig.get("updated_at")}


def configured(service):
    ig = db.integrations.one({"service": service})
    if not ig:
        raise ServiceError("not_configured", f"{service} is not connected to this workspace.")
    return ig


def save(service, base_url, key, by):
    if service not in SERVICES:
        raise ServiceError("invalid", f"service must be one of {', '.join(SERVICES)}")
    base_url = str(base_url or "").strip().rstrip("/")
    if not base_url.startswith(("https://", "http://")) or not urlsplit(base_url).hostname:
        raise ServiceError("invalid", "The address must start with https:// (or http://).")
    if not outside(base_url):
        raise ServiceError("invalid", "The address must be public (ALLOW_PRIVATE_URLS=1 allows private ones).")
    key = str(key or "").strip()
    if service == "gg-ops" and key and not key.startswith("ggops_sk_"):
        raise ServiceError("invalid", "A GG-Ops API key starts with ggops_sk_ (GG-Ops → Account → API keys).")
    old = db.integrations.one({"service": service}) or {}
    if service == "gg-ops" and not (key or old.get("credential")):
        raise ServiceError("invalid", "GG-Ops needs an API key.")
    db.integrations.upsert({"service": service}, {
        "base_url": base_url, "credential": secret.seal(key) if key else old.get("credential"), "status": "unverified",
        "verified_at": None, "last_error": None, "updated_by": by, "updated_at": db.now()})
    db.audit("integration.saved", by, service=service, base_url=base_url, key_changed=bool(key))
    return view(db.integrations.one({"service": service}))


def remove(service, by):
    """Forgets the address and the key; linked workflows stay listed (as disconnected) so reconnecting restores them."""
    db.integrations.delete({"service": service})
    if service == "gg-ops":
        db.workflows.update({}, {"$set": {"status": "disconnected"}})
    db.audit("integration.removed", by, service=service)


def verify(service, by):
    ig = configured(service)
    try:
        detail = (ggops_verify(ig["base_url"], secret.unseal(ig["credential"])) if service == "gg-ops"
                  else pulse_verify(ig["base_url"]))
        status, error = "connected", None
    except ServiceError as e:
        status, error, detail = e.status, str(e), None
    at = db.now() if status == "connected" else ig.get("verified_at")
    db.integrations.update({"service": service}, {"$set": {"status": status, "last_error": error,
                                                           "verified_at": at, "checked_at": db.now()}})
    db.audit("integration.verified", by, service=service, status=status)
    return {"status": status, "service": service, "verifiedAt": at, "detail": detail or error}


def workflow_view(w):
    return {k: w.get(k) for k in ("workflow_id", "name", "service", "status", "verified_at", "last_error", "added_by")}


def check_workflow(workflow_id):
    """The workflow's GG-Ops board read with the workspace's key, and its stored status updated either way."""
    ig = configured("gg-ops")
    key = secret.unseal(ig["credential"])
    try:
        board = ggops_board(lambda path, what: envelope(call(ig["base_url"], path, key), what), workflow_id)
    except ServiceError as e:
        db.workflows.update({"workflow_id": workflow_id}, {"$set": {"status": e.status, "last_error": str(e)}})
        raise
    db.workflows.update({"workflow_id": workflow_id}, {"$set": {"status": "connected", "last_error": None,
                                                                "verified_at": db.now()}})
    return board


def add_workflow(workflow_id, name, by):
    workflow_id = str(workflow_id or "").strip().lower()
    board = check_workflow(workflow_id)  # exists, and this workspace's GG-Ops key may read it
    db.workflows.upsert({"workflow_id": workflow_id}, {
        "name": str(name or "").strip()[:80] or board.get("projectName") or workflow_id, "service": "gg-ops",
        "status": "connected", "last_error": None, "verified_at": db.now()}, {"added_by": by, "added_at": db.now()})
    db.audit("workflow.added", by, workflow_id=workflow_id)
    return workflow_view(db.workflows.one({"workflow_id": workflow_id}))


def remove_workflow(workflow_id, by):
    if not db.workflows.delete({"workflow_id": workflow_id}):
        return False
    db.mongo.api_clients.update_many({"workspace_id": db.WORKSPACE.get(), "workflow_id": workflow_id},
                                     {"$set": {"revoked_at": db.utcnow()}})  # a client bound to it has nothing left
    db.audit("workflow.removed", by, workflow_id=workflow_id)
    return True


# ---------- a person's own GG-Ops account (OAuth 2.1 client of GG-Ops) ----------

_refresh_locks = {}  # GG-Docs user id -> Lock


def ggops_oauth():
    return bool(GGOPS_URL and GGOPS_CLIENT_ID)


def callback_url():
    return f"{PUBLIC_URL}/connect/ggops/callback"


def authorize_url(state, challenge):
    return f"{GGOPS_URL}/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": GGOPS_CLIENT_ID, "redirect_uri": callback_url(), "scope": "read",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state})


def token_request(**form):
    """POST /oauth/token, sent once and never retried: GG-Ops may have rotated the refresh token before an answer got
    lost, and it treats a second use of the old one as theft, revoking the whole connection."""
    try:
        r = HTTP.post(f"{GGOPS_URL}/oauth/token", data={"client_id": GGOPS_CLIENT_ID, **form})
    except httpx.HTTPError as e:
        raise ServiceError("unavailable", f"Could not reach GG-Ops ({type(e).__name__}).")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 200 and isinstance(body, dict) and body.get("access_token") and body.get("refresh_token"):
        return body
    if r.status_code == 429:  # nothing rotated: the stored tokens stay good
        raise ServiceError("rate_limited", "GG-Ops is rate limiting sign-ins; try again in a few minutes.")
    if isinstance(body, dict) and body.get("error") == "invalid_grant":
        raise ServiceError("reconnect", "GG-Ops ended this connection (revoked, unused for 90 days, or reused). "
                                        "Connect GG-Ops again.")
    raise ServiceError("unavailable", f"GG-Ops answered {r.status_code} to the token request.")


def save_tokens(user_id, t, upsert=False):
    """Refreshes update a connection that still exists; only connecting creates one (a disconnect wins a race)."""
    db.ggops_connections.update_one({"user_id": user_id}, {
        "$set": {"access_token": secret.seal(t["access_token"]), "refresh_token": secret.seal(t["refresh_token"]),
                 "expires_at": time.time() + int(t.get("expires_in") or 3600), "updated_at": db.utcnow()},
        "$setOnInsert": {"connected_at": db.utcnow()}}, upsert=upsert)


def revoke(refresh_token):
    """RFC 7009 at GG-Ops, best effort: True when GG-Ops took it."""
    if not refresh_token:
        return False
    try:
        return HTTP.post(f"{GGOPS_URL}/oauth/revoke", data={"token": refresh_token}).status_code == 200
    except httpx.HTTPError:
        return False


def connect(user_id, code, verifier):
    """The callback's code swapped for tokens (with the PKCE verifier), stored encrypted for this person only; then
    who they are in GG-Ops. A connection this replaces is revoked in GG-Ops so no grant is left behind."""
    t = token_request(grant_type="authorization_code", code=code, redirect_uri=callback_url(), code_verifier=verifier)
    old = db.ggops_connections.find_one({"user_id": user_id})
    save_tokens(user_id, t, upsert=True)
    if old:
        revoke(secret.unseal(old.get("refresh_token")))
    try:
        me = envelope(call(GGOPS_URL, "/api/v1/me", t["access_token"]), "account")
    except ServiceError:
        me = None  # ponytail: a GG-Ops without /api/v1/me yet; the account shows unnamed until it has one
    if isinstance(me, dict):
        db.ggops_connections.update_one({"user_id": user_id}, {"$set": {
            "ggops_user_id": me.get("id"), "name": me.get("name"), "email": me.get("email")}})


def access_token(user_id, stale=None):
    """This person's access token, refreshed when it is about to expire or GG-Ops just refused `stale`. One refresh
    at a time per person, and the stored tokens are read again once the lock is held: a token another request
    already refreshed is used as it is, never refreshed a second time."""
    def current():
        c = db.ggops_connections.find_one({"user_id": user_id})
        if not c:
            raise ServiceError("not_connected", "Connect your GG-Ops account first (Settings → Your GG-Ops account).")
        token = secret.unseal(c["access_token"])
        return c, token if token and token != stale and c["expires_at"] - REFRESH_EARLY > time.time() else None

    c, token = current()
    if token:
        return token
    # ponytail: a lock per process, as the portal runs one; a MongoDB lease if it ever runs several
    with _refresh_locks.setdefault(user_id, threading.Lock()):
        c, token = current()
        if token:
            return token
        refresh = secret.unseal(c["refresh_token"])
        try:
            if not refresh:  # sealed with an encryption key that has changed since
                raise ServiceError("reconnect", "Connect GG-Ops again.")
            t = token_request(grant_type="refresh_token", refresh_token=refresh)
        except ServiceError as e:
            if e.status == "reconnect":
                db.ggops_connections.delete_one({"user_id": user_id})
            raise
        save_tokens(user_id, t)  # the new refresh token is stored before the new access token is used
        return t["access_token"]


def get_as(user_id, path, what):
    """GET from GG-Ops as this person. A refused token is refreshed once; refused again, they connect again."""
    token = access_token(user_id)
    r = call(GGOPS_URL, path, token)
    if r.status_code == 401:
        r = call(GGOPS_URL, path, access_token(user_id, stale=token))
    if r.status_code == 401:
        raise ServiceError("reconnect", "GG-Ops no longer accepts this connection. Connect GG-Ops again.")
    return envelope(r, what)


def user_board(user_id, workflow_id):
    """A linked workflow read as this person. The workflow's stored status is the workspace's and stays as it is:
    one person's 404 says nothing about anyone else's view."""
    return ggops_board(lambda path, what: get_as(user_id, path, what), workflow_id)


def account(user_id):
    """The person's connection and the GG-Ops workspaces it can see, read now."""
    c = db.ggops_connections.find_one({"user_id": user_id})
    if not c:
        return {"connected": False}
    spaces = get_as(user_id, "/api/v1/workspaces", "workspace list")
    return {"connected": True, "name": c.get("name"), "email": c.get("email"),
            "connected_at": c["connected_at"].isoformat(timespec="seconds"),
            "workspaces": [{k: w.get(k) for k in ("name", "slug", "role")} for w in spaces or [] if isinstance(w, dict)]}


def disconnect(user_id):
    """Forgets the person's tokens and revokes the connection in GG-Ops. If GG-Ops can't be told (down, rate
    limiting), the tokens are gone here anyway and the person can revoke it under GG-Ops → Account → API keys."""
    c = db.ggops_connections.find_one_and_delete({"user_id": user_id})
    return bool(c) and revoke(secret.unseal(c.get("refresh_token")))
