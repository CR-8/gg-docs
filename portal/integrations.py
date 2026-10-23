"""External services, called only by the server, with their credentials kept encrypted (secret.py).

GG-Ops (the team's project-management app, github.com/Ghostfreak-cypher/GG-ops): its REST API v1, authenticated with
a GG-Ops personal API key (`Authorization: Bearer ggops_sk_…`, made in GG-Ops under Account → API keys; a read-only
key is enough) and answering in its {ok, data, error} envelope. Used here:
  GET /api/v1/workspaces                 the key works (the workspaces its user is an active member of)
  GET /api/v1/projects/{projectId}/tasks a project's board; GG-Ops answers 404 unless the key's user is an active
                                         member of the project's workspace
A GG-Docs *workflow* is a GG-Ops project linked to a workspace. Its id (the project's id) is an identifier, never a
credential: every read goes out with the workspace's stored key, so GG-Ops's own access control decides.

gg-pulse ("Pulse", github.com/CR-8/gg-pulse): the only HTTP API it has is GET /api/health, unauthenticated,
{"ok": true} when it and its database are up. It has no analytics API to read yet, so this integration is a
connectivity check and nothing more is invented on top of it.
"""
import ipaddress
import os
import re
import socket
import time
from collections import Counter
from urllib.parse import urlsplit

import httpx

from . import db, llm, secret

SERVICES = ("gg-ops", "gg-pulse")
HTTP = httpx.Client(timeout=10, follow_redirects=False, headers={"Accept": "application/json", "User-Agent": "gg-docs"})
ATTEMPTS, BACKOFF = 3, 0.5
PROJECT_ID = re.compile(r"^[0-9a-fA-F]{24}$")  # GG-Ops ids are MongoDB ObjectIds
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_URLS") == "1" or os.environ.get("ALLOW_PRIVATE_LLM") == "1"


class ServiceError(Exception):
    """status: not_configured | invalid | unauthorized | not_found | rate_limited | degraded | unavailable"""

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


def ggops_board(base, key, project_id):
    if not PROJECT_ID.match(project_id or ""):
        raise ServiceError("invalid", "A workflow id is a GG-Ops project id: 24 hexadecimal characters.")
    board = envelope(call(base, f"/api/v1/projects/{project_id}/tasks", key), "project with that id")
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
    try:
        board = ggops_board(ig["base_url"], secret.unseal(ig["credential"]), workflow_id)
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
