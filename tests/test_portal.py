"""Checks for the logic that is easy to get subtly wrong. Run: python -m pytest tests"""
import hashlib
import hmac
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()  # before importing portal, so the DB lands in a scratch dir
os.environ.update(PORTAL_PASSWORD="viewer-pw", ADMIN_PASSWORD="admin-pw", DISABLE_WORKER="1")

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
        assert 'name="password"' in viewer.get("/auth/login").text
        assert viewer.post("/auth/password", data={"password": "nope", "next": "/"}).status_code == 401
        r = viewer.post("/auth/password", data={"password": "viewer-pw", "next": "/activity"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/activity"
        assert viewer.get("/api/me").json() == {"login": "team", "admin": False}
        assert viewer.get("/api/summary").json()["github"] is None
        assert viewer.put("/api/settings", json={"llm_kind": "openai"}).status_code == 403
        admin.post("/auth/password", data={"password": "admin-pw", "next": "//evil.example"}, follow_redirects=False)
        assert admin.get("/api/me").json() == {"login": "admin", "admin": True}
        assert admin.put("/api/settings", json={"llm_kind": "openai"}).status_code == 200
