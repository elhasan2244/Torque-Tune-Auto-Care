"""
state_graph/tickets.py

The Failure Ticket system (Execution requirement: "برمجة مسار Failure
Tickets للتعامل مع أخطاء الـ Mid-Node").

Design: when a node raises an exception the graph engine did NOT expect
as a normal branch (a bug, a downed dependency, a malformed tool
response -- NOT a normal "out of stock" business outcome, which is a
regular state transition, not a failure), the engine:

  1. catches it,
  2. writes a Ticket row capturing the error, the node, and a full
     snapshot of the state as of just before the failing node,
  3. checkpoints the thread as status='failed' so it stops advancing
     silently,
  4. re-raises nothing to the caller by default -- the thread is now
     "stuck" on purpose, visible via list_open_tickets(), until a human
     resolves the ticket (fixes the underlying issue and calls
     resolve_ticket(), then resumes the thread) or explicitly discards it.

This is deliberately NOT the same thing as HITL (state_graph/engine.py's
`interrupt`). HITL is an expected pause built into the graph itself
(waiting on a human decision that was always part of the flow). A
Ticket is the graph hitting something it did NOT expect and refusing to
guess -- the "غير متوقع" (unexpected) case the roadmap calls out
separately from HITL.
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from typing import Any

from state_graph.db import get_connection


@dataclass
class Ticket:
    id: int
    thread_id: str
    graph_name: str
    node_name: str
    error_type: str
    error_message: str
    status: str
    created_at: str
    resolution_note: str | None = None


def file_ticket(
    *,
    thread_id: str,
    graph_name: str,
    node_name: str,
    exc: BaseException,
    state_snapshot: dict[str, Any],
) -> Ticket:
    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO Tickets
                (thread_id, graph_name, node_name, error_type, error_message, state_snapshot, status)
            VALUES (?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                thread_id,
                graph_name,
                node_name,
                type(exc).__name__,
                "".join(traceback.format_exception_only(type(exc), exc)).strip(),
                json.dumps(state_snapshot, default=str),
            ),
        )
        conn.commit()
        ticket_id = cur.lastrowid
    finally:
        conn.close()
    return Ticket(
        id=ticket_id,
        thread_id=thread_id,
        graph_name=graph_name,
        node_name=node_name,
        error_type=type(exc).__name__,
        error_message=str(exc),
        status="open",
        created_at="",
    )


def list_tickets(status: str | None = None) -> list[Ticket]:
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM Tickets WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM Tickets ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    return [
        Ticket(
            id=r["id"],
            thread_id=r["thread_id"],
            graph_name=r["graph_name"],
            node_name=r["node_name"],
            error_type=r["error_type"],
            error_message=r["error_message"],
            status=r["status"],
            created_at=r["created_at"],
            resolution_note=r["resolution_note"],
        )
        for r in rows
    ]


def get_ticket(ticket_id: int) -> Ticket | None:
    conn = get_connection()
    try:
        r = conn.execute("SELECT * FROM Tickets WHERE id = ?", (ticket_id,)).fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    return Ticket(
        id=r["id"],
        thread_id=r["thread_id"],
        graph_name=r["graph_name"],
        node_name=r["node_name"],
        error_type=r["error_type"],
        error_message=r["error_message"],
        status=r["status"],
        created_at=r["created_at"],
        resolution_note=r["resolution_note"],
    )


def resolve_ticket(ticket_id: int, resolution_note: str) -> None:
    """Marks a ticket resolved. Does NOT automatically resume the
    thread -- resuming is a deliberate, separate call
    (CompiledGraph.resume) so a human reviews the fix before the graph
    is allowed to continue from a node that previously failed."""
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE Tickets
            SET status = 'resolved', resolution_note = ?, resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (resolution_note, ticket_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_investigating(ticket_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE Tickets SET status = 'investigating' WHERE id = ?", (ticket_id,))
        conn.commit()
    finally:
        conn.close()
