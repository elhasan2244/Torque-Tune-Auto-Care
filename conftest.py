"""Shared pytest fixtures for the whole suite.

Placed at the repository root (not tests/) on purpose: pytest applies a
conftest.py to every test file in directories at or below it, and this
project has three separate test roots -- tests/, state_graph/tests/, and
planning/tests/ -- that all need the same database wiring.

## The leak this fixes (Task 3 handoff finding)

A few test modules pointed the database at the seeded demo SQLite file by
directly assigning ``databases.db.get_connection = demo_db.build_demo_connection``
(state_graph/tests/test_real_graphs.py, tests/test_memory_smoke.py), either
in a bare autouse fixture or at import time, and never restored it.

That direct assignment is fragile for two reasons:

1. Every production module in the list below binds the name once, at its
   own import time (``from databases.db import get_connection``), not by
   looking it up through ``databases.db`` each call. Reassigning
   ``databases.db.get_connection`` only changes what *later* imports of
   those modules see. Whether a given test observed the real (raising)
   connection, the demo one, or a stale mock from an unrelated earlier
   test therefore depended on pytest's collection/import order, not on
   what that test itself asked for.
2. The assignment was a plain attribute write, never undone. Once any
   test executed it, ``databases.db.get_connection`` stayed patched for
   the rest of that pytest process -- so ``databases/db.py``'s "no
   connection configured" ``RuntimeError`` (the genuinely-unwired
   behavior) could silently stop being observable for every later test.

``demo_db_connection`` below fixes both: it patches the *current* name
binding on every module that imports ``get_connection`` directly (so
patch order no longer matters -- each test re-patches every consumer
itself, regardless of when those modules were first imported), using
``monkeypatch.setattr`` (so pytest reverts every patch automatically at
the end of the test, pass or fail). It also reseeds a brand-new demo
SQLite file for every test that requests it, so tests don't share rows.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
MCP_SERVER = ROOT / "mcp-server"
AGENT = ROOT / "agent"

for _path in (str(ROOT), str(MCP_SERVER), str(AGENT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import agent.demo_db as demo_db  # noqa: E402 -- needs sys.path set up above


def _get_connection_consumers():
    """The modules that independently do ``from databases.db import
    get_connection`` (the list from the Task 3 handoff), imported here so
    they can be patched regardless of whether/when a test file already
    imported them.

    ``mcp-server/memory/run_consolidation.py`` also imports
    ``get_connection`` the same way, but it's a standalone script entry
    point (``python mcp-server/memory/run_consolidation.py``), never
    imported as a module by anything under tests/, state_graph/tests/, or
    planning/tests/ -- so it isn't included here.
    """
    import tools.write_tools as write_tools
    import tools.read_tools as read_tools
    import memory.episodic_memory as episodic_memory
    import memory.semantic_memory as semantic_memory
    from state_graph.graphs import (
        warranty_graph,
        purchase_order_graph,
        inventory_approval_graph,
    )

    return [
        write_tools,
        read_tools,
        episodic_memory,
        semantic_memory,
        warranty_graph,
        purchase_order_graph,
        inventory_approval_graph,
    ]


@pytest.fixture
def demo_db_connection(monkeypatch):
    """Point every ``get_connection`` consumer at one freshly seeded,
    isolated demo SQLite database for the duration of a single test.

    Request this fixture (directly, or through a local autouse fixture
    that depends on it) in any test that needs a real, seeded database.
    Every patch it makes is undone by pytest when the test ends, so nothing
    leaks into the next test regardless of pass/fail or execution order.
    """
    demo_db.reset_demo_database()
    monkeypatch.setattr("databases.db.get_connection", demo_db.build_demo_connection)
    for module in _get_connection_consumers():
        monkeypatch.setattr(module, "get_connection", demo_db.build_demo_connection)
    return demo_db.build_demo_connection
