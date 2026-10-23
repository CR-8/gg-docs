"""SQLite -> MongoDB, once, run by hand (README 6.1):

    python -m portal.migrate              dry run: read and convert everything, write nothing, report
    python -m portal.migrate --apply      write, then verify; a clean run lets the portal start (db.init)
    python -m portal.migrate --verify     only compare the SQLite files with what MongoDB has

It only ever reads the SQLite files of the previous release (opened read-only) and never deletes them:
  multi-tenant layout   DATA_DIR/control.db and DATA_DIR/workspaces/<id>/portal.db
  single-tenant layout  DATA_DIR/portal.db, which becomes a workspace named GITHUB_ORG owned by ADMINS (as before)

Writes are upserts by each table's natural key that only insert what is missing ($setOnInsert), in batches, so a
re-run changes nothing and never overwrites what the portal wrote since; progress per table is kept in `migrations`,
so an interrupted run resumes where it stopped (--restart reads everything again). Ids the API and URLs use (workspace,
review, finding, push, breakage, doc version, event and invite ids) are kept, and the id counters continue after them.

Not migrated, on purpose: docs_fts (an index, rebuilt from docs) and the users table's GitHub tokens (plaintext, 8-hour
tokens: everyone signs in with GitHub again). Members become pending memberships under their GitHub login, claimed by
that account at its first sign-in (db.claim_pending). LLM and Sonar keys are migrated encrypted.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from pymongo import UpdateOne

from . import ADMINS, BACKEND_REPO, DATA, FRONTEND_REPO, LEGACY_REPOS, ORG, db, secret

# table -> (collection, natural key); push_commits is folded into pushes.commit_shas, docs_fts is not data
TABLES = {
    "settings": ("settings", ("key",)), "identities": ("identities", ("email",)), "commits": ("commits", ("repo", "sha")),
    "pushes": ("pushes", ("repo", "branch", "before", "after")), "pulls": ("pulls", ("repo", "number")),
    "checks": ("checks", ("repo", "sha", "name")), "breakages": ("breakages", ("repo", "branch", "check_name", "broken_sha")),
    "reviews": ("reviews", ("repo", "pr", "branch", "head_sha")), "findings": ("findings", ("id",)),
    "sonar_issues": ("sonar_issues", ("taken_on", "repo", "author_email", "quality")), "docs": ("docs", ("repo", "slug")),
    "doc_versions": ("doc_versions", ("id",)), "events": ("events", ("id",)),
}
WITH_IDS = ("pushes", "breakages", "reviews", "findings", "doc_versions", "events")
DEFAULTS = {"commits": {"on_default": 0}, "reviews": {"pr": 0, "branch": ""}}
SAMPLE = 20


def open_ro(path):
    con = sqlite3.connect(f"file:{quote(Path(path).as_posix(), safe='/:')}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return closing(con)


def has_table(con, name):
    return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def convert(table, row, con=None, seal=True):
    """One SQLite row -> its MongoDB document (same field names), without workspace_id. seal=False (dry run) leaves
    secrets as they are, so a dry run writes nothing, not even the encryption key file."""
    doc = {**DEFAULTS.get(table, {}), **{k: row[k] for k in row.keys() if k != "_rowid"}}
    if table == "settings":
        value = json.loads(doc["value"])
        doc["value"] = secret.seal(value) if seal and doc["key"] in db.SECRET_SETTINGS else value
    if table == "pushes":
        doc["commit_shas"] = [] if con is None else \
            [r[0] for r in con.execute("SELECT sha FROM push_commits WHERE push_id=? ORDER BY rowid", (doc["id"],))]
    return doc


def same(table, src, dst):
    """Fields that differ between a converted SQLite row and the stored document."""
    diff = []
    for k, v in src.items():
        have = dst.get(k)
        if table == "settings" and k == "value" and src["key"] in db.SECRET_SETTINGS:
            v, have = secret.unseal(v), secret.unseal(have)
        if have != v:
            diff.append(k)
    return diff


class Run:
    def __init__(self, apply, batch, restart):
        self.apply, self.batch, self.restart = apply, batch, restart
        self.report = {"mode": "apply" if apply else "dry-run", "source": str(DATA), "workspaces": [], "control": {},
                       "problems": []}

    def problem(self, text):
        self.report["problems"].append(text)
        print(f"  !! {text}", flush=True)

    # ---- control data ----

    def upsert(self, collection, rows, key):
        """rows of (filter, document) -> (inserted, already there)."""
        if not (self.apply and rows):
            return (len(rows), 0) if not self.apply else (0, 0)
        r = db.mongo[collection].bulk_write([UpdateOne(f, {"$setOnInsert": d}, upsert=True) for f, d in rows], ordered=False)
        return r.upserted_count, r.matched_count

    def control(self, con):
        out = self.report["control"]
        ws_rows = [dict(r) for r in con.execute("SELECT * FROM workspaces ORDER BY id")]
        names = {u["login"].lower(): u for u in map(dict, con.execute("SELECT login, name, avatar_url FROM users"))}
        out["workspaces"] = self.upsert("workspaces", [({"_id": w["id"]}, {
            "name": w["name"], **(db.mcp_fields(w["mcp_token"] or os.urandom(18).hex()) if self.apply else {}),
            "created_by": w["created_by"],
            "created_at": w["created_at"]}) for w in ws_rows], "_id")
        out["members"] = self.upsert("members", [({"workspace_id": m["workspace_id"], "login_lower": m["login"].lower()}, {
            "login": m["login"], "role": m["role"], "joined_at": m["joined_at"], "pending": True, "user_id": None,
            "name": names.get(m["login"].lower(), {}).get("name"),
            "avatar_url": names.get(m["login"].lower(), {}).get("avatar_url")})
            for m in map(dict, con.execute("SELECT * FROM members"))], "login")
        out["invites"] = self.upsert("invites", [({"token_hash": i["token_hash"]}, i)
                                                 for i in map(dict, con.execute("SELECT * FROM invites"))], "token_hash")
        out["repos"] = self.upsert("repos", [({"workspace_id": r["workspace_id"], "repo_lower": r["repo"].lower()}, {
            "repo": r["repo"], "added_by": r["added_by"], "added_at": r["added_at"], "status": "unknown"})
            for r in map(dict, con.execute("SELECT * FROM repos ORDER BY rowid"))], "repo")
        out["users_not_migrated"] = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if self.apply:
            for name, table in (("workspaces", "workspaces"), ("invites", "invites")):
                top = con.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0
                db.counters.update_one({"_id": name}, {"$max": {"seq": top}}, upsert=True)
        for k, (new, old) in ((k, v) for k, v in out.items() if isinstance(v, tuple)):
            print(f"control  {k:<14} {'inserted' if self.apply else 'to insert'} {new:>7}   already there {old:>7}")
        return ws_rows

    def single_tenant(self):
        """The release before workspaces: DATA_DIR/portal.db and its folders become one workspace."""
        w = db.workspaces.find_one({"migrated_from": "single-tenant"})
        ws = w["_id"] if w else (db.next_id("workspaces") if self.apply else "(new)")
        with open_ro(DATA / "portal.db") as con:
            extra = json.loads((con.execute("SELECT value FROM settings WHERE key='extra_repos'").fetchone() or ["[]"])[0])
        if self.apply:
            db.workspaces.update_one({"_id": ws}, {"$setOnInsert": {
                "name": ORG, **db.mcp_fields(os.environ.get("MCP_TOKEN") or os.urandom(18).hex()), "created_by": None,
                "created_at": db.now(), "migrated_from": "single-tenant"}}, upsert=True)
            for login in ADMINS | {os.environ.get("DEV_USER", "")} - {""}:
                db.members.update_one({"workspace_id": ws, "login_lower": login}, {"$setOnInsert": {
                    "login": login, "role": "owner", "joined_at": db.now(), "pending": True, "user_id": None}}, upsert=True)
            for repo in LEGACY_REPOS + [r for r in extra if r not in LEGACY_REPOS]:
                db.add_repo(ws, repo, None, None)
            db.settings.c.update_one({"workspace_id": ws, "key": "views"}, {"$setOnInsert": {
                "value": {"backend": BACKEND_REPO, "frontend": FRONTEND_REPO}}}, upsert=True)
            dest = DATA / "workspaces" / str(ws)
            dest.mkdir(parents=True, exist_ok=True)
            for name in ("repos", "site", "work", "venvs"):  # clones and generated files; portal.db stays where it is
                if (DATA / name).exists() and not (dest / name).exists():
                    shutil.move(DATA / name, dest / name)
        print(f"single-tenant install -> workspace {ws} ({ORG}), owners {sorted(ADMINS) or 'none (set ADMINS)'}")
        return ws

    # ---- one workspace's tables ----

    def tenant(self, ws, path):
        stats = {"workspace": ws, "source": str(path), "tables": {}}
        self.report["workspaces"].append(stats)
        with open_ro(path) as con:
            for table, (collection, key) in TABLES.items():
                if not has_table(con, table):
                    continue
                stats["tables"][table] = s = self.copy(ws, con, table, collection, key)
                print(f"  {table:<14} read {s['read']:>7}   {'inserted' if self.apply else 'to insert'} "
                      f"{s['inserted']:>7}   already there {s['existing']:>7}", flush=True)

    def copy(self, ws, con, table, collection, key):
        mark = f"sqlite:{ws}:{table}"
        done = None if self.restart or not self.apply else db.migrations.find_one({"_id": mark})
        s = {k: (done or {}).get(k, 0) for k in ("read", "inserted", "existing")}
        last, top = (done or {}).get("last_rowid", 0), 0
        while True:
            rows = con.execute(f"SELECT rowid AS _rowid, * FROM {table} WHERE rowid > ? ORDER BY rowid LIMIT ?",
                               (last, self.batch)).fetchall()
            if not rows:
                break
            docs = [convert(table, r, con, self.apply) for r in rows]
            last, s["read"] = rows[-1]["_rowid"], s["read"] + len(rows)
            if table in WITH_IDS:
                top = max([top] + [d["id"] for d in docs])
            if self.apply:
                r = db.mongo[collection].bulk_write([UpdateOne({"workspace_id": ws, **{k: d[k] for k in key}},
                                                               {"$setOnInsert": d}, upsert=True) for d in docs], ordered=False)
                s["inserted"] += r.upserted_count
                s["existing"] += r.matched_count
                db.migrations.update_one({"_id": mark}, {"$set": {**s, "last_rowid": last}}, upsert=True)
            else:
                s["inserted"] += len(docs)
        if self.apply and table in WITH_IDS:
            top = max(top, con.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] or 0)
            db.counters.update_one({"_id": f"{collection}:{ws}"}, {"$max": {"seq": top}}, upsert=True)
        return s

    # ---- verification ----

    def verify(self, ws, path):
        out = {"workspace": ws, "tables": {}, "ok": True}
        with open_ro(path) as con:
            for table, (collection, key) in TABLES.items():
                if not has_table(con, table):
                    continue
                src = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                dst = db.mongo[collection].count_documents({"workspace_id": ws})
                diffs = []
                for row in con.execute(f"SELECT * FROM {table} ORDER BY RANDOM() LIMIT {SAMPLE}"):
                    doc = convert(table, row, con)
                    have = db.mongo[collection].find_one({"workspace_id": ws, **{k: doc[k] for k in key}})
                    if not have:
                        diffs.append(f"missing {[doc[k] for k in key]}")
                    elif d := same(table, doc, have):
                        diffs.append(f"{[doc[k] for k in key]} differs in {d}")
                state = "missing" if dst < src or any(x.startswith("missing") for x in diffs) else \
                    "differs" if diffs else "more in MongoDB (written since)" if dst > src else "match"
                out["tables"][table] = {"sqlite": src, "mongodb": dst, "state": state, "sample": diffs[:5]}
                out["ok"] &= state in ("match", "more in MongoDB (written since)")
                print(f"  {table:<14} sqlite {src:>7}   mongodb {dst:>7}   {state}" + (f"  {diffs[:2]}" if diffs else ""))
            if has_table(con, "push_commits"):
                src = con.execute("SELECT COUNT(*) FROM push_commits").fetchone()[0]
                dst = next(db.pushes.c.aggregate([{"$match": {"workspace_id": ws}}, {"$group": {
                    "_id": None, "n": {"$sum": {"$size": {"$ifNull": ["$commit_shas", []]}}}}}]), {"n": 0})["n"]
                out["tables"]["push_commits"] = {"sqlite": src, "mongodb (pushes.commit_shas)": dst}
                out["ok"] &= dst >= src
                print(f"  {'push_commits':<14} sqlite {src:>7}   pushes.commit_shas {dst:>7}")
            orphans = set(db.findings.c.distinct("review_id", {"workspace_id": ws})) - \
                set(db.reviews.c.distinct("id", {"workspace_id": ws}))
            src_orphans = con.execute("SELECT COUNT(DISTINCT review_id) FROM findings WHERE review_id NOT IN "
                                      "(SELECT id FROM reviews)").fetchone()[0] if has_table(con, "findings") else 0
            out["finding_reviews_missing"] = len(orphans)
            if len(orphans) > src_orphans:
                out["ok"] = False
                self.problem(f"workspace {ws}: findings point at {len(orphans)} reviews that are not there")
        self.report.setdefault("verification", []).append(out)
        return out["ok"]


def sources():
    """[(workspace id or None for single-tenant, sqlite path)] and the control database, if any."""
    control = DATA / "control.db"
    if control.exists():
        with open_ro(control) as con:
            ids = [r[0] for r in con.execute("SELECT id FROM workspaces ORDER BY id")]
            legacy = con.execute("SELECT id FROM workspaces WHERE created_by IS NULL ORDER BY id LIMIT 1").fetchone()
        found = []
        for ws in ids:
            own = DATA / "workspaces" / str(ws) / "portal.db"
            if own.exists():
                found.append((ws, own))
            elif legacy and ws == legacy[0] and (DATA / "portal.db").exists():  # its move was interrupted
                found.append((ws, DATA / "portal.db"))
        return control, found
    return None, ([(None, DATA / "portal.db")] if (DATA / "portal.db").exists() else [])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m portal.migrate", description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write to MongoDB (default: dry run)")
    ap.add_argument("--verify", action="store_true", help="only verify what is already in MongoDB")
    ap.add_argument("--batch", type=int, default=500, help="rows per write (default 500)")
    ap.add_argument("--restart", action="store_true", help="ignore saved progress and read every table again")
    args = ap.parse_args(argv)
    db.client.admin.command("ping")
    for name, specs in db.INDEXES.items():  # the unique indexes are what make the upserts idempotent
        for keys, opts in specs:
            db.mongo[name].create_index(keys, **opts)
    run = Run(args.apply and not args.verify, max(1, args.batch), args.restart)
    control, found = sources()
    if not found and not control:
        print(f"No SQLite data in {DATA}: nothing to migrate.")
        return 0
    print(("Verifying" if args.verify else "Migrating" if run.apply else "Dry run (nothing is written)")
          + f": {DATA} ({'multi-tenant' if control else 'single-tenant'} layout) -> {db.URI.split('@')[-1]}")
    if control and not args.verify:
        with open_ro(control) as con:
            run.control(con)
    ok = True
    for ws, path in found:
        if ws is None:
            ws = run.single_tenant() if not args.verify else (db.workspaces.find_one({"migrated_from": "single-tenant"}) or {}).get("_id")
            if ws is None or ws == "(new)":
                print("  (the dry run can't count against MongoDB before the workspace exists)")
                if not run.apply and not args.verify:
                    run.tenant(-1, path)  # counts only: nothing is written in a dry run
                continue
        print(f"workspace {ws}: {path}")
        if not args.verify:
            run.tenant(ws, path)
        if run.apply or args.verify:
            ok &= run.verify(ws, path)
    ok &= not run.report["problems"]
    if run.apply and ok:
        db.migrations.update_one({"_id": "sqlite"}, {"$set": {"status": "applied", "at": db.utcnow(),
                                                              "source": str(DATA)}}, upsert=True)
    stamp = db.now().replace(":", "")
    report = DATA / f"migration-report-{stamp}.json"
    report.write_text(json.dumps({**run.report, "ok": ok}, indent=2, default=str), encoding="utf-8")
    print(f"\n{'OK' if ok else 'NOT OK'}: report in {report}")
    if run.apply and ok:
        print("The portal can start now. Keep the SQLite files until you have checked it (README 6.1).")
    elif not run.apply and not args.verify:
        print("Dry run only. Back up DATA_DIR, then run with --apply.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
