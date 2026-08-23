"""
state_graph/db.py

A small, dedicated SQLite database for graph-runtime concerns:
checkpoints (one row per completed/paused node, per thread) and failure
tickets (one row per unexpected mid-node error).

This is deliberately a SEPARATE database file from the inventory demo DB
(agent/demo_db.py / databases/schema.sql). Those hold Torque Tune's
business data (SpareParts, Users, InventoryLogs, memory tables); this one
holds graph-runtime bookkeeping that has to survive a killed process even
if the business-data demo DB is reseeded on the next run. Keeping them
separate also means resetting one never wipes the other by accident.

The path is fixed (not a tempfile) specifically so that a new Python
process -- e.g. after `kill -9` on the old one -- can reconnect to the
SAME database and find the checkpoints the previous process wrote. That
is the whole point of the Crash-and-Resume test in the Execution
Roadmap.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "_data"
STATE_DIR.mkdir(exist_ok=True)

DB_PATH = str(STATE_DIR / "state_graph.sqlite3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS Checkpoints (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id     TEXT NOT NULL,
    graph_name    TEXT NOT NULL,
    node_name     TEXT NOT NULL,     -- last node that finished running
    status        TEXT NOT NULL CHECK (status IN (
                        'running', 'paused_hitl', 'paused_external', 'completed', 'failed',
                        'halted'
                  )),
    state_json    TEXT NOT NULL,     -- full state snapshot after node_name ran
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Fast "give me the latest checkpoint for this thread" lookup.
CREATE INDEX IF NOT EXISTS idx_checkpoints_thread
    ON Checkpoints (thread_id, id DESC);

CREATE TABLE IF NOT EXISTS Tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id       TEXT NOT NULL,
    graph_name      TEXT NOT NULL,
    node_name       TEXT NOT NULL,      -- node that raised the error
    error_type      TEXT NOT NULL,
    error_message   TEXT NOT NULL,
    state_snapshot  TEXT NOT NULL,      -- state as of just before the failing node
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN (
                          'open', 'investigating', 'resolved'
                    )),
    resolution_note TEXT,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at     DATETIME
);

CREATE INDEX IF NOT EXISTS idx_tickets_status ON Tickets (status);
"""


def get_connection() -> sqlite3.Connection:
    """One connection per call, same pattern as agent/demo_db.py's
    build_demo_connection -- callers are expected to open, use, and close
    (or use as a context manager) rather than holding one open long-term,
    since each graph node runs as its own short transaction."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_paused_external(conn: sqlite3.Connection) -> None:
    """One-time migration for local dev DBs created before the
    'paused_external' status existed (Warranty Claim graph's external-wait
    pause, distinct from 'paused_hitl' -- see engine.py's Interrupt.kind).
    SQLite can't ALTER a CHECK constraint in place, so if an existing
    Checkpoints table predates it, rebuild the table. This is safe for
    this runtime-bookkeeping DB specifically (see module docstring: it
    holds no business data, and reset_db() already exists for wiping it)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='Checkpoints'"
    ).fetchone()
    if row is None or "paused_external" in row[0]:
        return
    conn.executescript(
        """
        ALTER TABLE Checkpoints RENAME TO Checkpoints_old;
        """
    )
    conn.executescript(SCHEMA)  # recreates Checkpoints with the new CHECK + Tickets IF NOT EXISTS
    conn.execute(
        "INSERT INTO Checkpoints (id, thread_id, graph_name, node_name, status, state_json, created_at) "
        "SELECT id, thread_id, graph_name, node_name, status, state_json, created_at FROM Checkpoints_old"
    )
    conn.executescript("DROP TABLE Checkpoints_old;")
    conn.commit()


def _migrate_halted_status(conn: sqlite3.Connection) -> None:
    """One-time migration for local dev DBs created before the 'halted'
    status existed (P3-6's TRUE HITL hard-stop -- distinct from
    'paused_hitl'/'paused_external'/'failed', see engine.py's HardStop).
    Same rebuild-in-place approach as `_migrate_paused_external` above,
    for the same reason: SQLite can't ALTER a CHECK constraint in place."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='Checkpoints'"
    ).fetchone()
    if row is None or "halted" in row[0]:
        return
    conn.executescript(
        """
        ALTER TABLE Checkpoints RENAME TO Checkpoints_old;
        """
    )
    conn.executescript(SCHEMA)  # recreates Checkpoints with the new CHECK + Tickets IF NOT EXISTS
    conn.execute(
        "INSERT INTO Checkpoints (id, thread_id, graph_name, node_name, status, state_json, created_at) "
        "SELECT id, thread_id, graph_name, node_name, status, state_json, created_at FROM Checkpoints_old"
    )
    conn.executescript("DROP TABLE Checkpoints_old;")
    conn.commit()


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _migrate_paused_external(conn)
        _migrate_halted_status(conn)
        conn.commit()
    finally:
        conn.close()


def reset_db() -> None:
    """Wipe both tables. Used by tests that need a clean slate -- NOT
    called automatically, so a real crash-and-resume demo run is never
    silently reset out from under itself."""
    conn = get_connection()
    try:
        conn.executescript("DELETE FROM Checkpoints; DELETE FROM Tickets;")
        conn.commit()
    finally:
        conn.close()


init_db()
