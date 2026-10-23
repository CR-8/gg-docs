"""GeneGenius portal: git-synced architecture views, CI breakage tracking, PR review bot and chatbot.

Multi-tenant: every workspace has its own SQLite database, clones and generated files under
data/workspaces/<id>/. Users, workspaces, members, invites and which repositories each workspace imported live in
data/control.db. WORKSPACE says which tenant the current request or job is for; every per-tenant path and query
reads it, so code that runs outside a tenant fails loudly instead of touching someone else's data.

All configuration is environment variables (see .env.example); LLM keys and the Sonar token are set
by workspace admins in the Settings page and stored in that workspace's database.
"""
import contextvars
import json
import os
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()  # absolute: tools run in other cwds

# Single-tenant settings, read once to turn an existing install into its first workspace (db.migrate_legacy).
ORG = os.environ.get("GITHUB_ORG", "GeneGeniusinc")
BACKEND_REPO = os.environ.get("BACKEND_REPO", f"{ORG}/Genegenius-backend")
FRONTEND_REPO = os.environ.get("FRONTEND_REPO", f"{ORG}/Genegenius-phase-1")
LEGACY_REPOS = [BACKEND_REPO, FRONTEND_REPO] + [r.strip() for r in os.environ.get("EXTRA_REPOS", "").split(",") if r.strip()]
ADMINS = {a.strip().lower() for a in os.environ.get("ADMINS", "").split(",") if a.strip()}  # owners of that workspace

# Optional {"owner/name": "branch"} override; otherwise GitHub's default branch (or origin/HEAD offline).
DEFAULT_BRANCHES = json.loads(os.environ.get("DEFAULT_BRANCHES", "{}"))
# Where to clone from. Local development can point this at existing checkouts, e.g. "D:/GeneGenius/{name}".
GIT_URL = os.environ.get("GIT_URL_TEMPLATE", "https://github.com/{repo}.git")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:7000").rstrip("/")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "90"))
SONAR_URL = os.environ.get("SONAR_URL", "http://localhost:7010").rstrip("/")  # how the portal reaches Sonar
SONAR_PUBLIC_URL = os.environ.get("SONAR_PUBLIC_URL", "").rstrip("/")          # the link people click
SONAR_KEYS = ("genegenius-backend", "genegenius-frontend")  # build.py's project keys, for the views pair

WORKSPACE = contextvars.ContextVar("workspace")  # id of the tenant; no default on purpose


@contextmanager
def workspace(ws):
    token = WORKSPACE.set(ws)
    try:
        yield
    finally:
        WORKSPACE.reset(token)


def ws_dir(ws=None):
    return DATA / "workspaces" / str(WORKSPACE.get() if ws is None else ws)


def repo_dir(repo):
    return ws_dir() / "repos" / repo.split("/")[1]


def site_dir():  # what build.py generates
    return ws_dir() / "site"


def work_dir():
    return ws_dir() / "work"
