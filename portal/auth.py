"""Credentials besides the GitHub sign-in: API clients and their short-lived access tokens (OAuth 2.0 client
credentials, RFC 6749 §4.4), single-use browser login handoffs, and rate limits.

A person signs in with GitHub and holds a session cookie; a machine is an API client a workspace admin registered.
They are different principals and never stand in for each other: a client gets only the scopes it was registered
with, inside its own workspace, and a GitHub token is never accepted as a GG-Docs credential.
"""
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from . import db, secret

TOKEN_TTL = 3600     # seconds an access token lives
HANDOFF_TTL = 60     # seconds a login handoff can be redeemed in
CLIENT_SCOPES = {
    "repository:read": "list the workspace's connected repositories",
    "repository:write": "disconnect repositories",
    "workflow:read": "read the GG-Ops workflows linked to the workspace",
    "integration:read": "see integration status and run connection checks",
    "mcp:read": "use the MCP server",
    "portal:handoff": "sign a member of the workspace into the portal (single-use link, with that member's role: "
                      "grant only to services trusted as much)",
}
MEMBER_SCOPES = ("repository:read", "workflow:read", "integration:read", "mcp:read", "portal:handoff")
ADMIN_SCOPES = MEMBER_SCOPES + ("repository:write", "integration:write", "client:write")


def user_scopes(role):
    return ADMIN_SCOPES if role in ("owner", "admin") else MEMBER_SCOPES if role else ("portal:handoff",)


class OAuthError(Exception):
    """An RFC 6749 §5.2 error response."""

    def __init__(self, error, description, status=400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


# ---------- API clients ----------

def client_view(c):
    return {"client_id": c["client_id"], "name": c["name"], "scopes": c["scopes"], "workflow_id": c.get("workflow_id"),
            "created_by": c.get("created_by"), "created_at": iso(c.get("created_at")),
            "last_used_at": iso(c.get("last_used_at")), "revoked": bool(c.get("revoked_at"))}


def create_client(ws, name, scopes, workflow_id, by):
    """Returns (view, client_secret): the secret is shown once and only its digest is kept."""
    client_secret, digest = secret.mint("ggd_cs_")
    doc = {"client_id": "ggd_ci_" + secrets.token_hex(10), "secret_hash": digest, "workspace_id": ws, "name": name,
           "scopes": sorted(scopes), "workflow_id": workflow_id, "created_by": by, "created_at": db.utcnow(),
           "last_used_at": None, "revoked_at": None}
    db.api_clients.insert_one(doc)
    return client_view(doc), client_secret


def revoke_client(ws, client_id):
    """Revoked clients can't get tokens, and the tokens they hold stop working at once."""
    done = db.api_clients.update_one({"client_id": client_id, "workspace_id": ws, "revoked_at": None},
                                     {"$set": {"revoked_at": db.utcnow()}}).modified_count
    db.access_tokens.delete_many({"client_id": client_id})
    return bool(done)


def issue_token(client_id, client_secret, scope=None, workflow_id=None):
    c = db.api_clients.find_one({"client_id": str(client_id or ""), "revoked_at": None})
    # compare digests in constant time; an unknown client costs the same as a wrong secret
    if not hmac.compare_digest(c["secret_hash"] if c else "0" * 64, secret.digest(client_secret or "")) or not c:
        raise OAuthError("invalid_client", "Unknown client, wrong secret or revoked client.", 401)
    asked = set(scope.split() if isinstance(scope, str) else scope or c["scopes"])
    if not asked <= set(c["scopes"]):
        raise OAuthError("invalid_scope", f"This client may ask for: {' '.join(c['scopes'])}.")
    bound = c.get("workflow_id")
    if workflow_id and bound and workflow_id != bound:
        raise OAuthError("invalid_request", "This client is bound to another workflow.")
    if workflow_id and not bound and not db.mongo.workflows.find_one({"workspace_id": c["workspace_id"],
                                                                     "workflow_id": workflow_id}):
        raise OAuthError("invalid_request", "No such workflow in this client's workspace.")
    token, digest = secret.mint("ggd_at_")
    now = db.utcnow()
    db.access_tokens.insert_one({"token_hash": digest, "client_id": c["client_id"], "workspace_id": c["workspace_id"],
                                 "scopes": sorted(asked), "workflow_id": workflow_id or bound, "created_at": now,
                                 "expires_at": now + timedelta(seconds=TOKEN_TTL)})
    db.api_clients.update_one({"_id": c["_id"]}, {"$set": {"last_used_at": now}})
    return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL, "scope": " ".join(sorted(asked)),
            "workflow_id": workflow_id or bound}


def token_principal(token):
    """The client behind a bearer token, or None (unknown, expired, or its client was revoked)."""
    t = db.access_tokens.find_one({"token_hash": secret.digest(token)})
    if not t or t["expires_at"] <= db.utcnow():  # the TTL index only sweeps once a minute
        return None
    c = db.api_clients.find_one({"client_id": t["client_id"], "revoked_at": None}, {"name": 1})
    if not c:
        return None
    return {"type": "client", "client_id": t["client_id"], "name": c["name"], "workspace_id": t["workspace_id"],
            "scopes": tuple(t["scopes"]), "workflow_id": t.get("workflow_id"), "token_hash": t["token_hash"],
            "expires_at": iso(t["expires_at"])}


def revoke_token(token_hash):
    db.access_tokens.delete_one({"token_hash": token_hash})


# ---------- browser login handoff ----------

def create_handoff(user_id, ws, next_path, by):
    """A single-use code bound to one user, one workspace and one internal path, redeemable for HANDOFF_TTL seconds.
    It is not an API credential: on its own it can only start that one browser session."""
    code, digest = secret.mint("")
    db.login_handoffs.insert_one({"code_hash": digest, "user_id": user_id, "workspace_id": ws, "next": next_path,
                                  "created_by": by, "created_at": db.utcnow(), "used_at": None,
                                  "expires_at": db.utcnow() + timedelta(seconds=HANDOFF_TTL)})
    return code


def redeem_handoff(code):
    """Marks the handoff used and returns it, atomically, so a code works once; None when unknown, used or expired."""
    return db.login_handoffs.find_one_and_update(
        {"code_hash": secret.digest(code or ""), "used_at": None, "expires_at": {"$gt": db.utcnow()}},
        {"$set": {"used_at": db.utcnow()}})


# ---------- rate limits ----------

def over_limit(key, limit, window):
    """Fixed-window counter in MongoDB (shared by every worker, survives restarts). Returns seconds to wait, or 0."""
    n = int(time.time() // window)
    end = datetime.fromtimestamp((n + 1) * window, timezone.utc)
    doc = db.rate_limits.find_one_and_update({"_id": f"{key}:{n}"}, {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": end}},
                                             upsert=True, return_document=ReturnDocument.AFTER)
    return max(1, int((end - db.utcnow()).total_seconds())) if doc["count"] > limit else 0
