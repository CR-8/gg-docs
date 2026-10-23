"""GitHub-first sign-in, repository access, API clients, login handoff, integrations, MongoDB and the SQLite
migration. GitHub, GG-Ops and gg-pulse are stubbed; MongoDB is real (conftest.py)."""
import base64
import contextvars
import hashlib
import json
import sqlite3
import threading
import time
from datetime import timedelta
from urllib.parse import parse_qs, parse_qsl, unquote, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from legacy_schema import CONTROL_SCHEMA, SCHEMA
from portal import auth, db, integrations, secret, workspace
from portal.github import GitHub, RateLimited, gh
from portal.main import app
from test_portal import GITHUB_IDS, WS, sign_in

PID, PID2 = "a" * 24, "c" * 24


def client():
    return TestClient(app)


# ---------- GitHub sign-in ----------

def test_sign_in_is_keyed_by_github_id_with_state_and_pkce(monkeypatch):
    import portal.main as main
    with client() as c, client() as other:
        to_github = urlsplit(c.get("/auth/github", follow_redirects=False).headers["location"])
        q = parse_qs(to_github.query)
        assert to_github.netloc == "github.com" and "scope" not in q and q["redirect_uri"] == ["http://localhost:7000/auth/callback"]
        assert c.get("/auth/callback?error=access_denied", follow_redirects=False).headers["location"].endswith(
            "error=You%20cancelled%20the%20GitHub%20sign-in.")

        # a failed code exchange leaves no session behind
        monkeypatch.setattr(gh, "oauth_login", lambda *a: (_ for _ in ()).throw(PermissionError("bad_verification_code")))
        state = parse_qs(urlsplit(c.get("/auth/github", follow_redirects=False).headers["location"]).query)["state"][0]
        assert "error=GitHub%20sign-in%20failed" in c.get(f"/auth/callback?code=x&state={state}", follow_redirects=False).headers["location"]
        assert c.get("/api/me").status_code == 401
        monkeypatch.undo()

        # the state works once, and not after 10 minutes
        sign_in(c, "carol", github_id=501)
        assert c.get("/api/me").json()["github_id"] == 501
        monkeypatch.setattr(main, "OAUTH_STATE_SECONDS", -1)
        assert "expired" in sign_in(other, "carol", github_id=501)
        assert other.get("/api/me").status_code == 401
        monkeypatch.undo()

        # renaming the GitHub account keeps the same user and workspaces; someone taking the old name gets neither
        ws = c.post("/api/workspaces", json={"name": "Carol's"}).json()["id"]
        sign_in(c, "carol-renamed", github_id=501)
        me = c.get("/api/me").json()
        assert me["login"] == "carol-renamed" and ws in [w["id"] for w in me["workspaces"]]
        assert db.users.count_documents({"github_id": 501}) == 1
        assert db.members.find_one({"workspace_id": ws})["login"] == "carol-renamed"
        sign_in(other, "carol", github_id=502)
        assert other.get("/api/me").json()["workspaces"] == []

        # logout ends the session; the stored GitHub token is encrypted
        raw = db.github_accounts.find_one({"github_id": 501})
        assert raw["token"].startswith("v1:") and "t-carol" not in json.dumps(raw, default=str)
        c.get("/auth/logout")
        assert c.get("/api/me").status_code == 401


def test_memberships_from_before_first_sign_in_are_claimed_by_that_login_only():
    ws = db.next_id("workspaces")
    db.workspaces.insert_one({"_id": ws, "name": "Legacy", "created_at": db.now()})
    db.members.insert_one({"workspace_id": ws, "login": "Dana", "login_lower": "dana", "role": "owner",
                           "joined_at": db.now(), "pending": True, "user_id": None})
    with client() as dan, client() as dana:
        sign_in(dan, "dan")
        assert dan.get("/api/me").json()["workspaces"] == []
        sign_in(dana, "DANA")
        assert dana.get("/api/me").json()["workspaces"] == [{"id": ws, "name": "Legacy", "role": "owner"}]
        m = db.members.find_one({"workspace_id": ws})
        assert not m["pending"] and m["user_id"] == db.users.find_one({"login_lower": "dana"})["_id"]


def test_disconnecting_github_revokes_the_grant_and_ends_every_session(monkeypatch):
    revoked = []
    monkeypatch.setattr(gh, "revoke_grant", lambda token: revoked.append(token) or True)
    with client() as laptop, client() as phone:
        sign_in(laptop, "erin")
        sign_in(phone, "erin")
        assert laptop.post("/api/v1/github/disconnect").json() == {"ok": True, "revoked_on_github": True}
        assert revoked == ["t-erin"] and not db.github_accounts.find_one({"github_id": GITHUB_IDS["erin"]})
        assert laptop.get("/api/me").status_code == 401 and phone.get("/api/me").status_code == 401


def test_cookie_requests_from_other_sites_are_refused():
    with client() as c:
        sign_in(c, "alice")
        assert c.post("/api/workspaces", json={"name": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert c.post("/api/workspaces", json={"name": "x"}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert c.get("/api/me", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200  # reads are not changes
        ok = c.post("/api/workspaces", json={"name": "Same site"}, headers={"Origin": "http://testserver"})
        assert ok.status_code == 200
        c.post(f"/api/workspaces/{WS}/switch")


# ---------- repositories ----------

def gh_repo(full, owner_type="User", private=False, pushed="2026-10-01"):
    owner = full.split("/")[0]
    return {"id": abs(hash(full)) % 10**8, "full_name": full, "private": private, "visibility": "private" if private else "public",
            "owner": {"login": owner, "type": owner_type}, "default_branch": "main", "description": f"{full} repo",
            "html_url": f"https://github.com/{full}", "pushed_at": pushed}


def test_repository_discovery_selection_refresh_and_disconnect(monkeypatch):
    from portal import sync
    mine, org, shared = gh_repo("alice/notes", private=True), gh_repo("acme/api", "Organization"), gh_repo("bob/lib")
    readable = {r["full_name"] for r in (mine, org, shared)}
    pages = {"/user/installations": [{"id": 1}, {"id": 2}],
             "/user/installations/1/repositories": [mine, org], "/user/installations/2/repositories": [org, shared]}
    monkeypatch.setattr(GitHub, "mode", property(lambda self: "app"))
    monkeypatch.setattr(gh, "paginate", lambda path, key=None, limit=1000, token=None, **kw: pages[path])
    monkeypatch.setattr(gh, "install_url", lambda: "https://github.com/apps/portal/installations/new")
    monkeypatch.setattr(gh, "installation", lambda repo: 1)

    class Reply:
        def __init__(self, repo):
            self.repo = next((r for r in (mine, org, shared) if r["full_name"].lower() == repo.lower()), None)
            self.status_code = 200 if self.repo and self.repo["full_name"] in readable else 404

        def json(self):
            return self.repo

    monkeypatch.setattr(gh, "request", lambda method, path, check=True, token=None, **kw: Reply(path.removeprefix("/repos/")))
    with client() as alice, client() as bob, client() as eve:
        sign_in(alice, "alice")
        listing = alice.get("/api/v1/github/repositories").json()
        assert listing["total"] == 3  # both installations, the shared repository once
        assert {r["repo"]: r["access"] for r in listing["repositories"]} == {
            "alice/notes": "owner", "acme/api": "organization", "bob/lib": "collaborator"}
        assert [r["repo"] for r in alice.get("/api/v1/github/repositories?access=organization").json()["repositories"]] == ["acme/api"]
        assert alice.get("/api/v1/github/repositories?q=lib").json()["total"] == 1
        page2 = alice.get("/api/v1/github/repositories?per_page=1&page=2").json()
        assert len(page2["repositories"]) == 1 and page2["total"] == 3

        readable.discard("bob/lib")  # GitHub no longer lets alice read it
        r = alice.post(f"/api/v1/workspaces/{WS}/repositories", json={"repositories": ["alice/notes", "bob/lib", "nope"]}).json()
        assert [x["ok"] for x in r["results"]] == [True, False, False] and r["results"][1]["install_url"]
        repos = alice.get(f"/api/v1/workspaces/{WS}/repositories").json()["repositories"]
        notes = next(x for x in repos if x["repo"] == "alice/notes")
        assert (notes["visibility"], notes["default_branch"], notes["status"]) == ("private", "main", "active")
        assert next(x for x in alice.get("/api/v1/github/repositories").json()["repositories"] if x["repo"] == "alice/notes")["connected"]

        sign_in(bob, "bob")  # not a member of Acme (any more)
        assert bob.get(f"/api/v1/workspaces/{WS}/repositories").status_code == 404
        sign_in(eve, "eve")
        assert eve.delete(f"/api/v1/workspaces/{WS}/repositories/{notes['id']}").status_code == 404

        readable.discard("alice/notes")  # access revoked after it was connected
        after = alice.post(f"/api/v1/workspaces/{WS}/repositories/refresh").json()["repositories"]
        gone = next(x for x in after if x["repo"] == "alice/notes")
        assert gone["status"] == "revoked" and "Install the App" in gone["status_detail"]

        monkeypatch.setattr(GitHub, "enabled", property(lambda self: True))
        monkeypatch.setattr(gh, "get", lambda path, token=None, **kw: (_ for _ in ()).throw(PermissionError("not installed")))
        with workspace(WS):
            assert sync.still_reachable("alice/notes") is False  # the sync worker skips it and keeps its data
            assert sync.still_reachable("alice/gone") is False and "alice/gone" not in db.repos()  # never re-created

        monkeypatch.setattr(gh, "paginate", lambda *a, **kw: (_ for _ in ()).throw(RateLimited(30)))
        limited = alice.get("/api/v1/github/repositories")
        assert limited.status_code == 429 and limited.headers["retry-after"] == "30"

        assert alice.delete(f"/api/v1/workspaces/{WS}/repositories/nonsense").status_code == 404
        assert alice.delete(f"/api/v1/workspaces/{WS}/repositories/{notes['id']}").json()["repo"] == "alice/notes"
        assert "alice/notes" not in alice.get("/api/summary").json()["repos"]


# ---------- API clients and tokens ----------

def new_client(c, ws=WS, **body):
    r = c.post("/api/v1/clients", json={"name": "CI", "scopes": ["repository:read", "mcp:read"], **body})
    assert r.status_code == 200, r.text
    return r.json()


def get_token(c, cl, **extra):
    return c.post("/api/v1/auth/token", json={"grant_type": "client_credentials", "client_id": cl["client"]["client_id"],
                                              "client_secret": cl["client_secret"], **extra})


def test_api_clients_get_short_lived_scoped_tokens(monkeypatch):
    with client() as alice, client() as bob, client() as bot:
        sign_in(alice, "alice")
        sign_in(bob, "bob")
        assert bob.post("/api/v1/clients", json={"name": "x", "scopes": ["mcp:read"]}).status_code in (403, 409)
        assert alice.post("/api/v1/clients", json={"name": "x", "scopes": ["client:write"]}).status_code == 400
        cl = new_client(alice)
        stored = db.api_clients.find_one({"client_id": cl["client"]["client_id"]})
        assert cl["client_secret"] not in json.dumps(stored, default=str) and "secret_hash" not in json.dumps(alice.get("/api/v1/clients").json())

        r = get_token(bot, cl)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        tok = r.json()
        assert tok["expires_in"] == 3600 and tok["scope"] == "mcp:read repository:read" and tok["token_type"] == "Bearer"
        assert not db.access_tokens.find_one({"token_hash": tok["access_token"]})  # only its digest is stored
        form = bot.post("/api/v1/auth/token", data={"grant_type": "client_credentials", "scope": "mcp:read",
                                                    "client_id": cl["client"]["client_id"], "client_secret": cl["client_secret"]})
        assert form.json()["scope"] == "mcp:read"
        assert get_token(bot, {**cl, "client_secret": "wrong"}).json()["error"] == "invalid_client"
        assert get_token(bot, {"client": {"client_id": "ggd_ci_nope"}, "client_secret": "x"}).status_code == 401
        assert get_token(bot, cl, scope="repository:write").json()["error"] == "invalid_scope"
        assert get_token(bot, cl, workflow_id=PID).json()["error"] == "invalid_request"  # not a workflow of its workspace
        assert bot.post("/api/v1/auth/token", json={"grant_type": "password"}).json()["error"] == "unsupported_grant_type"

        h = {"Authorization": f"Bearer {tok['access_token']}"}
        me = bot.get("/api/v1/auth/me", headers=h).json()
        assert me["client"]["id"] == cl["client"]["client_id"] and me["workspace"]["id"] == WS
        assert bot.get(f"/api/v1/workspaces/{WS}/repositories", headers=h).status_code == 200
        assert bot.get(f"/api/v1/workspaces/{WS + 999}/repositories", headers=h).status_code == 404
        refused = bot.delete(f"/api/v1/workspaces/{WS}/repositories/{'0' * 24}", headers=h)
        assert refused.status_code == 403 and "repository:write" in refused.json()["error"]
        assert bot.get("/api/v1/clients", headers=h).status_code == 403
        assert bot.post(f"/api/v1/workspaces/{WS}/repositories", headers=h, json={"repositories": ["a/b"]}).status_code == 403
        assert bot.get("/api/v1/github/repositories", headers=h).status_code == 403  # not a person
        assert bot.get("/api/summary", headers=h).status_code == 401  # tokens are for /api/v1 and /mcp only
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert bot.post("/mcp", json=ping, headers=h).json()["result"] == {}
        assert bot.post(f"/mcp/{tok['access_token']}", json=ping).status_code == 401  # never in URLs
        assert bot.get("/api/v1/auth/me", headers={"Authorization": "Bearer ggd_at_forged"}).status_code == 401

        fresh = {"Authorization": f"Bearer {get_token(bot, cl).json()['access_token']}"}
        assert bot.post("/api/v1/auth/logout", headers=fresh).json() == {"ok": True}
        assert bot.get("/api/v1/auth/me", headers=fresh).status_code == 401  # revoked

        live = {"Authorization": f"Bearer {get_token(bot, cl).json()['access_token']}"}
        assert alice.delete(f"/api/v1/clients/{cl['client']['client_id']}").json() == {"ok": True}
        assert bot.get("/api/v1/auth/me", headers=live).status_code == 401  # revoking the client ends its tokens
        assert get_token(bot, cl).status_code == 401

        monkeypatch.setattr(auth, "over_limit", lambda *a: 7)
        slow = get_token(bot, cl)
        assert slow.status_code == 429 and slow.headers["retry-after"] == "7"
    audit = [e["action"] for e in db.audit_events.find({"meta.client_id": cl["client"]["client_id"]})]
    assert {"client.created", "client.revoked"} <= set(audit)
    assert cl["client_secret"] not in json.dumps(list(db.audit_events.find({}, {"_id": 0})), default=str)


def test_rate_limit_counts_in_mongodb():
    key = f"t:{db.utcnow().timestamp()}"
    assert [auth.over_limit(key, 2, 3600) > 0 for _ in range(3)] == [False, False, True]


def test_expired_token_is_refused_even_before_the_ttl_sweep():
    with client() as alice, client() as bot:
        sign_in(alice, "alice")
        cl = new_client(alice)
        token = get_token(bot, cl).json()["access_token"]
        db.access_tokens.update_one({"token_hash": secret.digest(token)}, {"$set": {"expires_at": db.utcnow() - timedelta(seconds=1)}})
        assert bot.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ---------- login handoff ----------

def test_login_handoff_is_single_use_short_lived_and_stays_on_site():
    with client() as alice, client() as browser, client() as bot:
        sign_in(alice, "alice")
        url = alice.post("/api/v1/auth/login-handoff", json={"next": "/docs/"}).json()["url"]
        code = parse_qs(urlsplit(url).query)["code"][0]
        assert not db.login_handoffs.find_one({"code_hash": code})  # stored as a digest
        r = browser.get(urlsplit(url).path + "?" + urlsplit(url).query, follow_redirects=False)
        assert r.headers["location"] == "/docs/" and r.headers["referrer-policy"] == "no-referrer"
        assert browser.get("/api/me").json()["login"] == "alice"
        with client() as again:
            assert "error=" in again.get(f"/auth/handoff?code={code}", follow_redirects=False).headers["location"]

        for evil in ("//evil.example", "https://evil.example", "/\\evil.example", "/api/v1/clients"):
            u = alice.post("/api/v1/auth/login-handoff", json={"next": evil}).json()["url"]
            with client() as b:
                assert b.get("/auth/handoff?" + urlsplit(u).query, follow_redirects=False).headers["location"] == "/"

        stale = alice.post("/api/v1/auth/login-handoff", json={}).json()["url"]
        db.login_handoffs.update_many({}, {"$set": {"expires_at": db.utcnow() - timedelta(seconds=1)}})
        with client() as b:
            assert "error=" in b.get("/auth/handoff?" + urlsplit(stale).query, follow_redirects=False).headers["location"]

        # an API client needs portal:handoff, and may only sign in members of its own workspace, pinned to it
        plain = new_client(alice)
        h = {"Authorization": f"Bearer {get_token(bot, plain).json()['access_token']}"}
        assert bot.post("/api/v1/auth/login-handoff", headers=h, json={"github_id": 1}).status_code == 403
        cl = new_client(alice, scopes=["portal:handoff"])
        h = {"Authorization": f"Bearer {get_token(bot, cl).json()['access_token']}"}
        assert bot.post("/api/v1/auth/login-handoff", headers=h, json={"github_id": 999999}).status_code == 404
        assert bot.post("/api/v1/auth/login-handoff", headers=h, json={}).status_code == 400
        db.create_workspace("Alice elsewhere", db.user_view(db.users.find_one({"github_id": 1})))
        u = bot.post("/api/v1/auth/login-handoff", headers=h, json={"github_id": 1, "next": "/people/"}).json()["url"]
        with client() as b:
            assert b.get("/auth/handoff?" + urlsplit(u).query, follow_redirects=False).headers["location"] == "/people/"
            assert [w["id"] for w in b.get("/api/me").json()["workspaces"]] == [WS]  # pinned to the client's workspace
            assert b.post("/api/workspaces", json={"name": "nope"}).status_code == 403


# ---------- GG-Ops and gg-pulse ----------

BOARD = {"projectId": PID, "projectName": "Variant pipeline", "projectStatusColor": "AT_RISK", "projectStatusNote": "late",
         "memberCount": 4, "sections": [{"id": "s", "name": "Doing", "position": 0, "tasks": [
             {"id": "t1", "title": "secret task title", "status": "IN_PROGRESS", "blockedByCount": 1},
             {"id": "t2", "title": "another", "status": "DONE", "blockedByCount": 0}]}],
         "unsectioned": [{"id": "t3", "title": "x", "status": "TODO", "blockedByCount": 0}]}


@pytest.fixture
def services(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        key = request.headers.get("authorization", "")
        host, path = request.url.host, request.url.path
        if host == "ops.example.com":
            if key != "Bearer ggops_sk_good":
                return httpx.Response(401, json={"ok": False, "error": "Unauthorized"})
            if path == "/api/v1/workspaces":
                return httpx.Response(200, json={"ok": True, "data": [{"id": "w", "name": "GG", "slug": "gg", "role": "MEMBER"}]})
            if path in (f"/api/v1/projects/{PID}/tasks", f"/api/v1/projects/{PID2}/tasks"):
                return httpx.Response(200, json={"ok": True, "data": {**BOARD, "projectName": path[17:20]} if PID2 in path else BOARD})
            return httpx.Response(404, json={"ok": False, "error": "Not found"})
        if host == "pulse.example.com":
            return httpx.Response(200, json={"ok": True})
        if host == "pulse-down.example.com":
            return httpx.Response(503, json={"ok": False})
        if host == "flaky.example.com":
            return httpx.Response(503 if len([c for c in calls if "flaky" in c]) < 3 else 200, json={"ok": True})
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(integrations, "HTTP", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(integrations, "BACKOFF", 0)
    monkeypatch.setattr(integrations, "outside", lambda url: "internal" not in url)
    yield calls
    with workspace(WS):
        for s in integrations.SERVICES:
            integrations.remove(s, "test")
        db.workflows.delete({})


def test_gg_ops_workflows_are_identifiers_checked_through_gg_ops(services):
    with client() as alice, client() as bob, client() as bot, client() as eve:
        sign_in(alice, "alice")
        put = lambda key, base="https://ops.example.com": alice.put("/api/v1/integrations/gg-ops", json={"base_url": base, "api_key": key})
        assert put("not-a-key").status_code == 400
        assert put("ggops_sk_good", "http://internal.example").status_code == 400  # no private addresses
        saved = put("ggops_sk_good").json()
        assert saved["has_key"] and "ggops_sk_good" not in json.dumps(saved)
        raw = db.integrations.c.find_one({"workspace_id": WS, "service": "gg-ops"})["credential"]
        assert raw.startswith("v1:") and "ggops_sk_good" not in raw
        listed = alice.get("/api/v1/integrations").json()
        assert "ggops_sk" not in json.dumps(listed) and listed["integrations"][1] == {"service": "gg-pulse", "status": "not_configured"}

        v = alice.post("/api/v1/integrations/connections/verify", json={"service": "gg-ops"}).json()
        assert v["status"] == "connected" and v["service"] == "gg-ops" and v["verifiedAt"]
        sign_in(bob, "bob")  # bob left Acme in an earlier test: not a member, so nothing to see
        assert bob.get("/api/v1/integrations").status_code in (404, 409)

        assert alice.post("/api/v1/integrations/workflows", json={"workflow_id": "123"}).status_code == 400
        assert alice.post("/api/v1/integrations/workflows", json={"workflow_id": "b" * 24}).status_code == 404
        wf = alice.post("/api/v1/integrations/workflows", json={"workflow_id": PID.upper()}).json()
        assert wf["workflow_id"] == PID and wf["name"] == "Variant pipeline" and wf["status"] == "connected"
        alice.post("/api/v1/integrations/workflows", json={"workflow_id": PID2, "name": "Other flow"})

        got = alice.get(f"/api/v1/integrations/workflows/{PID}").json()
        assert got["project"]["tasks"] == {"total": 3, "blocked": 1, "by_status": {"IN_PROGRESS": 1, "DONE": 1, "TODO": 1}}
        assert "secret task title" not in json.dumps(got)  # counts only, no task contents

        # knowing a workflow id grants nothing: no credentials, another workspace, or a client bound elsewhere
        assert eve.get(f"/api/v1/integrations/workflows/{PID}").status_code == 401
        sign_in(eve, "eve")
        eve.post("/api/workspaces", json={"name": "Eve's own"})
        assert eve.get(f"/api/v1/integrations/workflows/{PID}").status_code == 404
        bound = new_client(alice, scopes=["workflow:read", "integration:read"], workflow_id=PID)
        assert get_token(bot, bound, workflow_id=PID2).json()["error"] == "invalid_request"
        h = {"Authorization": f"Bearer {get_token(bot, bound).json()['access_token']}"}
        assert bot.get(f"/api/v1/integrations/workflows/{PID}", headers=h).status_code == 200
        assert bot.get(f"/api/v1/integrations/workflows/{PID2}", headers=h).status_code == 404
        assert [w["workflow_id"] for w in bot.get("/api/v1/integrations", headers=h).json()["workflows"]] == [PID]
        assert bot.put("/api/v1/integrations/gg-ops", headers=h, json={"base_url": "https://x.example"}).status_code == 403

        # a revoked GG-Ops key shows up as a status, and reads fail without leaking anything
        put("ggops_sk_revoked")
        assert alice.post("/api/v1/integrations/connections/verify", json={"service": "gg-ops"}).json()["status"] == "unauthorized"
        r = alice.get(f"/api/v1/integrations/workflows/{PID}")
        assert r.status_code == 502 and "API key" in r.json()["error"]
        assert db.workflows.c.find_one({"workspace_id": WS, "workflow_id": PID})["status"] == "unauthorized"

        assert alice.delete(f"/api/v1/integrations/workflows/{PID}").json() == {"ok": True}
        assert get_token(bot, bound).status_code == 401  # a client bound to a removed workflow is revoked
        alice.delete("/api/v1/integrations/gg-ops")
        assert db.workflows.c.find_one({"workspace_id": WS, "workflow_id": PID2})["status"] == "disconnected"


def test_gg_pulse_is_a_health_check_with_bounded_retries(services):
    with client() as alice:
        sign_in(alice, "alice")
        verify = lambda: alice.post("/api/v1/integrations/connections/verify", json={"service": "gg-pulse"}).json()
        assert alice.post("/api/v1/integrations/connections/verify", json={"service": "gg-pulse"}).status_code == 404
        assert alice.post("/api/v1/integrations/connections/verify", json={"service": "nope"}).status_code == 400
        alice.put("/api/v1/integrations/gg-pulse", json={"base_url": "https://pulse.example.com/"})
        assert verify()["status"] == "connected"
        alice.put("/api/v1/integrations/gg-pulse", json={"base_url": "https://pulse-down.example.com"})
        assert verify()["status"] == "degraded"  # retried, then reported, never invented
        assert len([c for c in services if "pulse-down" in c]) == integrations.ATTEMPTS
        alice.put("/api/v1/integrations/gg-pulse", json={"base_url": "https://flaky.example.com"})
        assert verify()["status"] == "connected"  # two 503s, then up
        alice.put("/api/v1/integrations/gg-pulse", json={"base_url": "https://gone.example.com"})
        out = verify()
        assert out["status"] == "unavailable" and "Could not reach" in out["detail"]


# ---------- the legacy MCP token ----------

# ---------- a person's own GG-Ops account ----------

OPS = "https://ops.example.com"


def s256(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@pytest.fixture
def ggops(monkeypatch):
    """GG-Ops's OAuth server and API as the portal meets them: the refresh token rotates on every use, and a second
    use of an old one revokes the whole grant (exchange-token.ts in GG-Ops)."""
    s = {"n": 0, "access": None, "refresh": None, "used": set(), "revoked": False, "challenge": None, "refreshes": 0,
         "revokes": [], "scope": "read write", "mcp": []}

    def mint():
        s["n"] += 1
        s["access"], s["refresh"] = f"ggops_sk_a{s['n']}", f"r{s['n']}"
        return httpx.Response(200, json={"access_token": s["access"], "refresh_token": s["refresh"],
                                         "token_type": "Bearer", "expires_in": 3600, "scope": s["scope"]})

    def found(data):
        return httpx.Response(200, json={"ok": True, "data": data})

    def handler(request):
        path = request.url.path
        form = dict(parse_qsl(request.content.decode())) if request.method == "POST" else {}
        if path == "/oauth/token":
            assert form["client_id"] == "mcp_test"
            if form["grant_type"] == "authorization_code":
                if form["code"] != "good-code" or form["redirect_uri"] != "http://localhost:7000/connect/ggops/callback" \
                        or s256(form["code_verifier"]) != s["challenge"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                s["revoked"] = False  # a new grant
                return mint()
            s["refreshes"] += 1
            time.sleep(0.2)  # slow enough that a second refresh would overlap this one
            s["revoked"] = s["revoked"] or form["refresh_token"] in s["used"]
            if s["revoked"] or form["refresh_token"] != s["refresh"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            s["used"].add(form["refresh_token"])
            return mint()
        if path == "/oauth/revoke":
            s["revokes"].append(form["token"])
            s["revoked"] = s["revoked"] or form["token"] == s["refresh"]
            return httpx.Response(200)
        if s["revoked"] or request.headers.get("authorization") != f"Bearer {s['access']}":
            return httpx.Response(401, json={"ok": False, "error": "Unauthorized"})
        if path == "/api/mcp":  # GG-Ops's MCP server: tools filtered by the token's scopes, calls recorded
            rpc = json.loads(request.content)
            if rpc["method"] == "tools/list":
                tools = [{"name": "list_projects", "description": "Projects", "inputSchema": {"type": "object"}}]
                if "write" in s["scope"].split():
                    tools.append({"name": "create_task", "description": "Create a task", "inputSchema": {"type": "object"}})
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"], "result": {"tools": tools}})
            s["mcp"].append((rpc["params"]["name"], rpc["params"]["arguments"]))
            text = json.dumps({"created": rpc["params"]["arguments"]})
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"],
                                             "result": {"content": [{"type": "text", "text": text}], "isError": False}})
        if path == "/api/v1/me":
            return found({"id": "u1", "name": "Alice Ops", "email": "alice@acme.example"})
        if path == "/api/v1/workspaces":
            return found([{"id": "w", "name": "Acme Ops", "slug": "acme", "role": "MEMBER"}])
        if path == f"/api/v1/projects/{PID}/tasks":
            return found(BOARD)
        return httpx.Response(404, json={"ok": False, "error": "Not found"})

    monkeypatch.setattr(integrations, "HTTP", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(integrations, "BACKOFF", 0)
    monkeypatch.setattr(integrations, "GGOPS_URL", OPS)
    monkeypatch.setattr(integrations, "GGOPS_CLIENT_ID", "mcp_test")
    yield s
    db.ggops_connections.delete_many({})
    with workspace(WS):
        db.workflows.delete({})


def connect_ggops(c, s, start="/connect/ggops", **answer):
    """/connect/ggops, GG-Ops's consent (stubbed: approved, or `answer` overrides), the callback; where it lands."""
    to = urlsplit(c.get(start, follow_redirects=False).headers["location"])
    if to.netloc != "ops.example.com":
        return to.path
    q = parse_qs(to.query)
    s["challenge"] = q["code_challenge"][0]
    params = {"code": "good-code", "state": q["state"][0], "iss": OPS, **answer}
    return unquote(c.get("/connect/ggops/callback", params=params, follow_redirects=False).headers["location"])


def test_people_read_gg_ops_as_themselves(ggops):
    alice_id = db.users.find_one({"github_id": 1})["_id"]
    with workspace(WS):
        for pid in (PID, PID2):
            db.workflows.upsert({"workflow_id": pid}, {"name": pid[:3], "service": "gg-ops", "status": "connected"})
    with client() as alice, client() as anon:
        # GG-Ops's "Open in GG-Docs" works signed out: sign-in first, and its query survives
        assert anon.get("/connect/ggops?workspace=acme", follow_redirects=False).headers["location"] == \
            "/auth/login?next=/connect/ggops%3Fworkspace%3Dacme"

        sign_in(alice, "alice")
        to = urlsplit(alice.get("/connect/ggops", follow_redirects=False).headers["location"])
        q = parse_qs(to.query)
        assert f"{to.scheme}://{to.netloc}{to.path}" == f"{OPS}/oauth/authorize"
        assert q["client_id"] == ["mcp_test"] and q["scope"] == ["read write"] and q["code_challenge_method"] == ["S256"]
        assert q["redirect_uri"] == ["http://localhost:7000/connect/ggops/callback"]

        # not connected: no GG-Ops data at all, and no falling back to the workspace's key
        r = alice.get(f"/api/v1/integrations/workflows/{PID}")
        assert r.status_code == 403 and "Connect your GG-Ops account" in r.json()["error"]

        # denied, another issuer, a code GG-Ops won't swap: nothing stored
        assert "didn't allow" in connect_ggops(alice, ggops, error="access_denied")
        assert "did not come from" in connect_ggops(alice, ggops, iss="https://evil.example")
        assert "Connect GG-Ops again" in connect_ggops(alice, ggops, code="stolen")
        assert db.ggops_connections.count_documents({}) == 0

        assert connect_ggops(alice, ggops) == "/settings/"
        raw = db.ggops_connections.find_one({"user_id": alice_id})
        assert raw["access_token"].startswith("v1:") and "ggops_sk_a" not in json.dumps(raw, default=str)
        me = alice.get("/api/v1/integrations/gg-ops/me").json()
        assert me["connected"] and me["name"] == "Alice Ops" and me["workspaces"] == [{"name": "Acme Ops", "slug": "acme", "role": "MEMBER"}]
        assert alice.get("/connect/ggops", follow_redirects=False).headers["location"] == "/settings/"  # straight in

        # reads go out as her: what GG-Ops doesn't show her is a 404, and her view isn't written as the workspace's
        assert alice.get(f"/api/v1/integrations/workflows/{PID}").json()["project"]["tasks"]["total"] == 3
        assert alice.get(f"/api/v1/integrations/workflows/{PID2}").status_code == 404
        with workspace(WS):
            assert db.workflows.one({"workflow_id": PID2})["status"] == "connected"

        # two requests at expiry: one refresh, both use its token, and the grant survives
        db.ggops_connections.update_one({"user_id": alice_id}, {"$set": {"expires_at": 0}})
        got = []
        threads = [threading.Thread(target=lambda: got.append(integrations.access_token(alice_id))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert ggops["refreshes"] == 1 and got == [ggops["access"]] * 2 and not ggops["revoked"]

        # an access token GG-Ops refuses early is refreshed once
        ggops["access"] = "gone"
        assert alice.get(f"/api/v1/integrations/workflows/{PID}").status_code == 200 and ggops["refreshes"] == 2

        # revoked in GG-Ops: the tokens are forgotten and she is asked to connect again
        ggops["revoked"] = True
        r = alice.get(f"/api/v1/integrations/workflows/{PID}")
        assert r.status_code == 403 and "Connect GG-Ops again" in r.json()["error"]
        assert alice.get("/api/v1/integrations/gg-ops/me").json()["connected"] is False

        # disconnect: forgotten here and revoked in GG-Ops
        assert connect_ggops(alice, ggops) == "/settings/"
        refresh = ggops["refresh"]
        assert alice.delete("/api/v1/integrations/gg-ops/me").json() == {"ok": True, "revoked_in_ggops": True}
        assert ggops["revokes"][-1] == refresh and db.ggops_connections.count_documents({}) == 0


def test_mcp_agents_use_gg_ops_as_their_person(ggops):
    """A person's own MCP token is them: GG-Ops's tools come along as ggops_*, run in GG-Ops with their token. A workspace
    token is no person and gets none of it."""
    def rpc(c, token, method, **params):
        return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                      headers={"Authorization": f"Bearer {token}"}).json()

    def names(c, token):
        return [t["name"] for t in rpc(c, token, "tools/list")["result"]["tools"]]

    with client() as alice, client() as agent:
        sign_in(alice, "alice")
        mine = alice.get("/api/settings/mcp/me").json()
        token, shared = mine["token"], alice.get("/api/settings/mcp").json()["token"]
        assert alice.get("/api/settings/mcp/me").json()["token"] == token  # made once, then shown again
        assert db.members.find_one({"mcp_token_hash": secret.digest(token)})["mcp_token"] != token  # sealed at rest
        assert mine["ggops"] and not mine["ggops_connected"]

        # not connected to GG-Ops: our tools only, and a ggops_ call says why
        assert "query" in names(agent, token) and not [n for n in names(agent, token) if n.startswith("ggops_")]
        assert "Connect your GG-Ops account" in rpc(agent, token, "tools/call", name="ggops_create_task",
                                                    arguments={})["result"]["content"][0]["text"]

        # connected read-only (before writes were asked for): reads only, and Settings says to connect again
        ggops["scope"] = "read"
        assert connect_ggops(alice, ggops) == "/settings/"
        assert not alice.get("/api/settings/mcp/me").json()["ggops_write"]
        assert [n for n in names(agent, token) if n.startswith("ggops_")] == ["ggops_list_projects"]

        # connecting again grants write; the old grant is revoked in GG-Ops
        ggops["scope"] = "read write"
        old_refresh = ggops["refresh"]
        assert alice.get("/connect/ggops", follow_redirects=False).headers["location"] == "/settings/"  # connected: straight in
        assert connect_ggops(alice, ggops, start="/connect/ggops?again=1") == "/settings/" and old_refresh in ggops["revokes"]
        assert alice.get("/api/settings/mcp/me").json()["ggops_write"]
        assert {"ggops_list_projects", "ggops_create_task"} <= set(names(agent, token))
        assert "ggops_*" in rpc(agent, token, "initialize", protocolVersion="2025-11-25")["result"]["instructions"]

        # a call goes to GG-Ops once, as her, with GG-Ops's own name and arguments
        out = rpc(agent, token, "tools/call", name="ggops_create_task", arguments={"title": "Ship mvp", "project": "p1"})
        assert out["result"]["isError"] is False and "Ship mvp" in out["result"]["content"][0]["text"]
        assert ggops["mcp"] == [("create_task", {"title": "Ship mvp", "project": "p1"})]

        # a token GG-Ops refuses is refreshed and the call sent again (a 401 means nothing happened)
        ggops["access"] = "gone"
        assert rpc(agent, token, "tools/call", name="ggops_list_projects", arguments={})["result"]["isError"] is False
        assert len(ggops["mcp"]) == 2

        # the workspace token is no person: no GG-Ops tools, and a ggops_ call is refused
        assert not [n for n in names(agent, shared) if n.startswith("ggops_")]
        assert rpc(agent, shared, "tools/call", name="ggops_create_task", arguments={})["result"]["isError"]
        assert len(ggops["mcp"]) == 2

        # also in the URL, for clients that only take one; a new token ends the old one
        assert agent.post(f"/mcp/{token}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200
        new = alice.post("/api/settings/mcp/me/rotate").json()["token"]
        assert agent.post(f"/mcp/{token}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
        assert agent.post(f"/mcp/{new}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200


def test_gg_ops_database_is_refused():
    db.mongo.create_collection("oauthgrants")
    try:
        with pytest.raises(RuntimeError, match="GG-Ops's database"):
            db.init()
    finally:
        db.mongo.drop_collection("oauthgrants")


def test_legacy_mcp_token_can_be_rotated_and_turned_off():
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    with client() as alice, client() as agent:
        sign_in(alice, "alice")
        old = alice.get("/api/settings/mcp").json()["token"]
        assert db.workspaces.find_one({"_id": WS})["mcp_token"] != old  # sealed at rest, looked up by digest
        assert agent.post(f"/mcp/{old}", json=ping).status_code == 200
        new = alice.post("/api/settings/mcp/rotate").json()["token"]
        assert agent.post(f"/mcp/{old}", json=ping).status_code == 401 and agent.post(f"/mcp/{new}", json=ping).status_code == 200
        assert alice.put("/api/settings/mcp", json={"legacy": False}).json()["token"] is None
        assert agent.post(f"/mcp/{new}", json=ping).status_code == 401
        alice.put("/api/settings/mcp", json={"legacy": True})


# ---------- MongoDB ----------

def test_mongodb_scoping_indexes_secrets_and_health(monkeypatch):
    with pytest.raises(LookupError):  # outside a workspace, tenant data can't be reached at all
        contextvars.Context().run(db.commits.find)
    with workspace(WS):
        db.commits.c.insert_one({"workspace_id": WS, "repo": "u/x", "sha": "1"})
        with pytest.raises(DuplicateKeyError):
            db.commits.c.insert_one({"workspace_id": WS, "repo": "u/x", "sha": "1"})
        db.commits.c.insert_one({"workspace_id": WS + 1000, "repo": "u/x", "sha": "1"})  # another tenant may
        assert db.commits.count({"repo": "u/x"}) == 1 and "workspace_id" not in db.commits.one({"repo": "u/x"})
        db.mongo.commits.delete_many({"repo": "u/x"})
        db.set_setting("llm_api_key", "sk-123")
        assert db.setting("llm_api_key") == "sk-123" and "sk-" not in db.settings.c.find_one({"key": "llm_api_key"})["value"]
        db.del_setting("llm_api_key")
    assert secret.unseal("v1:garbage") == "" and secret.unseal(None) == ""
    with client() as c:
        assert c.get("/healthz").json() == {"ok": True, "database": "up"}
        from pymongo import MongoClient
        monkeypatch.setattr(db, "client", MongoClient("mongodb://127.0.0.1:1", serverSelectionTimeoutMS=200))
        assert c.get("/healthz").status_code == 503
        with pytest.raises(Exception):
            db.init()  # no silent start on an empty database


# ---------- SQLite -> MongoDB migration ----------

def legacy_install(root):
    """A multi-tenant SQLite install of the previous release: workspace 50 with two members, history and settings."""
    con = sqlite3.connect(root / "control.db")
    con.executescript(CONTROL_SCHEMA)
    con.execute("INSERT INTO workspaces VALUES(50, 'Legacy co', 'legacy-mcp-token', 'lead', '2026-01-01T00:00:00+00:00')")
    con.executemany("INSERT INTO members VALUES(50, ?, ?, '2026-01-01')", [("Lead", "owner"), ("dev2", "member")])
    con.execute("INSERT INTO users(login, name, avatar_url, token) VALUES('Lead', 'Lead Person', 'https://a/x.png', 'gho_plain')")
    con.execute("INSERT INTO invites VALUES(9, 50, 'n@x.com', 'member', 'hash9', 'Lead', '2026-01-02', NULL, NULL)")
    con.execute("INSERT INTO repos VALUES(50, 'acme/legacy', 'Lead', '2026-01-01')")
    con.commit()
    con.close()
    (root / "workspaces" / "50").mkdir(parents=True)
    w = sqlite3.connect(root / "workspaces" / "50" / "portal.db")
    w.executescript(SCHEMA)
    w.execute("""INSERT INTO settings VALUES('llm_api_key', '"sk-legacy"'), ('llm_model', '"m1"')""")
    w.executemany("INSERT INTO commits(repo, sha, person, committed_at, additions, deletions, is_merge) VALUES(?,?,?,?,?,?,0)",
                  [("acme/legacy", f"s{i}", "lead", f"2026-01-0{i}", i, 0) for i in range(1, 6)])
    w.execute("INSERT INTO pushes VALUES(3, 'acme/legacy', 'main', 'b', 'a', 'lead', '2026-01-05', 0, 2, 'webhook')")
    w.executemany("INSERT INTO push_commits VALUES(3, ?)", [("s4",), ("s5",)])
    w.execute("INSERT INTO reviews(id, repo, pr, branch, head_sha, summary, created_at) VALUES(7, 'acme/legacy', 4, '', 'a', 'ok', '2026-01-05')")
    w.executemany("INSERT INTO findings(id, review_id, path, line, severity, category, title, person) VALUES(?, 7, 'x.py', 1, ?, 'bug', 't', 'lead')",
                  [(11, "major"), (12, "critical")])
    w.execute("INSERT INTO breakages(id, repo, branch, check_name, broken_sha, last_good_sha, person, broken_at) "
              "VALUES(2, 'acme/legacy', 'main', 'unit', 's5', 's4', 'lead', '2026-01-05')")
    w.execute("INSERT INTO docs(repo, slug, kind, title, content, status) VALUES('acme/legacy', 'overview', 'overview', 'O', 'text', 'reference')")
    w.executemany("INSERT INTO doc_versions(id, repo, slug, head_sha, content) VALUES(?, 'acme/legacy', 'overview', 'a', ?)", [(1, "v1"), (2, "v2")])
    w.executemany("INSERT INTO events(id, at, kind, detail, ok) VALUES(?, '2026-01-01', 'sync', 'x', 1)", [(i,) for i in range(1, 4)])
    w.execute("INSERT INTO docs_fts VALUES('acme/legacy', 'overview', 'O', 'text')")
    w.commit()
    w.close()


def test_sqlite_migration_is_dry_run_first_resumable_idempotent_and_verified(tmp_path, monkeypatch):
    from portal import migrate
    for module in (migrate, db):
        monkeypatch.setattr(module, "DATA", tmp_path)
    legacy_install(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    with pytest.raises(RuntimeError, match="python -m portal.migrate"):
        db.init()  # the portal won't start over unmigrated SQLite data

    assert migrate.main([]) == 0  # dry run: nothing written
    assert not db.workspaces.find_one({"_id": 50}) and not db.mongo.commits.count_documents({"workspace_id": 50})
    assert migrate.main(["--apply", "--batch", "2"]) == 0
    db.init()  # now it starts
    assert sorted(p.name for p in tmp_path.rglob("*") if not p.name.startswith("migration-report")) == before  # SQLite untouched

    w = db.workspaces.find_one({"_id": 50})
    assert w["name"] == "Legacy co" and w["mcp_token_hash"] == secret.digest("legacy-mcp-token") and "legacy-mcp-token" not in json.dumps(w, default=str)
    lead = db.members.find_one({"workspace_id": 50, "login_lower": "lead"})
    assert lead["pending"] and lead["role"] == "owner" and lead["name"] == "Lead Person"
    assert db.invites.find_one({"id": 9})["token_hash"] == "hash9"
    assert not db.users.find_one({"login_lower": "lead"})  # people are not migrated: they sign in with GitHub again
    with workspace(50):
        assert db.repos() == ["acme/legacy"] and db.commits.count() == 5
        assert db.setting("llm_api_key") == "sk-legacy" and db.setting("llm_model") == "m1"
        assert "sk-legacy" not in db.settings.c.find_one({"workspace_id": 50, "key": "llm_api_key"})["value"]
        assert db.pushes.one({"id": 3})["commit_shas"] == ["s4", "s5"]
        assert sorted(f["id"] for f in db.findings.find({"review_id": 7})) == [11, 12]
        assert db.next_id("reviews") == 8 and db.next_id("doc_versions") == 3  # ids continue after the old ones
    assert db.next_id("workspaces") > 50 and db.next_id("invites") > 9

    assert migrate.main(["--apply"]) == 0  # again: nothing changes
    assert db.mongo.commits.count_documents({"workspace_id": 50}) == 5

    # interrupted after rowid 3: a re-run resumes from there
    db.mongo.commits.delete_many({"workspace_id": 50, "sha": {"$in": ["s4", "s5"]}})
    db.migrations.update_one({"_id": "sqlite:50:commits"}, {"$set": {"last_rowid": 3}})
    assert migrate.main(["--apply"]) == 0 and db.mongo.commits.count_documents({"workspace_id": 50}) == 5
    # a progress mark ahead of what landed: verification notices, --restart reads everything again
    db.mongo.commits.delete_many({"workspace_id": 50, "sha": {"$in": ["s1", "s2", "s3"]}})
    assert migrate.main(["--verify"]) == 1
    assert migrate.main(["--apply"]) == 1  # resuming skips rows 1-3, so verification still fails
    assert migrate.main(["--apply", "--restart"]) == 0 and db.mongo.commits.count_documents({"workspace_id": 50}) == 5

    with client() as c:  # the GitHub account holding the login claims the membership at its first sign-in
        sign_in(c, "lead")
        assert c.get("/api/me").json()["workspace"] == {"id": 50, "name": "Legacy co"}
        assert c.get("/api/reviews/7").json()["findings"][0]["severity"] == "critical"
        assert c.get("/api/docs/legacy/diff/2").json()["diff"]
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert c.post("/mcp", json=ping, headers={"Authorization": "Bearer legacy-mcp-token"}).status_code == 200


def test_single_tenant_install_becomes_a_workspace(tmp_path, monkeypatch):
    from portal import migrate
    for module in (migrate, db):
        monkeypatch.setattr(module, "DATA", tmp_path)
    monkeypatch.setattr(migrate, "ADMINS", {"lead"})
    old = sqlite3.connect(tmp_path / "portal.db")
    old.executescript(SCHEMA)
    old.execute("""INSERT INTO settings VALUES('extra_repos', '["acme/extra"]')""")
    old.execute("INSERT INTO commits(repo, sha) VALUES('acme/extra', 'abc')")
    old.commit()
    old.close()
    (tmp_path / "repos" / "backend").mkdir(parents=True)
    assert migrate.main(["--apply"]) == 0
    assert migrate.main(["--apply"]) == 0  # and again: the same workspace, nothing more
    (w,) = db.workspaces.find({"migrated_from": "single-tenant"})
    ws = w["_id"]
    assert w["name"] == "your-org" and db.members.find_one({"workspace_id": ws})["login"] == "lead"
    assert (tmp_path / "portal.db").exists() and (tmp_path / f"workspaces/{ws}/repos/backend").is_dir()
    with workspace(ws):
        assert db.repos() == ["your-org/backend", "your-org/frontend", "acme/extra"]
        assert db.commits.find(fields=["sha"]) == [{"sha": "abc"}]
        assert db.setting("views") == {"backend": "your-org/backend",
                                       "frontend": "your-org/frontend"}
    db.migrations.delete_many({})
