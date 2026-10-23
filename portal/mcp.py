"""MCP server for coding agents: Claude Code, Codex, ChatGPT, Mistral Le Chat, Cursor, Gemini CLI, any MCP client.

Streamable HTTP, stateless: each POST carries JSON-RPC and gets a plain JSON answer (the spec allows that instead of
an SSE stream), so there are no sessions to keep. Read-only: the same tools as Ask (chat.py), with larger results.
Auth decides which workspace the agent sees: an API client's access token with the mcp:read scope
(`Authorization: Bearer <token>`, preferred), or the workspace's legacy MCP token, as that header or in the URL
(/mcp/<token>) for clients that only take a URL; admins can rotate the legacy token or turn it off.
"""
from .chat import run_tool, system, tools

OLDEST = "2025-03-26"  # first version with Streamable HTTP
LATEST = "2025-11-25"
LIMIT = 50_000  # characters per tool result (~12k tokens), under Claude Code's default MCP output cap


def error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg):
    """One JSON-RPC message -> its response, or None for a notification."""
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
            "serverInfo": {"name": "gg-portal", "title": "gg engineering portal", "version": "1.0.0"},
            "instructions": system(),
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["parameters"],
                             "annotations": {"readOnlyHint": True, "openWorldHint": False}} for t in tools()]}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in tools()}:
            return error(mid, -32602, f"Unknown tool: {name}")
        out = run_tool(name, args if isinstance(args, dict) else {}, LIMIT)
        result = {"content": [{"type": "text", "text": out}], "isError": False}
    else:
        return error(mid, -32601, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": mid, "result": result}
