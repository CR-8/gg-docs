"""MCP server for coding agents: Claude Code, Codex, ChatGPT, Mistral Le Chat, Cursor, Gemini CLI, any MCP client.

Streamable HTTP, stateless: each POST carries JSON-RPC and gets a plain JSON answer (the spec allows that instead of
an SSE stream), so there are no sessions to keep. Its own tools are the read-only ones of Ask (chat.py), with larger
results. Auth decides who the agent is: a person's own MCP token (header or URL) is that person in one workspace; the
workspace's legacy token and an API client's mcp:read token are the workspace, with no person.

A person who connected their GG-Ops account also gets GG-Ops's own MCP tools, named ggops_*: listed and called on
GG-Ops's MCP server with their token (integrations.mcp_as), so GG-Ops decides what they may read and change (create
and update tasks, comments, DSRs) and sends its usual emails. Nothing of GG-Ops is offered to a connection that is no
person.
"""
from . import db, integrations
from .chat import run_tool, system, tools

OLDEST = "2025-03-26"  # first version with Streamable HTTP
LATEST = "2025-11-25"
LIMIT = 50_000  # characters per tool result (~12k tokens), under Claude Code's default MCP output cap
PREFIX = "ggops_"  # GG-Ops's tools, so their names can never meet ours
GGOPS_NOTE = ("\n\nTools named ggops_* are GG-Ops's own (projects, tasks, comments, DSRs). They act as the person this "
              "connection belongs to: what they create or change is theirs in GG-Ops, and GG-Ops emails assignees as "
              "usual. Before creating a task, list the workspace's projects (and members, to assign it).")


def error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


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
            "serverInfo": {"name": "gg-pulse", "title": "gg-pulse", "version": "1.1.0"},
            "instructions": system() + (GGOPS_NOTE if connected(user) else ""),
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["parameters"],
                             "annotations": {"readOnlyHint": True, "openWorldHint": False}} for t in tools()]
                  + ggops_tools(user)}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        args = args if isinstance(args, dict) else {}
        if isinstance(name, str) and name.startswith(PREFIX):
            result = call_ggops(user, name, args)
        elif name in {t["name"] for t in tools()}:
            result = {"content": [{"type": "text", "text": run_tool(name, args, LIMIT)}], "isError": False}
        else:
            return error(mid, -32602, f"Unknown tool: {name}")
    else:
        return error(mid, -32601, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}
