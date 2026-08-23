"""
state_graph/bootstrap.py

Every graph node in this package calls the SAME real MCP tools, memory
manager, and planning/RAG modules the rest of the project already built
-- nothing about inventory, memory, or RAG is reimplemented here. This
module just does the one-time import wiring agent/client.py also does
(wire_demo_database() before anything imports databases.db), so graph
code can `from state_graph.bootstrap import server, memory_manager, ...`
instead of duplicating that setup in every graph file.

Importing this module is what makes `mcp-server/` importable as
`server`, `app`, `tools`, etc. (it inserts mcp-server/ onto sys.path,
same as agent/client.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_ROOT = ROOT / "mcp-server"
AGENT_ROOT = ROOT / "agent"

for path in (str(ROOT), str(MCP_SERVER_ROOT), str(AGENT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


def wire_demo_database() -> None:
    import databases.db as db
    from agent.demo_db import build_demo_connection

    db.get_connection = build_demo_connection


_wired = False


def ensure_wired() -> None:
    """Idempotent: safe to call from every graph module's import time."""
    global _wired
    if _wired:
        return
    wire_demo_database()
    _wired = True


ensure_wired()

import server  # noqa: E402  (registers tools + resources onto app.mcp)
from app import memory_manager  # noqa: E402
from fastmcp import ElicitationResult  # noqa: E402


class GraphContext:
    """Context object handed to MCP tools that need one (ctx.elicit,
    ctx.report_progress). Graph-level HITL (state_graph/engine.py's
    `interrupt`) already gathered human approval BEFORE a node calls a
    tool through this context, so auto_confirm is always True here --
    this is not a second, redundant human prompt, it's just satisfying
    the tool's own signature."""

    def __init__(self, auto_confirm: bool = True):
        self.auto_confirm = auto_confirm

    async def elicit(self, message: str, schema: dict | None = None) -> ElicitationResult:
        return ElicitationResult("accept" if self.auto_confirm else "decline", self.auto_confirm)

    async def report_progress(self, progress: float, total: float = 100) -> None:
        return None
