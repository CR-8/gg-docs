"""The SQLite schemas of the release before MongoDB (portal/db.py at ac8a6de), for building legacy files to migrate."""

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
