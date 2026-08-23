"""
state_graph/run_demo.py

Run directly for a narrated, end-to-end walkthrough of everything
state_graph/ adds:

    python state_graph/run_demo.py

What it shows, in order:
  1. Graph 1 (purchase_order): a supplier reorder batch that clears the
     manager-approval threshold, waits for the supplier to confirm, and
     completes -- Task Decomposition (per-supplier batching) +
     Constrained ReAct (bounded verify/draft/escalate action set).
  2. Graph 2 (inventory_approval): a sensitive change that PAUSES for a
     manager (HITL), grounded in real company policy via RAG, then is
     resumed and actually applied to the database.
  3. Graph 3 (warranty_claim): a claim that gets rejected, generates a
     Tree-of-Thoughts appeal argument, pauses for manager approval above
     a dollar threshold (HITL), then is resent and approved -- RAG
     Architecture (policy grounding) + Tree of Thoughts (appeal choice).
  4. A live Crash-and-Resume: starts a thread, then simulates the
     process dying immediately after the first checkpoint by throwing
     away the in-memory graph object and rebuilding everything from
     scratch (a fresh CompiledGraph, fresh imports-equivalent state) --
     resume() proves the thread continues correctly from disk alone.
     (state_graph/tests/test_crash_resume.py additionally proves this
     across a REAL os.kill'd subprocess, not just an in-memory reset.)
  5. A deliberately broken node that raises mid-run -- shows a Failure
     Ticket getting filed and the thread halting instead of crashing the
     whole process.
"""

from __future__ import annotations

from state_graph.bootstrap import ensure_wired  # noqa: F401
import agent.demo_db as demo_db
import databases.db as db
from state_graph.db import reset_db
from state_graph.checkpointer import Checkpointer
from state_graph.tickets import list_tickets


def _setup() -> None:
    reset_db()
    demo_db.reset_demo_database()
    db.get_connection = demo_db.build_demo_connection


def demo_purchase_order() -> None:
    from state_graph.graphs.purchase_order_graph import (
        build_graph,
        record_manager_decision,
        record_supplier_reply,
    )

    print("\n=== Graph 1: Purchase Order (Task Decomposition + Constrained ReAct) ===")
    compiled = build_graph().compile()
    thread_id = "demo-purchase-order-1"
    paused = compiled.invoke(thread_id, {"user_id": 2})
    print(f"status={paused.status} node={paused.node_name}")
    if paused.status == "paused_hitl":
        batch = paused.state["current_batch"]
        print(f"  HITL: ${batch['total_cost']} PO for {batch['supplier_name']} needs manager sign-off")
        print("  [manager approves]")
        paused = record_manager_decision(thread_id, True)
        print(f"status={paused.status} node={paused.node_name}")
    print(f"  -- PO {paused.state.get('po_code')} sent; thread paused on disk awaiting supplier --")
    print("  [supplier confirms]")
    resumed = record_supplier_reply(thread_id, "confirmed", note="Ships in 5 business days")
    print(f"status={resumed.status} node={resumed.node_name}")
    print(f"  batch_results={resumed.state.get('batch_results')}")


def demo_inventory_approval() -> None:
    from state_graph.graphs.inventory_approval_graph import build_graph

    print("\n=== Graph 2: Sensitive Inventory Update (RAG policy check + graph-level HITL) ===")
    compiled = build_graph().compile()
    thread_id = "demo-inventory-1"
    paused = compiled.invoke(
        thread_id,
        {"part_id": 2, "action": "decrease", "quantity": 2, "reason": "Sold to customer #4471", "user_id": 2},
    )
    print(f"status={paused.status} node={paused.node_name}")
    print(f"  sensitive={paused.state.get('sensitive')}  reason={paused.state.get('elicitation_reason')}")
    print(f"  policy check (RAG-grounded): {paused.state.get('policy_check', '')[:120]}...")
    print("  -- thread is now paused on disk; a manager could take hours to respond --")

    print("  [manager approves]")
    resumed = compiled.resume(thread_id, human_response={"approved": True})
    print(f"status={resumed.status} node={resumed.node_name}")
    print(f"  update_result={resumed.state.get('update_result')}")


def demo_warranty_claim() -> None:
    from state_graph.graphs.warranty_graph import (
        build_graph,
        record_manager_decision,
        record_supplier_reply,
    )

    print("\n=== Graph 3: Warranty Claim (RAG Architecture + Tree of Thoughts) ===")
    compiled = build_graph().compile()
    thread_id = "demo-warranty-1"
    paused = compiled.invoke(
        thread_id,
        {
            "part_id": 4,
            "user_id": 2,
            "inventory_log_id": 1,
            "claim_reason": "manufacturing defect in caliper mount, not wear",
        },
    )
    print(f"status={paused.status} node={paused.node_name}")
    print(f"  claim_code={paused.state.get('claim_code')}  policy_eligible={paused.state.get('policy_eligible')}")
    print("  -- thread paused on disk; supplier may take days to answer --")

    print("  [supplier rejects the first submission]")
    rejected = record_supplier_reply(thread_id, "rejected", note="Photos inconclusive")
    print(f"status={rejected.status} node={rejected.node_name}")
    print(f"  Tree-of-Thoughts appeal argument chosen: {rejected.state.get('appeal_argument')}")

    if rejected.status == "paused_hitl":
        print(f"  HITL: appeal for ${rejected.state['price']} claim needs manager sign-off before resending")
        print("  [manager approves]")
        rejected = record_manager_decision(thread_id, True)
        print(f"status={rejected.status} node={rejected.node_name}")

    print("  [supplier approves the appeal]")
    final = record_supplier_reply(thread_id, "approved", note="Appeal accepted")
    print(f"status={final.status} node={final.node_name} final_status={final.state.get('final_status')}")


def demo_crash_and_resume() -> None:
    from state_graph.graphs.purchase_order_graph import build_graph, decompose_into_supplier_batches

    print("\n=== Live Crash-and-Resume ===")
    thread_id = "demo-crash-1"

    # Simulate "process 1": run only the first node, checkpoint it, then
    # stop -- as if the process died right here.
    state = {"thread_id": thread_id, "user_id": 2}
    state.update(decompose_into_supplier_batches(state))
    Checkpointer().save(
        thread_id=thread_id,
        graph_name="purchase_order",
        node_name="decompose_into_supplier_batches",
        status="running",
        state=state,
    )
    print("  [process 1] ran 'decompose_into_supplier_batches', checkpointed, then crashed (simulated)")
    del state  # nothing left in memory from "process 1"

    # "process 2": nothing but the thread_id and a fresh graph object.
    compiled = build_graph().compile()
    result = compiled.resume(thread_id)
    print(f"  [process 2] resumed -> status={result.status} node={result.node_name}")
    print("  see state_graph/tests/test_crash_resume.py for the same proof across a real os.kill'd process")


def demo_failure_ticket() -> None:
    from state_graph.engine import StateGraph, END

    print("\n=== Failure Ticket on an unexpected Mid-Node error ===")

    def flaky_node(state):
        raise ConnectionError("simulated: downstream supplier API unreachable")

    g = StateGraph("demo_ticket").add_node("flaky", flaky_node)
    g.set_entry_point("flaky").add_edge("flaky", END)
    compiled = g.compile()

    result = compiled.invoke("demo-ticket-1", {})
    print(f"status={result.status} ticket_id={result.ticket_id}")
    open_tickets = list_tickets(status="open")
    print(f"open tickets now: {len(open_tickets)}")
    if open_tickets:
        t = open_tickets[0]
        print(f"  #{t.id} [{t.graph_name}/{t.node_name}] {t.error_type}: {t.error_message}")


def main() -> None:
    _setup()
    demo_purchase_order()
    demo_inventory_approval()
    demo_warranty_claim()
    demo_crash_and_resume()
    demo_failure_ticket()
    print("\nAll demos completed.")


if __name__ == "__main__":
    main()
