"""MCP server for coding agents: Claude Code, Codex, ChatGPT, Mistral Le Chat, Cursor, Gemini CLI, any MCP client.

Streamable HTTP, stateless: each POST carries JSON-RPC and gets a plain JSON answer (the spec allows that instead of
an SSE stream), so there are no sessions to keep. Its own tools are the read-only ones of Ask (chat.py), with larger
results. Auth decides who the agent is: a person's own MCP token (header or URL) is that person in one workspace; the
workspace's legacy token and an API client's mcp:read token are the workspace, with no person.

A person who connected their GG-Ops account also gets GG-Ops's own MCP tools, named ggops_*: listed and called on
GG-Ops's MCP server with their token (integrations.mcp_as), so GG-Ops decides what they may read and change (create
and update tasks, comments, DSRs) and sends its usual emails. Nothing of GG-Ops is offered to a connection that is no
person.

Two tools are MCP's own, for "build X, split it between us": members (who is in the workspace, with the GG-Ops
account they connected) and, for a person who owns or administers the workspace, start_project: a GitHub repository
made with their own GitHub sign-in, the other members invited to it, imported here, and a GG-Ops project made as them
and linked as a workflow. The agent then splits the work itself, one ggops_create_task per task.
"""
import json
import re

import httpx

from . import WORKSPACE, db, integrations
from .chat import run_tool, system, tools
from .github import RateLimited, gh

OLDEST = "2025-03-26"  # first version with Streamable HTTP
LATEST = "2025-11-25"
LIMIT = 50_000  # characters per tool result (~12k tokens), under Claude Code's default MCP output cap
PREFIX = "ggops_"  # GG-Ops's tools, so their names can never meet ours
GGOPS_NOTE = ("\n\nTools named ggops_* are GG-Ops's own (projects, tasks, comments, DSRs). They act as the person this "
              "connection belongs to: what they create or change is theirs in GG-Ops, and GG-Ops emails assignees as "
              "usual. Before creating a task, list the workspace's projects (and members, to assign it).")
PLAN_NOTE = ("\n\nTo start new work with the team (\"build X, divide the tasks between us\"): call members, then "
             "start_project once (a GitHub repository with everyone invited, imported here, and a GG-Ops project when "
             "you pass ggops_workspace; ggops_list_workspaces names those), then split the work into concrete tasks "
             "and make each with ggops_create_task in that project, assigned by GG-Ops email (members shows the ones "
             "people connected; match the rest by name with ggops_list_members). Spread the work evenly, give each task "
             "a description and a due date, and finish with the links and who does what.")
REPO_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")  # what GitHub allows in a repository name
LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
NO_EDIT = {"readOnlyHint": True, "openWorldHint": False}
OWN_TOOLS = [
    {"name": "members", "description": "Who is in this workspace: GitHub login, name, role, and the GG-Ops account "
     "(name, email) each person connected, or null. Use it to know who to split work between and who to assign.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": NO_EDIT},
    {"name": "start_project", "description": "Start a new project for the team, as you: creates a GitHub repository "
     "(with a README), invites collaborators, imports it into this workspace so its commits, PRs and CI are tracked, "
     "and, with ggops_workspace, creates a GG-Ops project for its tasks. Owners and admins of this workspace only. "
     "Call it once per project; afterwards create the tasks with ggops_create_task in the GG-Ops project it returns.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Repository name, e.g. intelligent-pm-analytics (spaces become -)"},
         "title": {"type": "string", "description": "Human name for the GG-Ops project; default the repository name"},
         "description": {"type": "string", "description": "One or two sentences: what is being built, and the stack"},
         "private": {"type": "boolean", "description": "Default true"},
         "owner": {"type": "string", "description": "A GitHub organization to create it in; default your own account"},
         "collaborators": {"type": "array", "items": {"type": "string"},
                           "description": "GitHub logins to invite with write access; default every other member"},
         "ggops_workspace": {"type": "string", "description": "GG-Ops workspace name or slug for the project's tasks"}},
         "required": ["name"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}},
]


def error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def text(value, failed=False):
    return {"content": [{"type": "text", "text": value if isinstance(value, str) else json.dumps(value, default=str)}],
            "isError": failed}


def members():
    ws = WORKSPACE.get()
    rows = list(db.members.find({"workspace_id": ws}, sort=[("joined_at", 1), ("login_lower", 1)]))
    ids = [m["user_id"] for m in rows if m.get("user_id")]
    names = {u["_id"]: u.get("name") for u in db.users.find({"_id": {"$in": ids}}, {"name": 1})}
    ops = {c["user_id"]: {"name": c.get("name"), "email": c.get("email")}
           for c in db.ggops_connections.find({"user_id": {"$in": ids}}, {"user_id": 1, "name": 1, "email": 1})}
    return [{"login": m["login"], "name": names.get(m.get("user_id")), "role": m["role"],
             "ggops": ops.get(m.get("user_id"))} for m in rows]


def github_says(r):
    try:
        body = r.json()
    except ValueError:
        return f"GitHub answered {r.status_code}"
    errors = "; ".join(e.get("message") or e.get("code", "") for e in body.get("errors") or [] if isinstance(e, dict))
    return f"{body.get('message') or r.status_code}{': ' + errors if errors else ''}"


def ggops_project(user, login, workspace, name, description, repo):
    """The GG-Ops project, made as this person by GG-Ops's own create_project, then linked here as a workflow (it
    exists, and every read of it goes out with the reader's own GG-Ops account or the workspace's key)."""
    if not connected(user):
        return {"created": False, "error": "Connect your GG-Ops account first (Settings → Your GG-Ops account)."}
    args = {"workspace": workspace, "name": name[:80], **({"description": description[:500]} if description else {})}
    try:
        res = integrations.mcp_as(user, "tools/call", {"name": "create_project", "arguments": args})
    except integrations.ServiceError as e:
        return {"created": False, "error": str(e)}
    said = "".join(c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict))
    if res.get("isError"):
        return {"created": False, "error": said}
    try:
        made = json.loads(said)
    except ValueError:
        made = {}
    pid = str(made.get("projectId") or "").lower() if isinstance(made, dict) else ""
    if integrations.PROJECT_ID.match(pid):
        db.workflows.upsert({"workflow_id": pid}, {
            "name": made.get("project") or name[:80], "service": "gg-ops", "status": "connected", "last_error": None,
            "verified_at": db.now(), "repo": repo}, {"added_by": login, "added_at": db.now()})
        db.audit("workflow.added", login, workflow_id=pid, via="mcp")
    return {"created": True, **(made if isinstance(made, dict) else {}), "linked": bool(pid)}


def start_project(user, args):
    """GitHub repository -> collaborators -> import -> GG-Ops project. Each step says how it went, so a failure part
    way (the App not on the new repository, GG-Ops down) leaves the agent and the person knowing what is left."""
    from fastapi import HTTPException
    from .main import connect, user_token  # main imports this module
    ws = WORKSPACE.get()
    me = db.members.find_one({"workspace_id": ws, "user_id": user})
    if not me or me["role"] not in ("owner", "admin"):
        return text("Only owners and admins of this workspace start projects, since it imports a repository.", True)
    u = db.user_view(db.users.find_one({"_id": user}))
    name = REPO_CHARS.sub("-", str(args.get("name") or "")).strip("-.")[:100]
    owner = str(args.get("owner") or "").strip()
    description = " ".join(str(args.get("description") or "").split())
    if not name:
        return text("name: the repository name, e.g. intelligent-pm-analytics", True)
    if owner and not LOGIN.match(owner):
        return text("owner: a GitHub organization's login", True)
    if not gh.mode:
        return text("GitHub is not set up on this portal (GITHUB_APP_ID or GITHUB_TOKEN).", True)
    try:
        token = user_token(u) if gh.mode == "app" else gh.static
        path = f"/orgs/{owner}/repos" if owner and owner.lower() != u["login"].lower() else "/user/repos"
        r = gh.request("POST", path, check=False, token=token, json={
            "name": name, "description": description[:350], "private": args.get("private") is not False,
            "auto_init": True})
        if r.status_code != 201:
            hint = (" The GitHub App needs the Administration (read and write) repository permission to create "
                    "repositories (README 3.1)." if r.status_code in (403, 404) else "")
            return text(f"GitHub didn't create {name}: {github_says(r)}.{hint}", True)
        made = r.json()
        repo = made["full_name"]
        out = {"repository": {"name": repo, "url": made["html_url"], "private": made["private"]}}
        try:
            connect(u, repo)
            out["imported"] = True
        except LookupError as e:
            out.update(imported=False, import_note=f"{e} ({gh.install_url()})" if gh.mode == "app" else str(e))
        except HTTPException as e:
            out.update(imported=False, import_note=e.detail)
        logins = args.get("collaborators")
        if not isinstance(logins, list):
            logins = [m["login"] for m in db.members.find({"workspace_id": ws, "user_id": {"$ne": user}})]
        invited, failed = [], {}
        for login in [str(x).strip().lstrip("@") for x in logins][:50]:
            if login.lower() == u["login"].lower():
                continue
            if not LOGIN.match(login):
                failed[login] = "not a GitHub login"
                continue
            c = gh.request("PUT", f"/repos/{repo}/collaborators/{login}", check=False, token=token,
                           json={"permission": "push"})
            if c.status_code in (201, 204):  # 201: invitation sent, 204: already a collaborator
                invited.append(login)
            else:
                failed[login] = github_says(c)
        out["collaborators"] = {"invited": invited, **({"failed": failed} if failed else {})}
    except HTTPException as e:  # the person's GitHub sign-in expired
        return text(e.detail, True)
    except (RateLimited, httpx.HTTPError) as e:
        return text(f"GitHub: {e}", True)
    if args.get("ggops_workspace"):
        out["ggops"] = ggops_project(user, u["login"], str(args["ggops_workspace"]),
                                     str(args.get("title") or name), description, repo)
    db.audit("project.started", u["login"], repo=repo, via="mcp")
    return text(out)


def call_own(name, args, user):
    if name == "members":
        return text(members())
    if not user:
        return text("start_project needs a person: connect this agent with your own token from Settings → Connect a "
                    "coding agent.", True)
    return start_project(user, args)


def connected(user):
    return bool(user and integrations.ggops_oauth() and db.ggops_connections.find_one({"user_id": user}, {"_id": 1}))


def ggops_tools(user):
    """GG-Ops's tools as this person may use them (GG-Ops lists only what their token's scopes allow). GG-Ops being
    down or the connection gone leaves the list without them rather than failing it."""
    if not connected(user):
        return []
    try:
        listed = integrations.mcp_as(user, "tools/list").get("tools") or []
    except integrations.ServiceError:
        return []
    return [{**t, "name": PREFIX + t["name"], "description": f"GG-Ops: {t.get('description', '')}"}
            for t in listed if isinstance(t, dict) and t.get("name")]


def call_ggops(user, name, args):
    """A ggops_* tool call, made in GG-Ops as this person; GG-Ops's own result, or the reason it didn't happen."""
    if not user:
        return {"content": [{"type": "text", "text": "GG-Ops tools need a person: connect this agent with your own "
                             "token from Settings → Connect a coding agent."}], "isError": True}
    try:
        return integrations.mcp_as(user, "tools/call", {"name": name[len(PREFIX):], "arguments": args})
    except integrations.ServiceError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}


def handle(msg, user=None):
    """One JSON-RPC message -> its response, or None for a notification. `user`: the person this connection belongs
    to, or None for a workspace token."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    if "id" not in msg:
        return None  # notifications/initialized, notifications/cancelled: nothing to answer
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
    if method == "initialize":
        asked = str(params.get("protocolVersion", ""))
        result = {
            # ponytail: a tools-only stateless server reads the same in every version since 2025-03-26, so the
            # client's own version is echoed; pin a list here if a future version changes tools/list or tools/call.
            "protocolVersion": asked if asked >= OLDEST else LATEST,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "gg-pulse", "title": "gg-pulse", "version": "1.2.0"},
            "instructions": system() + (GGOPS_NOTE if connected(user) else "") + (PLAN_NOTE if user else ""),
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["parameters"],
                             "annotations": NO_EDIT} for t in tools()]
                  + [t for t in OWN_TOOLS if user or t["name"] == "members"] + ggops_tools(user)}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        args = args if isinstance(args, dict) else {}
        if isinstance(name, str) and name.startswith(PREFIX):
            result = call_ggops(user, name, args)
        elif name in {t["name"] for t in OWN_TOOLS}:
            result = call_own(name, args, user)
        elif name in {t["name"] for t in tools()}:
            result = {"content": [{"type": "text", "text": run_tool(name, args, LIMIT)}], "isError": False}
        else:
            return error(mid, -32602, f"Unknown tool: {name}")
    else:
        return error(mid, -32601, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}
