"""SQLite storage. One short-lived connection per call; WAL lets the worker write while pages read.

control.db holds the tenants (CONTROL_SCHEMA); each workspace has its own portal.db with everything else (SCHEMA),
so a workspace's queries, including the chatbot's raw SQL, can only ever see that workspace's data."""
import json
import os
import secrets
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from . import ADMINS, BACKEND_REPO, DATA, FRONTEND_REPO, LEGACY_REPOS, ORG, WORKSPACE, workspace, ws_dir

CONTROL = DATA / "control.db"

CONTROL_SCHEMA = """
-- token: the user's GitHub user-to-server token, used to check they can read a repository before it is imported
CREATE TABLE IF NOT EXISTS users(
  login TEXT PRIMARY KEY COLLATE NOCASE, name TEXT, avatar_url TEXT, token TEXT, refresh_token TEXT,
  token_expires REAL, created_at TEXT, last_login TEXT);
CREATE TABLE IF NOT EXISTS workspaces(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, mcp_token TEXT UNIQUE, created_by TEXT, created_at TEXT);
-- role = owner | admin | member; owners and admins manage members, repositories and settings
CREATE TABLE IF NOT EXISTS members(
  workspace_id INT, login TEXT COLLATE NOCASE, role TEXT, joined_at TEXT, PRIMARY KEY(workspace_id, login));
-- the link's token is only ever stored hashed; one use, expires after INVITE_DAYS
CREATE TABLE IF NOT EXISTS invites(
  id INTEGER PRIMARY KEY, workspace_id INT, email TEXT, role TEXT, token_hash TEXT UNIQUE, invited_by TEXT,
  created_at TEXT, accepted_by TEXT, accepted_at TEXT);
-- repositories each workspace imported; webhooks for a repository go to every workspace listed here
CREATE TABLE IF NOT EXISTS repos(
  workspace_id INT, repo TEXT COLLATE NOCASE, added_by TEXT, added_at TEXT, PRIMARY KEY(workspace_id, repo));
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
-- git author email -> GitHub login, learned from the API, webhooks and noreply addresses
CREATE TABLE IF NOT EXISTS identities(email TEXT PRIMARY KEY, login TEXT, name TEXT);
-- person = GitHub login when known, else the git author email
CREATE TABLE IF NOT EXISTS commits(
  repo TEXT, sha TEXT, person TEXT, author_name TEXT, author_email TEXT, committed_at TEXT,
  message TEXT, additions INT, deletions INT, files INT, is_merge INT, on_default INT DEFAULT 0,
  PRIMARY KEY(repo, sha));
CREATE INDEX IF NOT EXISTS commits_person ON commits(person, committed_at);
-- pusher is the GitHub login of whoever pushed (webhook or Events API)
CREATE TABLE IF NOT EXISTS pushes(
  id INTEGER PRIMARY KEY, repo TEXT, branch TEXT, before TEXT, after TEXT, pusher TEXT,
  pushed_at TEXT, forced INT, commits INT, source TEXT, UNIQUE(repo, branch, before, after));
CREATE TABLE IF NOT EXISTS push_commits(push_id INT, sha TEXT, PRIMARY KEY(push_id, sha));
-- pull requests from GitHub (every sync and every pull_request webhook); state = open|closed|merged
CREATE TABLE IF NOT EXISTS pulls(
  repo TEXT, number INT, title TEXT, author TEXT, state TEXT, draft INT, base TEXT, head TEXT, head_sha TEXT,
  created_at TEXT, updated_at TEXT, merged_at TEXT, url TEXT, PRIMARY KEY(repo, number));
-- latest completed CI check run per commit and check name
CREATE TABLE IF NOT EXISTS checks(
  repo TEXT, sha TEXT, name TEXT, conclusion TEXT, completed_at TEXT, url TEXT, PRIMARY KEY(repo, sha, name));
-- a check that passed on the previous default-branch commit and failed on this one
CREATE TABLE IF NOT EXISTS breakages(
  id INTEGER PRIMARY KEY, repo TEXT, branch TEXT, check_name TEXT, broken_sha TEXT, last_good_sha TEXT,
  person TEXT, pusher TEXT, broken_at TEXT, fixed_sha TEXT, fixed_at TEXT,
  UNIQUE(repo, branch, check_name, broken_sha));
-- pr = 0 for a direct push to a default branch; branch = '' for a PR
CREATE TABLE IF NOT EXISTS reviews(
  id INTEGER PRIMARY KEY, repo TEXT, pr INT NOT NULL DEFAULT 0, branch TEXT NOT NULL DEFAULT '', title TEXT,
  author TEXT, base_sha TEXT, head_sha TEXT, model TEXT, summary TEXT, status TEXT, posted_url TEXT,
  created_at TEXT, UNIQUE(repo, pr, branch, head_sha));
-- person = author of the flagged line, from git blame
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY, review_id INT, path TEXT, line INT, severity TEXT, category TEXT,
  title TEXT, body TEXT, sha TEXT, person TEXT);
-- open SonarQube issues per SCM author, one snapshot a day; quality = reliability|security|maintainability
CREATE TABLE IF NOT EXISTS sonar_issues(
  taken_on TEXT, repo TEXT, author_email TEXT, quality TEXT, count INT,
  PRIMARY KEY(taken_on, repo, author_email, quality));
-- documents the portal writes for each repository (docgen.py); inputs = fingerprint of what it was written from
CREATE TABLE IF NOT EXISTS docs(
  repo TEXT, slug TEXT, kind TEXT, title TEXT, summary TEXT, content TEXT, inputs TEXT, head_sha TEXT, model TEXT,
  generated_at TEXT, status TEXT, error TEXT, PRIMARY KEY(repo, slug));
CREATE TABLE IF NOT EXISTS doc_versions(
  id INTEGER PRIMARY KEY, repo TEXT, slug TEXT, head_sha TEXT, content TEXT, generated_at TEXT, model TEXT);
CREATE INDEX IF NOT EXISTS doc_versions_slug ON doc_versions(repo, slug, id);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(repo UNINDEXED, slug UNINDEXED, title, content);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT, kind TEXT, detail TEXT, ok INT);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(readonly=False, path=None):
    path = path or ws_dir() / "portal.db"
    if readonly:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30, check_same_thread=False)
        con.execute("PRAGMA query_only = ON")
    else:
        con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init():
    """Control database, an existing single-tenant install as workspace 1, and every workspace's schema."""
    DATA.mkdir(parents=True, exist_ok=True)
    with closing(connect(path=CONTROL)) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(CONTROL_SCHEMA)
    migrate_legacy()
    for w in cq("SELECT id FROM workspaces"):
        with workspace(w["id"]):
            init_workspace()


def init_workspace():
    ws_dir().mkdir(parents=True, exist_ok=True)
    with closing(connect()) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)


def q(sql, *args, path=None):
    with closing(connect(path=path)) as con:
        return [dict(r) for r in con.execute(sql, args)]


def x(sql, *args, path=None):
    with closing(connect(path=path)) as con, con:
        return con.execute(sql, args).lastrowid


def xs(sql, rows):
    with closing(connect()) as con, con:
        con.executemany(sql, rows)


def cq(sql, *args):  # the control database
    return q(sql, *args, path=CONTROL)


def cx(sql, *args):
    return x(sql, *args, path=CONTROL)


def setting(key, default=None):
    r = q("SELECT value FROM settings WHERE key=?", key)
    return json.loads(r[0]["value"]) if r else default


def set_setting(key, value):
    x("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      key, json.dumps(value))


def schema_text():
    return "\n".join(r["sql"] for r in q("SELECT sql FROM sqlite_master WHERE type='table' AND name != 'settings' "
                                          "AND name NOT LIKE 'docs_fts%' AND sql IS NOT NULL"))


# ---------- tenants ----------

def repos():
    """The current workspace's repositories, oldest import first."""
    return [r["repo"] for r in cq("SELECT repo FROM repos WHERE workspace_id=? ORDER BY added_at, rowid", WORKSPACE.get())]


def create_workspace(name, owner):
    ws = cx("INSERT INTO workspaces(name, mcp_token, created_by, created_at) VALUES(?,?,?,?)",
            name, secrets.token_urlsafe(24), owner, now())
    cx("INSERT INTO members(workspace_id, login, role, joined_at) VALUES(?,?,?,?)", ws, owner, "owner", now())
    with workspace(ws):
        init_workspace()
    return ws


def add_repo(ws, repo, by):
    cx("INSERT OR IGNORE INTO repos(workspace_id, repo, added_by, added_at) VALUES(?,?,?,?)", ws, repo, by, now())


def migrate_legacy():
    """Before workspaces, everything lived straight in data/: that install becomes workspace 1, named after
    GITHUB_ORG, with its repositories, its architecture views and its MCP token. ADMINS (and DEV_USER) own it."""
    if not (DATA / "portal.db").exists():
        return
    old = cq("SELECT id FROM workspaces WHERE created_by IS NULL ORDER BY id LIMIT 1")  # an interrupted migration
    ws = old[0]["id"] if old else cx("INSERT INTO workspaces(name, mcp_token, created_at) VALUES(?,?,?)",
                                     ORG, os.environ.get("MCP_TOKEN") or secrets.token_urlsafe(24), now())
    dest = ws_dir(ws)
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("repos", "site", "work", "venvs", "portal.db-wal", "portal.db-shm", "portal.db"):  # the db last
        if (DATA / name).exists() and not (dest / name).exists():
            shutil.move(DATA / name, dest / name)
    with workspace(ws):
        init_workspace()
        for repo in LEGACY_REPOS + [r for r in setting("extra_repos", []) if r not in LEGACY_REPOS]:
            add_repo(ws, repo, None)
        set_setting("views", {"backend": BACKEND_REPO, "frontend": FRONTEND_REPO})
    for login in ADMINS | {os.environ.get("DEV_USER", "")} - {""}:
        cx("INSERT OR IGNORE INTO members(workspace_id, login, role, joined_at) VALUES(?,?,?,?)", ws, login, "owner", now())
    print(f"[migrate] the single-tenant data is now workspace {ws} ({ORG}), owned by {sorted(ADMINS) or 'nobody yet'}",
          flush=True)
