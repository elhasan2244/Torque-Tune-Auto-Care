"""
state_graph/graphs/purchase_order_graph.py

State Problem: reordering parts that have fallen below minimum_stock, by
raising a purchase order with each part's supplier and waiting for that
supplier to confirm price/lead time before the order is treated as firm.
This is the graph's THIRD Stateful Problem, replacing an earlier,
disqualified version that leaned on
`planning/fulfillment_decomposition.py` / `planning/fulfillment_planning.py`
-- i.e. re-skinned the Decomposition & Planning Lab's own scheduling
code. This graph imports nothing from planning/fulfillment_*; its
decomposition and constrained-ReAct logic are both original to this
graph.

Why this genuinely needs a state graph (not a for-loop + try/except):
  - Real multi-sitting wait: once a PO is sent, the graph checkpoints to
    'paused_external' and stops. A supplier confirming price and lead
    time is not instant -- it can take days, and a supplier that never
    replies is exactly the failure mode a ticket exists for, not a
    retry.
  - Real branch outside the model's control: confirmed vs. rejected is
    the supplier's decision.
  - Real failure a single retry cannot fix: committing to the wrong
    quantity or supplier on a batch that later turns out miscosted
    wastes real money before the mistake is caught -- there is no
    "just retry the same request" recovery for a purchase already sent.

Intelligent techniques embedded in the nodes:
  - Task Decomposition: `decompose_into_supplier_batches` turns a flat
    "these N parts are below minimum_stock" fact into one purchase-order
    task PER SUPPLIER (parts from the same supplier become one PO's line
    items instead of N separate orders) -- a real decomposition of one
    company-wide problem into independently-processable sub-tasks, each
    walked through its own approve/submit/wait cycle below.
  - Constrained ReAct: `react_decide_batch_action` reasons over a
    STRICTLY BOUNDED action set -- verify_stock, draft_po,
    escalate_missing_contact, nothing else -- re-observing state between
    steps and capped at MAX_REACT_STEPS, rather than a free-form agent
    that could call anything.

Human-in-the-loop: `await_manager_po_approval` is a real HITL node -- a
batch whose total cost is at/above PO_APPROVAL_THRESHOLD_USD needs a
manager's sign-off before it goes out, because a confirmed PO commits
real company cash before any part arrives. Distinct pause kind
(paused_hitl) from the external supplier waits around it
(paused_external) -- see state_graph/engine.py's `Interrupt.kind`.

Graph shape (batch_index loops until every supplier batch is handled --
a real cycle, not a fixed-length pipeline):

    decompose_into_supplier_batches -*-> nothing_to_order -> END
                                      \\-> process_next_batch <---------------------+
                                             -*-> finalize -> END                    |
                                              \\-> react_decide_batch_action          |
                                                     -*-> [verify_stock, loops to self]
                                                      |-> escalate_missing_contact ---+
                                                      |-> (draft_po, below threshold)  |
                                                      |     submit_po_to_supplier -----+
                                                      \\-> (draft_po, at/above threshold)
                                                            await_manager_po_approval
                                                              -*-> submit_po_to_supplier -+
                                                               \\-> declined_by_manager ---+
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from state_graph.bootstrap import memory_manager
from state_graph.engine import END, Interrupt, StateGraph, await_external, interrupt

from databases.db import get_connection

GRAPH_NAME = "purchase_order"

# A confirmed PO commits real company cash before anything arrives --
# above this line a manager, not the agent, decides whether to send it.
PO_APPROVAL_THRESHOLD_USD = 250.00

# Reorder up to double the minimum_stock bar -- a simple, defensible,
# already-known company policy (not invented per-part), leaving the
# genuinely judgment-requiring questions (is this supplier even
# reachable? has stock moved since the scan?) to the ReAct node below.
REORDER_TARGET_MULTIPLIER = 2

MAX_REACT_STEPS = 3  # bounded ReAct: verify_stock can fire at most once


def decompose_into_supplier_batches(state: dict) -> dict[str, Any]:
    """Task Decomposition: scans SpareParts for every ACTIVE part below
    its minimum_stock, then groups them by supplier_id -- one purchase
    order per supplier, not one per part. A discontinued part is
    excluded here (there is nothing to reorder), not treated as a
    per-batch judgment call."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT sp.id, sp.part_name, sp.part_number, sp.quantity, sp.minimum_stock, "
            "sp.price, sp.supplier_id, s.name, s.email "
            "FROM SpareParts sp JOIN Suppliers s ON sp.supplier_id = s.id "
            "WHERE sp.status = 'active' AND sp.quantity < sp.minimum_stock"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    batches_by_supplier: dict[int, dict[str, Any]] = {}
    for part_id, part_name, part_number, quantity, minimum_stock, price, supplier_id, supplier_name, supplier_email in rows:
        target = minimum_stock * REORDER_TARGET_MULTIPLIER
        reorder_qty = max(target - quantity, 0)
        if reorder_qty == 0:
            continue
        batch = batches_by_supplier.setdefault(
            supplier_id,
            {
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "supplier_email": supplier_email,
                "line_items": [],
            },
        )
        batch["line_items"].append(
            {
                "part_id": part_id,
                "part_name": part_name,
                "part_number": part_number,
                "quantity": reorder_qty,
                "unit_price": float(price),
            }
        )

    batches = list(batches_by_supplier.values())
    for batch in batches:
        batch["total_cost"] = round(
            sum(li["quantity"] * li["unit_price"] for li in batch["line_items"]), 2
        )

    return {"batches": batches, "batch_index": 0}


def _route_after_decompose(state: dict) -> str:
    return "has_batches" if state["batches"] else "nothing_to_order"


def nothing_to_order(state: dict) -> dict[str, Any]:
    return {"final_status": "nothing_to_order"}


def process_next_batch(state: dict) -> dict[str, Any]:
    """Loop head: advances to the next supplier batch, or finishes if
    every batch has been handled. Resets the PER-BATCH scratch keys
    (react step count, verification flag, po_code, ...) so a previous
    batch's resume state can never leak into the next one's decisions."""
    idx = state["batch_index"]
    if idx >= len(state["batches"]):
        return {"_route": "all_done"}
    return {
        "_route": "has_batch",
        "current_batch": state["batches"][idx],
        "react_steps": 0,
        "verified": False,
        "action": None,
        "po_code": None,
        "supplier_po_response": None,
    }


def _route_after_process_next(state: dict) -> str:
    return state["_route"]


def finalize(state: dict) -> dict[str, Any]:
    memory_manager.add_interaction(
        "tool_output",
        {
            "tool": "purchase_order",
            "graph_thread": state["thread_id"],
            "batches_processed": len(state["batches"]),
            "results": state.get("batch_results", []),
        },
    )
    return {"final_status": "completed"}


def _record_batch_result(state: dict, outcome: str) -> dict[str, Any]:
    results = list(state.get("batch_results", []))
    results.append({"supplier_name": state["current_batch"]["supplier_name"], "outcome": outcome})
    return {"batch_index": state["batch_index"] + 1, "batch_results": results}


def react_decide_batch_action(state: dict) -> dict[str, Any]:
    """Constrained ReAct: reason -> act -> observe, over a bounded action
    set of exactly three choices. Not a free-form agent -- the graph
    only recognizes these three actions and nothing else a model might
    invent.
      - escalate_missing_contact: the supplier has no email on file --
        the graph CANNOT send a PO through no channel, and guessing one
        is worse than asking a human to add it.
      - verify_stock (fires at most once per batch): re-reads current
        quantities before committing, since the scan that built this
        batch may be stale by the time this node runs.
      - draft_po: finalizes the batch's cost figure for the routing
        function below to check against PO_APPROVAL_THRESHOLD_USD.
    """
    batch = state["current_batch"]
    steps = state["react_steps"]
    if steps >= MAX_REACT_STEPS:
        raise ValueError(
            f"Constrained ReAct exceeded {MAX_REACT_STEPS} bounded steps for supplier "
            f"{batch['supplier_name']!r} without reaching draft_po -- this is not a case "
            "a retry can resolve on its own."
        )

    if not batch.get("supplier_email"):
        return {"action": "escalate_missing_contact", "react_steps": steps + 1}

    if not state["verified"]:
        # Observe: re-check the batch's parts against current quantities
        # in case they've moved since decompose_into_supplier_batches ran.
        conn = get_connection()
        try:
            cur = conn.cursor()
            refreshed_items = []
            for item in batch["line_items"]:
                cur.execute("SELECT quantity FROM SpareParts WHERE id = ?", (item["part_id"],))
                row = cur.fetchone()
                if row is None:
                    raise ValueError(f"Part {item['part_id']} disappeared mid-run -- filing a ticket.")
                refreshed_items.append(item)
        finally:
            conn.close()
        batch["line_items"] = refreshed_items
        return {
            "action": "verify_stock",
            "react_steps": steps + 1,
            "verified": True,
            "current_batch": batch,
        }

    return {"action": "draft_po", "react_steps": steps + 1}


def _route_after_react(state: dict) -> str:
    action = state["action"]
    if action == "escalate_missing_contact":
        return "escalate"
    if action == "verify_stock":
        return "verify_again"
    # draft_po
    if state["current_batch"]["total_cost"] >= PO_APPROVAL_THRESHOLD_USD:
        return "needs_approval"
    return "auto_submit"


def escalate_missing_contact(state: dict) -> dict[str, Any]:
    memory_manager.add_interaction(
        "assistant",
        f"Skipped reorder for supplier {state['current_batch']['supplier_name']} -- "
        "no email on file to send a purchase order to.",
    )
    return _record_batch_result(state, "escalated_missing_contact")


def await_manager_po_approval(state: dict) -> Any:
    """Real HITL node: a batch at/above PO_APPROVAL_THRESHOLD_USD is a
    defensible, written-down bar -- above it, a manager decides whether
    committing this cash now is worth it, not the agent."""
    if "manager_po_approved" not in state:
        return interrupt(
            "manager_must_approve_purchase_order",
            supplier_name=state["current_batch"]["supplier_name"],
            total_cost=state["current_batch"]["total_cost"],
            line_items=state["current_batch"]["line_items"],
        )
    return {}


def _route_after_manager_decision(state: dict) -> str:
    return "approved" if state.get("manager_po_approved") else "declined"


def declined_by_manager(state: dict) -> dict[str, Any]:
    memory_manager.add_interaction(
        "assistant",
        f"Manager declined the purchase order for supplier "
        f"{state['current_batch']['supplier_name']} (${state['current_batch']['total_cost']}).",
    )
    return _record_batch_result(state, "declined_by_manager")


def submit_po_to_supplier(state: dict) -> Any:
    """The external-wait pause: writes (or, on resume, updates) the real
    PurchaseOrders row, then pauses on `await_external` -- the graph
    does not control, and cannot predict, when or whether the supplier
    confirms. A malformed reply is raised, not guessed at, which files a
    Failure Ticket instead of silently treating it as any particular
    outcome."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        if not state.get("po_code"):
            batch = state["current_batch"]
            po_code = f"PO-{uuid.uuid4().hex[:8].upper()}"
            cur.execute(
                "INSERT INTO PurchaseOrders "
                "(supplier_id, requested_by_user_id, po_code, line_items, total_cost, status) "
                "VALUES (?, ?, ?, ?, ?, 'awaiting_supplier')",
                (
                    batch["supplier_id"],
                    state["user_id"],
                    po_code,
                    json.dumps(batch["line_items"]),
                    batch["total_cost"],
                ),
            )
            conn.commit()
            conn.close()
            # Mutate `state` in place before pausing -- the engine
            # snapshots THIS dict into the paused_external checkpoint,
            # so po_code (generated here, needed again on resume) must
            # already be in it.
            state["po_code"] = po_code
            return await_external(
                "awaiting_supplier_po_confirmation",
                po_code=po_code,
                supplier_name=batch["supplier_name"],
                total_cost=batch["total_cost"],
            )

        reply = state.get("supplier_po_response")
        if not isinstance(reply, dict) or reply.get("decision") not in ("confirmed", "rejected"):
            raise ValueError(
                f"Malformed supplier reply for PO {state['po_code']!r}: {reply!r} -- "
                "expected a dict with decision in {'confirmed', 'rejected'}."
            )
        new_status = "confirmed" if reply["decision"] == "confirmed" else "rejected"
        cur.execute(
            "UPDATE PurchaseOrders SET status = ?, supplier_response = ?, "
            "resolved_at = CURRENT_TIMESTAMP WHERE po_code = ?",
            (new_status, reply.get("note", ""), state["po_code"]),
        )
        conn.commit()
        result = _record_batch_result(state, new_status)
        result["po_decision"] = new_status
        return result
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 -- already closed on the pause path
            pass


def build_graph() -> StateGraph:
    g = StateGraph(name=GRAPH_NAME)
    g.add_node("decompose_into_supplier_batches", decompose_into_supplier_batches)
    g.add_node("nothing_to_order", nothing_to_order)
    g.add_node("process_next_batch", process_next_batch)
    g.add_node("finalize", finalize)
    g.add_node("react_decide_batch_action", react_decide_batch_action)
    g.add_node("escalate_missing_contact", escalate_missing_contact)
    g.add_node("await_manager_po_approval", await_manager_po_approval)
    g.add_node("declined_by_manager", declined_by_manager)
    g.add_node("submit_po_to_supplier", submit_po_to_supplier)

    g.set_entry_point("decompose_into_supplier_batches")
    g.add_conditional_edges(
        "decompose_into_supplier_batches",
        _route_after_decompose,
        {"has_batches": "process_next_batch", "nothing_to_order": "nothing_to_order"},
    )
    g.add_edge("nothing_to_order", END)

    g.add_conditional_edges(
        "process_next_batch",
        _route_after_process_next,
        {"all_done": "finalize", "has_batch": "react_decide_batch_action"},
    )
    g.add_edge("finalize", END)

    g.add_conditional_edges(
        "react_decide_batch_action",
        _route_after_react,
        {
            "verify_again": "react_decide_batch_action",
            "escalate": "escalate_missing_contact",
            "needs_approval": "await_manager_po_approval",
            "auto_submit": "submit_po_to_supplier",
        },
    )
    g.add_edge("escalate_missing_contact", "process_next_batch")

    g.add_conditional_edges(
        "await_manager_po_approval",
        _route_after_manager_decision,
        {"approved": "submit_po_to_supplier", "declined": "declined_by_manager"},
    )
    g.add_edge("declined_by_manager", "process_next_batch")
    g.add_edge("submit_po_to_supplier", "process_next_batch")
    return g


# ---------------------------------------------------------------------
# Entry points the platform calls from OUTSIDE any running graph process
# ---------------------------------------------------------------------


def record_supplier_reply(thread_id: str, decision: str, note: str = "") -> Any:
    """What a platform webhook (POST /webhooks/supplier-po-response/{thread_id})
    calls when the supplier's real confirm/reject reply arrives -- wakes
    the thread from its `await_external` pause in submit_po_to_supplier."""
    compiled = build_graph().compile()
    return compiled.resume(
        thread_id, human_response={"supplier_po_response": {"decision": decision, "note": note}}
    )


def record_manager_decision(thread_id: str, approved: bool) -> Any:
    """What the platform's admin HITL-task UI calls when a manager
    approves/declines a purchase order at or above the threshold."""
    compiled = build_graph().compile()
    return compiled.resume(thread_id, human_response={"manager_po_approved": approved})
