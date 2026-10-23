"""Checks for the logic that is easy to get subtly wrong. Run: python -m pytest tests (needs a MongoDB, conftest.py)"""
import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit

from portal import WORKSPACE, db, secret
from portal.chat import tool_query
from portal.github import verify_signature
from portal.llm import anthropic_tools, openai_tools, parse_args
from portal.review import annotate, clean, make_chunks, parse_json, split_diff
from portal.sync import detect_breakages, person_for

db.init()
GITHUB_IDS = {"alice": 1}
ALICE = db.sign_in({"github_id": 1, "login": "alice", "name": "Alice", "avatar_url": None})
WS = db.create_workspace("Acme", ALICE)
WORKSPACE.set(WS)  # the tests below run inside Acme, like a request or a job would


def sign_in(client, login, next="/", github_id=None):
    """The GitHub sign-in round trip, with GitHub itself stubbed out. Returns where it lands. Each login is its own
    GitHub account (a stable numeric id) unless github_id says otherwise."""
    from portal.github import gh
    gid = github_id or GITHUB_IDS.setdefault(login, 100 + len(GITHUB_IDS))
    to_github = client.get("/auth/github", params={"next": next}, follow_redirects=False).headers["location"]
    q = parse_qs(urlsplit(to_github).query)
    seen = {}

    def oauth_login(code, uri, verifier):
        seen["verifier"] = verifier
        return {"github_id": gid, "login": login, "name": login.title(), "avatar_url": None, "token": f"t-{login}",
                "refresh_token": None, "token_expires": None}

    saved, gh.oauth_login = gh.oauth_login, oauth_login
    try:
        where = client.get(f"/auth/callback?code=x&state={q['state'][0]}", follow_redirects=False).headers["location"]
    finally:
        gh.oauth_login = saved
    if seen:  # PKCE: GitHub gets the challenge, the exchange sends its verifier
        challenge = base64.urlsafe_b64encode(hashlib.sha256(seen["verifier"].encode()).digest()).rstrip(b"=").decode()
        assert q["code_challenge"] == [challenge] and q["code_challenge_method"] == ["S256"]
    return where


def mcp_token(ws):
    return secret.unseal(db.workspaces.find_one({"_id": ws})["mcp_token"])


DIFF = """diff --git a/app/x.py b/app/x.py
index 1..2 100644
--- a/app/x.py
+++ b/app/x.py
@@ -10,4 +10,5 @@ def f():
     a = 1
-    b = 2
+    b = 3
+    print(b)
     return a
diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1 @@
-{}
+{"a": 1}
"""


def test_webhook_signature():
    body = b'{"zen": "hi"}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature("s3cret", body, good)
    assert not verify_signature("s3cret", body + b" ", good)
    assert not verify_signature("", body, good)
    assert not verify_signature("s3cret", body, None)


def test_diff_split_skips_lockfiles_and_numbers_new_lines():
    files = split_diff(DIFF)
    assert [p for p, _ in files] == ["app/x.py"]
    text, lines = annotate(files[0][1])
    assert lines == {10, 11, 12, 13}  # context 10, added 11-12, context 13; the removed line has no new number
    assert "   12 +    print(b)" in text
    assert "      -    b = 2" in text


def test_review_skips_docs_and_data_but_keeps_config():
    from portal.review import SKIP
    for path in ("README.md", "benchmarks/results/run.json", "package-lock.json", "web/dist/app.js", "x.min.js"):
        assert SKIP.search(path), path
    for path in ("package.json", "tsconfig.json", "app/main.py", ".github/workflows/ci.yml", "src/app/page.tsx"):
        assert not SKIP.search(path), path


def test_findings_are_validated_against_the_diff():
    raw = [{"path": "b/app/x.py", "line": "12", "severity": "major", "category": "slop", "title": "debug print"},
           {"path": "other.py", "line": 1, "severity": "major"},           # not in the diff
           {"path": "app/x.py", "line": "x"},                              # bad line
           {"path": "app/x.py", "line": 11, "severity": "huge", "category": "???"}]
    out = clean(raw, {"app/x.py": {10, 11, 12, 13}})
    assert [(f["line"], f["severity"], f["category"]) for f in out] == [(12, "major", "slop"), (11, "minor", "slop")]


def test_chunking_lists_oversized_files_instead_of_cutting_them():
    chunks, skipped = make_chunks([("a", "x" * 10), ("big", "y" * 170_000), ("b", "z" * 10)])
    assert skipped == ["big"] and len(chunks) == 1 and "big" not in chunks[0]


def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"summary": "ok", "findings": []}\n```') == {"summary": "ok", "findings": []}
    assert parse_json("no json here") == {}


def test_breakage_attribution():
    order = ["c1", "c2", "c3", "c4", "c5"]
    results = {"c1": {"unit": "success", "lint": "failure"},
               "c2": {"unit": "failure"},                     # unit breaks here
               "c3": {"unit": "failure", "lint": "failure"},  # still broken; lint never passed, so not attributed
               "c4": {"unit": "cancelled"},                   # ignored
               "c5": {"unit": "success"}}                     # fixed
    assert detect_breakages(order, results) == [("unit", "c2", "c1", "c5")]
    assert detect_breakages(order[:3], results) == [("unit", "c2", "c1", None)]


def test_llm_tool_formats():
    tool = {"name": "query", "description": "d", "parameters": {"type": "object", "properties": {}}}
    assert anthropic_tools([tool]) == [{"name": "query", "description": "d", "input_schema": tool["parameters"]}]
    assert openai_tools([tool]) == [{"type": "function", "function": tool}]
    assert parse_args('{"query": "select 1"}') == {"query": "select 1"}
    assert parse_args("{not json") == {} and parse_args("[1]") == {}


def test_people_and_readonly_query():
    assert person_for("12345+octo@users.noreply.github.com") == "octo"
    db.identities.insert({"email": "a@x.com", "login": "alice", "name": None})
    assert person_for("A@X.com") == "alice" and person_for("b@x.com") == "b@x.com"
    out = json.loads(tool_query("identities", [{"$project": {"login": 1}}]))
    assert out == {"rows": [{"login": "alice"}], "truncated": False}
    assert json.loads(tool_query("identities", '[{"$count": "n"}]'))["rows"] == [{"n": 1}]  # a JSON string works too
    for bad in ([{"$out": "x"}], [{"$merge": {"into": "x"}}], [{"$match": {"$where": "true"}}],
                [{"$unionWith": "settings"}], [{"$facet": {"a": [{"$lookup": {"from": "users"}}]}}],
                [{"$group": {"_id": None, "x": {"$accumulator": {}}}}]):
        assert "Not allowed" in tool_query("identities", bad), bad
    assert "Stage $collStats is not allowed" in tool_query("identities", [{"$collStats": {}}])
    assert "Unknown collection" in tool_query("settings") and "Unknown collection" in tool_query("users")
    assert "$lookup takes" in tool_query("identities", [{"$lookup": {"from": "users", "localField": "login",
                                                                      "foreignField": "login", "as": "u"}}])
    assert "$lookup takes" in tool_query("identities", [{"$lookup": {"from": "commits", "pipeline": [], "as": "u"}}])
    assert db.identities.count() == 1


def test_repository_urls():
    from portal.main import parse_repo
    for text in ("https://github.com/acme/app", "https://github.com/acme/app.git", "github.com/acme/app/tree/main/src",
                 "git@github.com:acme/app.git", "acme/app", " https://www.github.com/acme/app/ ",
                 "http://github.com/acme/app?tab=readme-ov-file"):
        assert parse_repo(text) == "acme/app", text
    assert parse_repo("acme/my.lib.git") == "acme/my.lib"
    for text in ("https://gitlab.com/acme/app", "gitlab.com/acme/app", "ftp://github.com/acme/app", "acme", "../etc",
                 "acme/..", "acme/.", "", "a b/c"):
        assert parse_repo(text) is None, text


def test_workspaces_invites_roles_and_isolation(monkeypatch):
    from fastapi.testclient import TestClient
    from portal import mail
    from portal.main import app
    sent = []
    monkeypatch.setattr(mail, "send_invite", lambda to, ws, who, link, days: sent.append((to, ws, who, link)) or True)
    db.commits.upsert_many([{"repo": "acme/app", "sha": "c0ffee", "person": "alice", "committed_at": "2099-01-01",
                             "additions": 5, "is_merge": 0}], ["repo", "sha"], only_new=True)
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as eve:
        assert alice.get("/api/summary").status_code == 401
        assert alice.get("/people/", follow_redirects=False).headers["location"].startswith("/auth/login?next=")
        assert alice.get("/auth/login", follow_redirects=False).headers["location"] == "/login/?next=/"
        forged = alice.get("/auth/callback?code=x&state=forged", follow_redirects=False).headers["location"]
        assert forged.startswith("/login/?next=/&error=")
        assert sign_in(alice, "alice", next="/\\evil.example") == "/"  # browsers read /\host as //host
        assert alice.get("/api/me").json()["workspace"] == {"id": WS, "name": "Acme"}

        sign_in(bob, "bob")  # anyone can sign in; with no workspace they land on the welcome page
        assert bob.get("/api/me").json()["workspace"] is None
        assert bob.get("/api/summary").status_code == 409
        assert bob.get("/", follow_redirects=False).headers["location"] == "/welcome/"

        r = alice.post("/api/workspace/invites", json={"email": " Bob@X.com ", "role": "member"}).json()
        assert r["emailed"] and sent == [("bob@x.com", "Acme", "alice", r["link"])]
        assert alice.post("/api/workspace/invites", json={"email": "not-an-email"}).status_code == 400
        assert alice.post("/api/workspace/invites", json={"email": "a@b.co", "role": "god"}).status_code == 400
        assert [i["email"] for i in alice.get("/api/workspace/members").json()["invites"]] == ["bob@x.com"]
        link = urlsplit(r["link"]).path
        token = link.removeprefix("/invite/")
        assert eve.get(link, follow_redirects=False).headers["location"].startswith("/login/?next=/invite/")
        # Opening the link only asks; joining takes a POST, so no other site can sign someone into a workspace.
        assert bob.get(link, follow_redirects=False).headers["location"] == f"/welcome/?invite={token}"
        assert bob.get("/api/me").json()["workspace"] is None
        assert bob.get(f"/api/invites/{token}").json() == {"workspace": "Acme", "role": "member", "invited_by": "alice"}
        assert bob.post(f"/api/invites/{token}/accept").json() == {"id": WS}
        me = bob.get("/api/me").json()
        assert me["workspace"]["name"] == "Acme" and me["role"] == "member" and not me["admin"]
        sign_in(eve, "eve")
        assert eve.get(f"/api/invites/{token}").status_code == 404  # works once
        assert eve.post(f"/api/invites/{token}/accept").status_code == 404
        assert eve.post("/api/invites/made-up/accept").status_code == 404
        assert alice.get("/api/workspace/members").json()["invites"] == []
        assert not db.invites.find_one({"token_hash": token})  # stored hashed only

        # members read everything and manage nothing
        assert bob.get("/api/summary").status_code == 200
        assert bob.post("/api/workspace/invites", json={"email": "c@x.com"}).status_code == 403
        assert bob.post("/api/import", json={"url": "acme/x"}).status_code == 403
        assert bob.put("/api/settings", json={"llm_kind": "openai"}).status_code == 403
        assert bob.put("/api/workspace/members/alice", json={"role": "member"}).status_code == 403
        assert alice.put("/api/workspace/members/alice", json={"role": "member"}).status_code == 400  # last owner
        assert alice.put("/api/workspace/members/bob", json={"role": "admin"}).json()["role"] == "admin"
        assert bob.put("/api/workspace/members/alice", json={"role": "member"}).status_code == 403  # owners only
        assert bob.post("/api/workspace/invites", json={"email": "c@x.com", "role": "owner"}).status_code == 403

        # tenants don't get to point the LLM at the server's own network
        for url in ("http://169.254.169.254/latest", "http://127.0.0.1:9000/v1", "http://10.0.0.5/v1"):
            assert alice.put("/api/settings", json={"llm_kind": "openai", "llm_base_url": url}).status_code == 400, url
        assert alice.put("/api/settings", json={"llm_kind": "openai", "llm_base_url": "http://llm:11434/v1"}).status_code == 200
        alice.put("/api/settings", json={"llm_kind": "anthropic", "llm_base_url": "", "llm_api_key": "sk-ant-secret"})
        assert alice.get("/api/settings").json()["llm_api_key"] == "saved, ends in cret"
        raw = db.settings.c.find_one({"workspace_id": WS, "key": "llm_api_key"})["value"]
        assert raw.startswith("v1:") and "sk-ant" not in raw  # encrypted at rest
        db.del_setting("llm_api_key")  # (a blank field keeps it) no model for the tests below

        # import (no GitHub configured here, so no access check): a URL in, a repository synced in this workspace
        assert alice.post("/api/import", json={"url": "https://github.com/acme/extra.git"}).json()["repo"] == "acme/extra"
        assert "acme/extra" in bob.get("/api/summary").json()["repos"]
        assert alice.post("/api/import", json={"url": "other/Extra"}).status_code == 400  # same clone folder
        for bad in ("../etc", "acme/..", "acme", "https://gitlab.com/acme/x"):
            assert alice.post("/api/import", json={"url": bad}).status_code == 400, bad

        # another tenant sees none of it, over the API or MCP (joins included), and can't switch in
        eve_ws = eve.post("/api/workspaces", json={"name": "  Eve\nCorp "}).json()
        assert eve_ws["name"] == "Eve Corp" and eve.get("/api/me").json()["role"] == "owner"
        assert eve.post(f"/api/workspaces/{WS}/switch").status_code == 403
        assert eve.get("/api/summary").json()["repos"] == [] and eve.get("/api/people").json() == []
        assert [p["person"] for p in alice.get("/api/people").json()] == ["alice"]
        count = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "query", "arguments": {"collection": "commits", "pipeline": [{"$count": "n"}]}}}
        text = lambda ws, body=count: eve.post(f"/mcp/{mcp_token(ws)}", json=body).json()["result"]["content"][0]["text"]
        assert json.loads(text(WS))["rows"] == [{"n": 1}] and json.loads(text(eve_ws["id"]))["rows"] == []
        with __import__("portal").workspace(eve_ws["id"]):
            db.identities.insert({"email": "c0ffee", "login": "x", "name": None})
        join = {**count, "params": {"name": "query", "arguments": {"collection": "identities", "pipeline": [
            {"$lookup": {"from": "commits", "localField": "email", "foreignField": "sha", "as": "found"}}]}}}
        assert json.loads(text(eve_ws["id"], join))["rows"][0]["found"] == []  # Acme's commit c0ffee stays invisible

        alice.post("/api/admin/remove_repo", json={"repo": "acme/extra"})
        assert "acme/extra" not in alice.get("/api/summary").json()["repos"]
        assert bob.delete("/api/workspace/members/bob").json() == {"ok": True}  # leaving
        assert bob.get("/api/summary").status_code == 409


def test_import_goes_by_the_users_own_github_access(monkeypatch):
    """The App may be installed on another tenant's private repository: only the user's own token decides."""
    from fastapi.testclient import TestClient
    from portal.github import GitHub, gh
    from portal.main import app
    readable = {"acme/site", "acme/noapp"}
    tokens = []

    class Reply:
        def __init__(self, repo):
            self.repo, self.status_code = repo, 200 if repo in readable else 404

        def json(self):  # GitHub's spelling wins
            return {"id": 42, "full_name": self.repo.replace("acme", "Acme"), "private": True, "visibility": "private",
                    "owner": {"login": "Acme", "type": "Organization"}, "default_branch": "main", "description": "Site",
                    "html_url": f"https://github.com/{self.repo}"}

    monkeypatch.setattr(GitHub, "mode", property(lambda self: "app"))
    monkeypatch.setattr(gh, "request", lambda method, path, check=True, token=None, **kw:
                        tokens.append(token) or Reply(path.removeprefix("/repos/")))
    monkeypatch.setattr(gh, "installation", lambda repo: None if repo.endswith("noapp") else 1)
    monkeypatch.setattr(gh, "install_url", lambda: "https://github.com/apps/portal/installations/new")
    with TestClient(app) as c:
        sign_in(c, "alice")
        r = c.post("/api/import", json={"url": "https://github.com/other/private"})
        assert r.status_code == 400 and r.json()["install_url"].endswith("/installations/new")
        assert c.post("/api/import", json={"url": "acme/noapp"}).json()["install_url"]  # readable, but no App
        assert c.post("/api/import", json={"url": "github.com/acme/site"}).json()["repo"] == "Acme/site"
    assert set(tokens) == {"t-alice"}  # the token stored (encrypted) at sign-in
    link = db.repo_links.find_one({"workspace_id": WS, "repo_lower": "acme/site"})
    assert (link["github_id"], link["visibility"], link["default_branch"], link["status"]) == (42, "private", "main", "active")
    db.repo_links.delete_many({"repo_lower": "acme/site"})


def test_pull_request_webhook_feeds_the_tools_and_mcp():
    from fastapi.testclient import TestClient
    from portal import workspace
    from portal.chat import run_tool
    from portal.main import app
    from portal.sync import handle_webhook
    db.add_repo(WS, "acme/app", "alice")
    dave = db.sign_in({"github_id": 9, "login": "dave"})
    other = db.create_workspace("Other", dave)  # imported the same repository: gets the same webhooks
    db.add_repo(other, "acme/app", "dave")
    pr = {"number": 7, "title": "Add VCF parser", "user": {"login": "octo"}, "state": "open", "draft": False,
          "base": {"ref": "main"}, "head": {"ref": "feat/vcf", "sha": "a" * 40}, "created_at": "2026-10-01T00:00:00Z",
          "updated_at": "2026-10-02T00:00:00Z", "merged_at": None, "html_url": "https://github.com/x/pull/7"}
    event = {"action": "edited", "repository": {"full_name": "Acme/App"}, "number": 7, "pull_request": pr}
    assert handle_webhook("pull_request", event) == "queued"
    assert handle_webhook("pull_request", {**event, "repository": {"full_name": "acme/unknown"}}) == "ignored"
    stale = {**pr, "title": "Old title", "updated_at": "2026-09-01T00:00:00Z"}  # an older delivery doesn't win
    handle_webhook("pull_request", {**event, "pull_request": stale})
    assert '"title": "Add VCF parser"' in run_tool("pull_requests", {})
    with workspace(other):
        assert '"repo": "acme/app"' in run_tool("pull_requests", {})
    rpc = lambda c, body, **kw: c.post("/mcp", json=body, **kw)
    with TestClient(app) as c:
        assert rpc(c, {"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401  # no token
        assert rpc(c, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": "Bearer x"}).status_code == 401
        auth = {"headers": {"Authorization": f"Bearer {mcp_token(WS)}"}}
        init = rpc(c, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}, **auth).json()["result"]
        assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"]
        assert "pushes {id, repo" in init["instructions"]  # the collections, described for the agent
        assert rpc(c, {"jsonrpc": "2.0", "method": "notifications/initialized"}, **auth).status_code == 202
        tools = rpc(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, **auth).json()["result"]["tools"]
        assert {"pull_requests", "pull_request", "recent_pushes", "docs", "query"} <= {t["name"] for t in tools}
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "pull_request", "arguments": {"repo": "acme/app", "number": 7}}}
        out = c.post(f"/mcp/{mcp_token(WS)}", json=call).json()["result"]  # token in the URL, for ChatGPT and co
        assert '"head": "feat/vcf"' in out["content"][0]["text"]
        assert rpc(c, {"jsonrpc": "2.0", "id": 4, "method": "nope"}, **auth).json()["error"]["code"] == -32601


def test_generated_docs_follow_the_code():
    import subprocess
    from fastapi.testclient import TestClient
    from portal import docgen, repo_dir, sync
    from portal.main import app
    repo = "acme/tiny"
    db.add_repo(WS, repo, "alice")
    sync.DEFAULT_BRANCHES[repo] = "main"
    d = repo_dir(repo)
    files = {
        "app/main.py": '"""Entry point."""\nfrom fastapi import FastAPI\nfrom app.api.routes import router\napp = FastAPI()\n',
        "app/api/routes.py": 'from fastapi import APIRouter\nfrom app.services.calc import add\nrouter = APIRouter(prefix="/v1")\n\n'
                             '@router.get("/sum")\ndef total(a: int, b: int):\n    """Adds two numbers."""\n    return add(a, b)\n',
        "app/api/health.py": 'import os\nTOKEN = os.environ.get("API_TOKEN")\n',
        "app/api/util.py": "def x():\n    pass\n",
        "app/services/calc.py": "import motor\n\ndef add(a, b):\n    return a + b\n",
        "app/services/more.py": "def y():\n    pass\n",
        "app/services/other.py": "def z():\n    pass\n",
        "app/core/a.py": "", "app/core/b.py": "", "app/core/c.py": "", "app/core/d.py": "",
        "tests/test_x.py": "def test(): pass\n",
    }
    for path, text in files.items():
        (d / path).parent.mkdir(parents=True, exist_ok=True)
        (d / path).write_text(text)
    git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=d, check=True, capture_output=True)
    commit = lambda: (git("add", "-A"), git("commit", "-qm", "x"), git("update-ref", "refs/remotes/origin/main", "HEAD"))
    git("init", "-q")
    commit()

    a = docgen.analyze(repo)
    assert a["root"] == "app" and "tests/test_x.py" not in a["files"]
    assert set(a["components"]) == {"api", "services", "core", "(root)"}
    assert ["GET", "/v1/sum", "total", "app/api/routes.py"] in a["components"]["api"]["routes"]
    assert ["api", "services", 1] in a["edges"] and "API_TOKEN" in a["env"] and "motor" in a["externals"]
    docgen.run(repo)
    slugs = {r["slug"] for r in db.docs.find({"repo": repo}, fields=["slug"])}
    assert {"overview", "architecture", "data-flow", "system-design", "api", "modules/api", "modules/services"} <= slugs
    assert "```mermaid" in db.docs.one({"repo": repo, "slug": "architecture"})["content"]
    arch = db.setting(f"architecture:{repo}")  # what Overview charts, saved by the same run
    assert arch["root"] == "app" and {c["name"] for c in arch["components"]} == {"api", "services", "core", "(root)"}
    assert ["api", "services", 1] in arch["edges"] and arch["languages"] == {"python": arch["lines"]}
    assert arch["diagrams"]["components"].startswith("flowchart") and "Database" in arch["integrations"]
    # an app kept in one folder (my-app/src) is stepped into, so @/ imports resolve; a real monorepo is not
    assert docgen.source_root([f"my-app/src/lib/f{i}.ts" for i in range(40)] + ["my-app/proxy.ts", "scripts/x.ts"]) == "my-app/src"
    assert docgen.source_root([f"web/src/c{i}.ts" for i in range(85)] + [f"api/m{i}.py" for i in range(15)]) == ""
    versions = lambda: db.doc_versions.count({"repo": repo})
    before = versions()

    (d / "app/services/calc.py").write_text("import motor\n\ndef add(a, b):\n    \"\"\"Now documented.\"\"\"\n    return a + b\n")
    commit()
    docgen.run(repo)  # only the services document reads that file
    changed = db.doc_versions.find({"repo": repo}, sort=[("id", -1)], limit=versions() - before)
    assert [r["slug"] for r in changed] == ["modules/services"]
    hit = docgen.search("documented")[0]
    assert hit["slug"] == "modules/services" and "**documented**" in hit["snippet"]
    assert docgen.search("docum")[0]["slug"] == "modules/services"  # words match as prefixes
    assert docgen.search("zzz-nothing") == [] and docgen.search("") == []

    with TestClient(app) as c:
        sign_in(c, "alice")
        hist = c.get("/api/docs/tiny/history/modules/services").json()
        assert len(hist) == 2 and hist[0]["size"] > 0
        assert "Now documented" in c.get(f"/api/docs/tiny/diff/{hist[0]['id']}").json()["diff"]
        assert c.get("/api/docs/tiny/modules/services?download=1").headers["content-disposition"].endswith('services.md"')
        assert c.get("/api/docs/tiny/zip").content[:2] == b"PK"
        assert [d["kind"] for d in c.get("/api/docs").json()[-1]["docs"][:2]] == ["overview", "architecture"]
        tiny = next(a for a in c.get("/api/architecture").json() if a["repo"] == repo)
        assert tiny["name"] == "tiny" and tiny["files"] == 11 and tiny["routes"][0][:2] == ["GET", "/v1/sum"]

    # Once a model is set, every document is rewritten with its prose on top of the reference.
    saved = docgen.llm.configured, docgen.write_prose
    docgen.llm.configured, docgen.write_prose = (lambda: True), (lambda kind, facts: f"## Purpose\n\nWritten for {kind}.")
    try:
        docgen.run(repo)
    finally:
        docgen.llm.configured, docgen.write_prose = saved
    rows = db.docs.find({"repo": repo}, fields=["slug", "status", "summary", "content"])
    assert all(r["status"] == "written" for r in rows)  # the API reference needs no prose, so it counts as written
    services = next(r for r in rows if r["slug"] == "modules/services")
    assert services["summary"] == "Written for module." and services["content"].index("Written for") < services["content"].index("## Reference")


def test_ask_goes_before_background_work():
    import threading
    from portal import llm
    entered = threading.Event()

    def background_request():
        with llm.background():
            entered.set()

    with llm.interactive():
        t = threading.Thread(target=background_request)
        t.start()
        assert not entered.wait(0.3)  # documentation and reviews wait while someone is asking
    assert entered.wait(2)
    t.join()
