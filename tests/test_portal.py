"""Checks for the logic that is easy to get subtly wrong. Run: python -m pytest tests"""
import hashlib
import hmac
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()  # before importing portal, so the DB lands in a scratch dir
os.environ.update(PORTAL_PASSWORD="viewer-pw", ADMIN_PASSWORD="admin-pw", DISABLE_WORKER="1", MCP_TOKEN="mcp-secret")

from portal import db  # noqa: E402
from portal.chat import tool_sql  # noqa: E402
from portal.github import verify_signature  # noqa: E402
from portal.llm import anthropic_tools, openai_tools, parse_args  # noqa: E402
from portal.review import annotate, clean, make_chunks, parse_json, split_diff  # noqa: E402
from portal.sync import detect_breakages, person_for  # noqa: E402

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
    tool = {"name": "sql", "description": "d", "parameters": {"type": "object", "properties": {}}}
    assert anthropic_tools([tool]) == [{"name": "sql", "description": "d", "input_schema": tool["parameters"]}]
    assert openai_tools([tool]) == [{"type": "function", "function": tool}]
    assert parse_args('{"query": "select 1"}') == {"query": "select 1"}
    assert parse_args("{not json") == {} and parse_args("[1]") == {}


def test_people_and_readonly_sql():
    db.init()
    assert person_for("12345+octo@users.noreply.github.com") == "octo"
    db.x("INSERT INTO identities(email, login) VALUES('a@x.com', 'alice')")
    assert person_for("A@X.com") == "alice" and person_for("b@x.com") == "b@x.com"
    assert '"rows": [["alice"]]' in tool_sql("SELECT login FROM identities")
    assert "Only one SELECT" in tool_sql("DELETE FROM identities")
    assert "Only one SELECT" in tool_sql("SELECT 1; DELETE FROM identities")
    assert "SQL error" in tool_sql("WITH x AS (SELECT 1) DELETE FROM identities")  # read-only connection refuses
    assert db.q("SELECT COUNT(*) n FROM identities")[0]["n"] == 1


def test_password_login_without_github():
    from fastapi.testclient import TestClient
    from portal.main import app
    with TestClient(app) as viewer, TestClient(app) as admin:
        assert viewer.get("/api/summary").status_code == 401
        assert viewer.get("/people/", follow_redirects=False).headers["location"].startswith("/auth/login?next=")
        assert viewer.get("/auth/login", follow_redirects=False).headers["location"] == "/login/?next=/"
        assert viewer.get("/auth/methods").json() == {"github": False, "password": True, "org": "GeneGeniusinc"}
        r = viewer.post("/auth/password", data={"password": "nope", "next": "/x"}, follow_redirects=False)
        assert r.headers["location"] == "/login/?next=/x&error=Wrong%20password."
        r = viewer.post("/auth/password", data={"password": "viewer-pw", "next": "/activity"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/activity"
        assert viewer.get("/api/me").json() == {"login": "team", "admin": False}
        assert viewer.get("/api/summary").json()["github"] is None
        assert viewer.put("/api/settings", json={"llm_kind": "openai"}).status_code == 403
        r = admin.post("/auth/password", data={"password": "admin-pw", "next": "/\\evil.example"}, follow_redirects=False)
        assert r.headers["location"] == "/"  # browsers read /\host as //host
        assert admin.get("/api/me").json() == {"login": "admin", "admin": True}
        assert admin.put("/api/settings", json={"llm_kind": "openai"}).status_code == 200
        assert viewer.post("/api/admin/add_repo", json={"repo": "acme/extra"}).status_code == 403
        assert admin.post("/api/admin/add_repo", json={"repo": "acme/extra"}).status_code == 200
        assert "acme/extra" in admin.get("/api/summary").json()["repos"]
        assert admin.post("/api/admin/add_repo", json={"repo": "other/extra"}).status_code == 400  # same clone folder
        for bad in ("../etc", "acme/..", "acme/.", "a/b/c", "acme"):
            assert admin.post("/api/admin/add_repo", json={"repo": bad}).status_code == 400, bad
        admin.post("/api/admin/remove_repo", json={"repo": "acme/extra"})
        assert "acme/extra" not in admin.get("/api/summary").json()["repos"]


def test_pull_request_webhook_feeds_the_tools_and_mcp():
    from fastapi.testclient import TestClient
    from portal import REPOS
    from portal.chat import run_tool
    from portal.main import app
    from portal.sync import handle_webhook
    db.init()
    pr = {"number": 7, "title": "Add VCF parser", "user": {"login": "octo"}, "state": "open", "draft": False,
          "base": {"ref": "main"}, "head": {"ref": "feat/vcf", "sha": "a" * 40}, "created_at": "2026-10-01T00:00:00Z",
          "updated_at": "2026-10-02T00:00:00Z", "merged_at": None, "html_url": "https://github.com/x/pull/7"}
    assert handle_webhook("pull_request", {"action": "edited", "repository": {"full_name": REPOS[0]},
                                           "number": 7, "pull_request": pr}) == "queued"
    assert '"title": "Add VCF parser"' in run_tool("pull_requests", {})
    rpc = lambda c, body, **kw: c.post("/mcp", json=body, **kw)
    with TestClient(app) as c:
        assert rpc(c, {"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401  # no token
        auth = {"headers": {"Authorization": "Bearer mcp-secret"}}
        init = rpc(c, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}, **auth).json()["result"]
        assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"]
        assert rpc(c, {"jsonrpc": "2.0", "method": "notifications/initialized"}, **auth).status_code == 202
        tools = rpc(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, **auth).json()["result"]["tools"]
        assert {"pull_requests", "pull_request", "recent_pushes", "docs", "sql"} <= {t["name"] for t in tools}
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "pull_request", "arguments": {"repo": REPOS[0], "number": 7}}}
        out = c.post("/mcp/mcp-secret", json=call).json()["result"]  # token in the URL, for ChatGPT and co
        assert '"head": "feat/vcf"' in out["content"][0]["text"]
        assert rpc(c, {"jsonrpc": "2.0", "id": 4, "method": "nope"}, **auth).json()["error"]["code"] == -32601


def test_generated_docs_follow_the_code():
    import subprocess
    from fastapi.testclient import TestClient
    from portal import REPOS, docgen, repo_dir, sync
    from portal.main import app
    db.init()
    repo = "acme/tiny"
    REPOS.append(repo)
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
    slugs = {r["slug"] for r in db.q("SELECT slug FROM docs WHERE repo=?", repo)}
    assert {"overview", "architecture", "data-flow", "system-design", "api", "modules/api", "modules/services"} <= slugs
    assert "```mermaid" in db.q("SELECT content FROM docs WHERE repo=? AND slug='architecture'", repo)[0]["content"]
    versions = lambda: db.q("SELECT COUNT(*) n FROM doc_versions WHERE repo=?", repo)[0]["n"]
    before = versions()

    (d / "app/services/calc.py").write_text("import motor\n\ndef add(a, b):\n    \"\"\"Now documented.\"\"\"\n    return a + b\n")
    commit()
    docgen.run(repo)  # only the services document reads that file
    changed = db.q("SELECT slug FROM doc_versions WHERE repo=? ORDER BY id DESC LIMIT ?", repo, versions() - before)
    assert [r["slug"] for r in changed] == ["modules/services"]
    assert docgen.search("documented")[0]["slug"] == "modules/services"

    with TestClient(app) as c:
        c.post("/auth/password", data={"password": "viewer-pw", "next": "/"})
        hist = c.get("/api/docs/tiny/history/modules/services").json()
        assert len(hist) == 2 and "Now documented" in c.get(f"/api/docs/tiny/diff/{hist[0]['id']}").json()["diff"]
        assert c.get("/api/docs/tiny/modules/services?download=1").headers["content-disposition"].endswith('services.md"')
        assert c.get("/api/docs/tiny/zip").content[:2] == b"PK"

    # Once a model is set, every document is rewritten with its prose on top of the reference.
    saved = docgen.llm.configured, docgen.write_prose
    docgen.llm.configured, docgen.write_prose = (lambda: True), (lambda kind, facts: f"## Purpose\n\nWritten for {kind}.")
    try:
        docgen.run(repo)
    finally:
        docgen.llm.configured, docgen.write_prose = saved
    rows = db.q("SELECT slug, status, summary, content FROM docs WHERE repo=?", repo)
    assert all(r["status"] == "written" for r in rows)  # the API reference needs no prose, so it counts as written
    services = next(r for r in rows if r["slug"] == "modules/services")
    assert services["summary"] == "Written for module." and services["content"].index("Written for") < services["content"].index("## Reference")
    REPOS.remove(repo)
