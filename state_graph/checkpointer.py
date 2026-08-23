"""
state_graph/checkpointer.py

Snapshot-style checkpointing: after every node finishes, the graph
engine (state_graph/engine.py) writes the FULL state dict to the
Checkpoints table as one row, tagged with which node just ran and what
status the thread is in. Resuming a thread means: read the latest row
for that thread_id, restore `state` from `state_json`, and ask the graph
"what comes after node_name?".

Snapshot-per-step (rather than diff/event-sourcing) is the simplest
design that still gives real crash safety: at any point a process is
killed, the worst case is losing the CURRENT node's in-progress work,
never a state that doesn't correspond to some node boundary the graph
actually reached.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from state_graph.db import get_connection

TERMINAL_STATUSES = {"completed", "failed", "halted"}


@dataclass
class Checkpoint:
    id: int
    thread_id: str
    graph_name: str
    node_name: str
    status: str
    state: dict[str, Any]
    created_at: str


class Checkpointer:
    """Thin persistence wrapper around the Checkpoints table."""

    def save(
        self,
        *,
        thread_id: str,
        graph_name: str,
        node_name: str,
        status: str,
        state: dict[str, Any],
    ) -> Checkpoint:
        payload = json.dumps(state, default=str)
        conn = get_connection()
        try:
            cur = conn.execute(
                """
                INSERT INTO Checkpoints (thread_id, graph_name, node_name, status, state_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (thread_id, graph_name, node_name, status, payload),
            )
            conn.commit()
            row_id = cur.lastrowid
        finally:
            conn.close()
        return Checkpoint(
            id=row_id,
            thread_id=thread_id,
            graph_name=graph_name,
            node_name=node_name,
            status=status,
            state=state,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def latest(self, thread_id: str) -> Checkpoint | None:
        conn = get_connection()
        try:
            row = conn.execute(
                """
                SELECT id, thread_id, graph_name, node_name, status, state_json, created_at
                FROM Checkpoints
                WHERE thread_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return Checkpoint(
            id=row["id"],
            thread_id=row["thread_id"],
            graph_name=row["graph_name"],
            node_name=row["node_name"],
            status=row["status"],
            state=json.loads(row["state_json"]),
            created_at=row["created_at"],
        )

    def history(self, thread_id: str) -> list[Checkpoint]:
        """Every checkpoint for a thread, oldest first -- lets a human (or
        a test) see the full path the graph actually took, not just where
        it ended up."""
        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT id, thread_id, graph_name, node_name, status, state_json, created_at
                FROM Checkpoints
                WHERE thread_id = ?
                ORDER BY id ASC
                """,
                (thread_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            Checkpoint(
                id=r["id"],
                thread_id=r["thread_id"],
                graph_name=r["graph_name"],
                node_name=r["node_name"],
                status=r["status"],
                state=json.loads(r["state_json"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def is_resumable(self, thread_id: str) -> bool:
        cp = self.latest(thread_id)
        return cp is not None and cp.status not in TERMINAL_STATUSES
    
    def list_latest(self, statuses: list[str] | None = None) -> list[Checkpoint]:
        """The latest checkpoint for every distinct thread_id, optionally
        filtered to a set of statuses. Lets the admin panel answer "every
        thread currently paused_hitl" across all three graphs at once."""
        conn = get_connection()
        try:
            rows = conn.execute(
                """
                SELECT id, thread_id, graph_name, node_name, status, state_json, created_at
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY thread_id ORDER BY id DESC
                    ) AS rn
                    FROM Checkpoints
                )
                WHERE rn = 1
                ORDER BY id DESC
                """
            ).fetchall()
        finally:
            conn.close()
        out = [
            Checkpoint(
                id=r["id"],
                thread_id=r["thread_id"],
                graph_name=r["graph_name"],
                node_name=r["node_name"],
                status=r["status"],
                state=json.loads(r["state_json"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]
        if statuses is not None:
            out = [c for c in out if c.status in statuses]
        return out
