"""GeneGenius portal: git-synced architecture views, CI breakage tracking, PR review bot and chatbot.

All configuration is environment variables (see .env.example); LLM keys and the Sonar token are set
by admins in the Settings page and stored in the portal database.
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data")).resolve()  # absolute: tools run in other cwds
REPOS_DIR = DATA / "repos"
SITE = Path(os.environ.get("SITE_DIR", DATA / "site")).resolve()
WORK = Path(os.environ.get("WORK_DIR", DATA / "work")).resolve()
DB_PATH = DATA / "portal.db"

ORG = os.environ.get("GITHUB_ORG", "GeneGeniusinc")
BACKEND_REPO = os.environ.get("BACKEND_REPO", f"{ORG}/Genegenius-backend")
FRONTEND_REPO = os.environ.get("FRONTEND_REPO", f"{ORG}/Genegenius-phase-1")
# More repositories (synced, reviewed and documented, without the architecture views): EXTRA_REPOS or Settings.
REPOS = [BACKEND_REPO, FRONTEND_REPO] + [r.strip() for r in os.environ.get("EXTRA_REPOS", "").split(",") if r.strip()]
# Optional {"owner/name": "branch"} override; otherwise GitHub's default branch (or origin/HEAD offline).
DEFAULT_BRANCHES = json.loads(os.environ.get("DEFAULT_BRANCHES", "{}"))
# Where to clone from. Local development can point this at existing checkouts, e.g. "D:/GeneGenius/{name}".
GIT_URL = os.environ.get("GIT_URL_TEMPLATE", "https://github.com/{repo}.git")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:7000").rstrip("/")
ADMINS = {a.strip().lower() for a in os.environ.get("ADMINS", "").split(",") if a.strip()}
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "90"))
SONAR_URL = os.environ.get("SONAR_URL", "http://localhost:7010").rstrip("/")  # how the portal reaches Sonar
SONAR_PUBLIC_URL = os.environ.get("SONAR_PUBLIC_URL", "").rstrip("/")          # the link people click
SONAR_KEYS = {BACKEND_REPO: "genegenius-backend", FRONTEND_REPO: "genegenius-frontend"}


def repo_dir(repo):
    return REPOS_DIR / repo.split("/")[1]
