"""
Real initialize / initialized handshake logic, per the MCP spec. The
server declares exactly what it supports; the client checks this
declaration before relying on any capability, rather than assuming
everything is supported.

Why this matters for Auto Care's spare parts inventory:
A client that does NOT check for `elicitation` support before offering
the risky `update_inventory` write tool could get stuck waiting for a
confirmation prompt that will never come. Declaring capabilities up
front is what lets a client safely decide whether to expose that tool
or fall back to read-only tools.
"""

SERVER_INFO = {
    "name": "auto-care-inventory-mcp-server",
    "version": "0.1.0",
}

# Single source of truth for what this server actually supports.
SERVER_CAPABILITIES = {
    "resources": {
        "listChanged": False,  # warehouse policy resource is static
    },
    "elicitation": {},  # server can call elicitation/create mid-tool-call
    "tools": {
        "listChanged": True,  # tool set changes at runtime (technician -> manager)
    },
}

# Tracks whether a given session has completed the handshake.
_initialized_sessions = set()


def handle_initialize(request: dict) -> dict:
    """
    Handles a real 'initialize' request. Returns the JSON-RPC response
    containing exactly what this server supports -- nothing assumed.
    """
    client_info = request.get("params", {}).get("clientInfo", {})

    return {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": SERVER_INFO,
            "capabilities": SERVER_CAPABILITIES,
        },
    }


def handle_initialized_notification(session_id: str) -> None:
    """
    Marks a session as fully initialized. Nothing else should be served
    to this session before this notification arrives.
    """
    _initialized_sessions.add(session_id)


def is_session_initialized(session_id: str) -> bool:
    """
    Used by the rest of the server (tools/resources/prompts handlers) to
    defensively refuse requests before the handshake is complete.
    """
    return session_id in _initialized_sessions


def build_not_initialized_error(request_id) -> dict:
    """Standard error response for any request that arrives before the
    initialize/initialized handshake has completed."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": -32002,
            "message": "Server not initialized. Send 'initialize' first.",
        },
    }
