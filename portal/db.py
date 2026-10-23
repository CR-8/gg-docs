"""MongoDB storage (MONGODB_URI): one database for every workspace.

Control collections (users, github_accounts, ggops_connections, workspaces, members, invites, repos, api_clients,
access_tokens, login_handoffs, audit_events, rate_limits, counters, migrations) are global. Every other collection is tenant data:
each document carries workspace_id, and code reaches it only through Tenant, which adds the current WORKSPACE to every
filter, write and pipeline. Outside a workspace WORKSPACE.get() raises, so nothing can read across tenants by accident.

Identifiers the API and URLs already use stay as they were in SQLite: workspaces are integers (_id), and reviews,
findings, pushes, breakages, doc versions, events and invites keep an integer `id` (per workspace, from `counters`).
Timestamps of synced data stay ISO-8601 strings, as GitHub and git give them, so API output and string comparisons
are unchanged; expiry fields of the new auth collections are BSON dates, for their TTL indexes.

No transactions: the default deployment is a standalone mongod. Multi-document changes are ordered so a failure part
way leaves a state the next request or job repairs (see accept_invite, sign_in).
"""
import os
import re
import secrets
from datetime import datetime, timezone

from pymongo import DESCENDING, MongoClient, ReturnDocument, UpdateOne
from pymongo.errors import DuplicateKeyError

from . import DATA, WORKSPACE, secret, ws_dir

URI = os.environ.get("MONGODB_URI") or "mongodb://127.0.0.1:27017/gg-docs"
if not URI.startswith(("mongodb://", "mongodb+srv://")):
    raise RuntimeError("MONGODB_URI must start with mongodb:// or mongodb+srv://")
client = MongoClient(URI, serverSelectionTimeoutMS=int(os.environ.get("MONGODB_TIMEOUT_MS") or 10000),
                     tz_aware=True, connect=False, appname="gg-docs")
mongo = client.get_default_database("gg-docs")

SECRET_SETTINGS = ("llm_api_key",)
LEGACY_SQLITE = ("control.db", "portal.db")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utcnow():
    return datetime.now(timezone.utc)


class Tenant:
    """A collection seen from inside the current workspace."""

    def __init__(self, name):
        self.name, self.c = name, mongo[name]

    def scope(self, f=None):
        return {**(f or {}), "workspace_id": WORKSPACE.get()}

    def find(self, f=None, sort=None, limit=0, fields=None, skip=0):
        proj = {"_id": 0, "workspace_id": 0} if fields is None else {"_id": 0, **{k: 1 for k in fields}}
        return list(self.c.find(self.scope(f), proj, sort=sort, limit=limit, skip=skip))

    def one(self, f=None, sort=None, fields=None):
        rows = self.find(f, sort, 1, fields)
        return rows[0] if rows else None

    def count(self, f=None):
        return self.c.count_documents(self.scope(f))

    def insert(self, doc, with_id=False):
        """Returns the new integer id when with_id."""
        doc = {**doc, "workspace_id": WORKSPACE.get()}
        if with_id:
            doc["id"] = next_id(self.name)
        self.c.insert_one(doc)
        return doc.get("id")

    def insert_many(self, docs, with_id=False):
        if not docs:
            return
        last = next_id(self.name, len(docs)) if with_id else 0
        self.c.insert_many([{**d, "workspace_id": WORKSPACE.get(), **({"id": last - len(docs) + 1 + i} if with_id else {})}
                            for i, d in enumerate(docs)], ordered=False)

    def update(self, f, changes):
        return self.c.update_many(self.scope(f), changes).modified_count

    def upsert(self, f, fields, on_insert=None):
        self.c.update_one(self.scope(f), {"$set": fields, **({"$setOnInsert": on_insert} if on_insert else {})},
                          upsert=True)

    def upsert_many(self, rows, key, only_new=False):
        """rows: dicts that include the `key` fields. only_new: insert-or-ignore, existing documents stay as they are."""
        ops = [UpdateOne(self.scope({k: r[k] for k in key}), {"$setOnInsert" if only_new else "$set": r}, upsert=True)
               for r in rows]
        if ops:
            self.c.bulk_write(ops, ordered=False)

    def delete(self, f):
        return self.c.delete_many(self.scope(f)).deleted_count

    def aggregate(self, pipeline, **kw):
        return list(self.c.aggregate([{"$match": {"workspace_id": WORKSPACE.get()}}, *pipeline], **kw))


# Tenant data. The chatbot's query tool may read QUERYABLE (settings, integrations and workflows hold configuration).
QUERYABLE = ("commits", "identities", "pushes", "pulls", "checks", "breakages", "reviews", "findings", "docs",
             "doc_versions", "events")
(commits, identities, pushes, pulls, checks, breakages, reviews, findings, docs, doc_versions, events) = map(Tenant, QUERYABLE)
settings, integrations, workflows = Tenant("settings"), Tenant("integrations"), Tenant("workflows")
TENANT = QUERYABLE + ("settings", "integrations", "workflows")

# Control data
(users, github_accounts, ggops_connections, workspaces, members, invites, repo_links, api_clients, access_tokens,
 login_handoffs, audit_events, rate_limits, counters, migrations) = (mongo[n] for n in (
    "users", "github_accounts", "ggops_connections", "workspaces", "members", "invites", "repos", "api_clients",
    "access_tokens", "login_handoffs", "audit_events", "rate_limits", "counters", "migrations"))

W = "workspace_id"
UNIQUE = {"unique": True}
INDEXES = {
    "users": [([("github_id", 1)], {"unique": True, "partialFilterExpression": {"github_id": {"$gt": 0}}}),
              ([("login_lower", 1)], {})],
    "github_accounts": [([("user_id", 1)], UNIQUE), ([("github_id", 1)], UNIQUE)],
    "ggops_connections": [([("user_id", 1)], UNIQUE)],
    "workspaces": [([("mcp_token_hash", 1)], {"unique": True,
                                               "partialFilterExpression": {"mcp_token_hash": {"$type": "string"}}})],
    # bound memberships are unique per user; pending ones (made before the person first signed in) per login
    "members": [([(W, 1), ("user_id", 1)], {"unique": True, "partialFilterExpression": {"user_id": {"$type": "objectId"}}}),
                ([(W, 1), ("login_lower", 1)], {"unique": True, "partialFilterExpression": {"pending": True}}),
                ([("user_id", 1)], {}), ([("login_lower", 1), ("pending", 1)], {})],
    "invites": [([("token_hash", 1)], UNIQUE), ([("id", 1)], UNIQUE), ([(W, 1), ("created_at", 1)], {})],
    "repos": [([(W, 1), ("repo_lower", 1)], UNIQUE), ([("repo_lower", 1)], {})],
    "api_clients": [([("client_id", 1)], UNIQUE), ([(W, 1)], {})],
    "access_tokens": [([("token_hash", 1)], UNIQUE), ([("client_id", 1)], {}), ([("expires_at", 1)], {"expireAfterSeconds": 0})],
    "login_handoffs": [([("code_hash", 1)], UNIQUE), ([("expires_at", 1)], {"expireAfterSeconds": 3600})],
    "audit_events": [([(W, 1), ("at", DESCENDING)], {}), ([("at", 1)], {"expireAfterSeconds": 730 * 86400})],
    "rate_limits": [([("expires_at", 1)], {"expireAfterSeconds": 0})],
    "settings": [([(W, 1), ("key", 1)], UNIQUE)],
    "identities": [([(W, 1), ("email", 1)], UNIQUE)],
    "commits": [([(W, 1), ("repo", 1), ("sha", 1)], UNIQUE), ([(W, 1), ("person", 1), ("committed_at", 1)], {}),
                ([(W, 1), ("committed_at", 1)], {})],
    "pushes": [([(W, 1), ("id", 1)], UNIQUE), ([(W, 1), ("repo", 1), ("branch", 1), ("before", 1), ("after", 1)], UNIQUE),
               ([(W, 1), ("pushed_at", 1)], {}), ([(W, 1), ("repo", 1), ("commit_shas", 1)], {}),
               ([(W, 1), ("repo", 1), ("after", 1)], {})],
    "pulls": [([(W, 1), ("repo", 1), ("number", 1)], UNIQUE), ([(W, 1), ("updated_at", 1)], {})],
    "checks": [([(W, 1), ("repo", 1), ("sha", 1), ("name", 1)], UNIQUE)],
    "breakages": [([(W, 1), ("id", 1)], UNIQUE),
                  ([(W, 1), ("repo", 1), ("branch", 1), ("check_name", 1), ("broken_sha", 1)], UNIQUE),
                  ([(W, 1), ("broken_at", 1)], {}), ([(W, 1), ("person", 1)], {})],
    "reviews": [([(W, 1), ("id", 1)], UNIQUE), ([(W, 1), ("repo", 1), ("pr", 1), ("branch", 1), ("head_sha", 1)], UNIQUE),
                ([(W, 1), ("repo", 1), ("head_sha", 1)], {})],
    "findings": [([(W, 1), ("id", 1)], UNIQUE), ([(W, 1), ("review_id", 1)], {}), ([(W, 1), ("person", 1)], {})],
    "docs": [([(W, 1), ("repo", 1), ("slug", 1)], UNIQUE)],
    "doc_versions": [([(W, 1), ("id", 1)], UNIQUE), ([(W, 1), ("repo", 1), ("slug", 1), ("id", 1)], {})],
    "events": [([(W, 1), ("id", 1)], UNIQUE)],
    "integrations": [([(W, 1), ("service", 1)], UNIQUE)],
    "workflows": [([(W, 1), ("workflow_id", 1)], UNIQUE)],
}

SCHEMA = """Every collection is this workspace's only; timestamps are ISO-8601 strings, compare them as strings.
commits {repo, sha, person, author_name, author_email, committed_at, message, additions, deletions, files, is_merge 0|1, on_default 0|1}
identities {email, login, name}
pushes {id, repo, branch, before, after, pusher, pushed_at, forced 0|1, commits, source, commit_shas[] (the pushed commits, at most 250)}
pulls {repo, number, title, author, state open|closed|merged, draft 0|1, base, head, head_sha, created_at, updated_at, merged_at, url}
checks {repo, sha, name, conclusion, completed_at, url}
breakages {id, repo, branch, check_name, broken_sha, last_good_sha, person, pusher, broken_at, fixed_sha (null while open), fixed_at}
reviews {id, repo, pr (0 = direct push), branch ('' for a PR), title, author, base_sha, head_sha, model, summary, status, posted_url, created_at}
findings {id, review_id (-> reviews.id), path, line, severity critical|major|minor|nit, category bug|security|perf|test-gap|slop, title, body, sha, person}
docs {repo, slug, kind, title, summary, content, head_sha, model, generated_at, status, error}
doc_versions {id, repo, slug, head_sha, content, generated_at, model}
events {id, at, kind, detail, ok 0|1}"""


def schema_text():
    return SCHEMA


def next_id(name, n=1):
    """The last of n new integer ids. Workspaces and invites count globally (as control.db did), the rest per workspace."""
    key = name if name in ("workspaces", "invites") else f"{name}:{WORKSPACE.get()}"
    return counters.find_one_and_update({"_id": key}, {"$inc": {"seq": n}}, upsert=True,
                                        return_document=ReturnDocument.AFTER)["seq"]


def legacy_sqlite():
    return [DATA / n for n in LEGACY_SQLITE if (DATA / n).exists()]


def init():
    """Reach MongoDB (or fail: the portal never runs on an empty database by accident) and create the indexes.
    SQLite data that was never migrated stops the start too: `python -m portal.migrate` first (README 6.1)."""
    client.admin.command("ping")
    if legacy_sqlite() and not migrations.find_one({"_id": "sqlite", "status": "applied"}):
        raise RuntimeError(f"{', '.join(map(str, legacy_sqlite()))}: SQLite data that is not in MongoDB yet. Run "
                           "`python -m portal.migrate` (a dry run), then `python -m portal.migrate --apply`.")
    # GG-Ops keeps `users` and `workspaces` too, with unique indexes (email, slug) that the portal's documents break
    if {"workspacemembers", "oauthgrants"} & set(mongo.list_collection_names()):
        raise RuntimeError(f"MONGODB_URI names GG-Ops's database ({mongo.name}). Give the portal its own database on "
                           "the same cluster, e.g. mongodb+srv://user:pass@cluster/gg-docs.")
    DATA.mkdir(parents=True, exist_ok=True)
    for name, specs in INDEXES.items():
        for keys, opts in specs:
            mongo[name].create_index(keys, **opts)


def ping():
    try:
        client.admin.command("ping")
        return True
    except Exception:
        return False


# ---------- settings ----------

def setting(key, default=None):
    r = settings.one({"key": key})
    if not r:
        return default
    return secret.unseal(r["value"]) if key in SECRET_SETTINGS else r["value"]


def set_setting(key, value):
    settings.upsert({"key": key}, {"value": secret.seal(value) if key in SECRET_SETTINGS else value})


def del_setting(key):
    settings.delete({"key": key})


# ---------- people ----------

def user_view(u):
    return {"id": u["_id"], "login": u["login"], "github_id": u.get("github_id"), "name": u.get("name"),
            "avatar_url": u.get("avatar_url"), "session_version": u.get("session_version", 0)}


def member_doc(ws, user, role):
    return {W: ws, "user_id": user["id"], "login": user["login"], "login_lower": user["login"].lower(),
            "pending": False, "role": role, "joined_at": now()}


def claim_pending(user):
    """Memberships made before someone's first sign-in (ADMINS, the SQLite era) name a GitHub login, which is how the
    portal identified people then. The account holding that login at its first sign-in claims them; from then on they
    follow the immutable user id, so renaming the GitHub account changes nothing."""
    for m in members.find({"pending": True, "login_lower": user["login"].lower()}):
        try:
            members.update_one({"_id": m["_id"], "pending": True}, {"$set": {
                "user_id": user["id"], "pending": False, "login": user["login"]}, "$unset": {"name": "", "avatar_url": ""}})
        except DuplicateKeyError:  # already a member through an invite
            members.delete_one({"_id": m["_id"]})


def sign_in(profile):
    """Create or update the user keyed by GitHub's numeric id (never by the mutable login)."""
    fields = {"login": profile["login"], "login_lower": profile["login"].lower(), "name": profile.get("name"),
              "avatar_url": profile.get("avatar_url"), "last_login": now()}
    for attempt in (1, 2):
        try:
            u = users.find_one_and_update({"github_id": profile["github_id"]},
                                          {"$set": fields, "$setOnInsert": {"created_at": now(), "session_version": 0}},
                                          upsert=True, return_document=ReturnDocument.AFTER)
            break
        except DuplicateKeyError:  # two first sign-ins at once: the other one created it
            if attempt == 2:
                raise
    user = user_view(u)
    members.update_many({"user_id": u["_id"]}, {"$set": {"login": u["login"], "login_lower": u["login_lower"]}})
    claim_pending(user)
    return user


def dev_user(login):
    """DEV_USER (local development without GitHub sign-in): a user without a GitHub id."""
    u = users.find_one_and_update({"dev": True, "login_lower": login.lower()},
                                  {"$setOnInsert": {"login": login, "login_lower": login.lower(), "dev": True,
                                                    "created_at": now(), "session_version": 0}},
                                  upsert=True, return_document=ReturnDocument.AFTER)
    user = user_view(u)
    claim_pending(user)
    return user


def save_github_tokens(user, t):
    github_accounts.update_one({"user_id": user["id"]}, {"$set": {
        "github_id": user["github_id"], "token": secret.seal(t["token"]), "refresh_token": secret.seal(t.get("refresh_token")),
        "token_expires": t.get("token_expires"), "updated_at": utcnow()}}, upsert=True)


def memberships(user_id):
    rows = list(members.find({"user_id": user_id}, {"_id": 0, W: 1, "role": 1}))
    names = {w["_id"]: w["name"] for w in workspaces.find({"_id": {"$in": [m[W] for m in rows]}}, {"name": 1})}
    return sorted(({"id": m[W], "name": names[m[W]], "role": m["role"]} for m in rows if m[W] in names),
                  key=lambda w: (w["name"], w["id"]))


# ---------- workspaces and their repositories ----------

def mcp_fields(token):
    return {"mcp_token": secret.seal(token), "mcp_token_hash": secret.digest(token), "mcp_legacy": True}


def create_workspace(name, user):
    ws = next_id("workspaces")
    workspaces.insert_one({"_id": ws, "name": name, **mcp_fields(secrets.token_urlsafe(24)),
                           "created_by": user["login"], "created_at": now()})
    members.insert_one(member_doc(ws, user, "owner"))
    ws_dir(ws).mkdir(parents=True, exist_ok=True)
    return ws


def repos():
    """The current workspace's repositories, oldest import first."""
    return [r["repo"] for r in repo_links.find({W: WORKSPACE.get()}, {"repo": 1}, sort=[("added_at", 1), ("_id", 1)])]


def add_repo(ws, repo, by, meta=None):
    """Insert-or-ignore the connection; meta (GitHub's metadata and access status) is refreshed every time."""
    repo_links.update_one({W: ws, "repo_lower": repo.lower()},
                          {"$setOnInsert": {"repo": repo, "added_by": by, "added_at": now()},
                           **({"$set": meta} if meta else {})}, upsert=True)


def update_repo(ws, repo, meta):
    """Refreshes a connection's metadata and status; never re-creates one that was disconnected meanwhile."""
    repo_links.update_one({W: ws, "repo_lower": repo.lower()}, {"$set": meta})


def remove_repo(repo):
    """Stops syncing; history, docs and the clone stay (importing it again picks them back up)."""
    repo_links.delete_one({W: WORKSPACE.get(), "repo_lower": repo.lower()})
    v = setting("views")
    if v and repo in v.values():
        del_setting("views")


def audit(action, actor=None, workspace=None, **meta):
    """Security-relevant outcomes (never secrets): sign-in, credentials issued and revoked, connections changed."""
    if workspace is None:
        workspace = WORKSPACE.get(None)
    audit_events.insert_one({W: workspace, "actor": actor, "action": action, "meta": meta, "at": utcnow()})


def regex_word(term):
    return {"$regex": r"\b" + re.escape(term), "$options": "i"}
