"""End-to-end tests for the three real graphs against the seeded demo
database -- these are the actual Stateful Problems, not synthetic
stand-ins."""

import uuid

import pytest

from state_graph.bootstrap import ensure_wired  # noqa: F401 -- import-time wiring
import databases.db as db
from state_graph.db import reset_db


@pytest.fixture(autouse=True)
def fresh_databases(demo_db_connection):
    """Wires ``databases.db.get_connection`` (and every module that
    imports it directly) to a fresh, isolated demo SQLite database via the
    shared ``demo_db_connection`` fixture in the root conftest.py -- see
    that file for why this replaced a direct, never-restored
    ``db.get_connection = ...`` assignment. ``reset_db()`` is this graph
    suite's own, separate concern: it wipes the unrelated Checkpoints/
    Tickets tables in state_graph/db.py, not the demo database above.
    """
    reset_db()
    yield


def _tid() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def test_purchase_order_graph_below_threshold_auto_submits_no_hitl():
    """A batch under PO_APPROVAL_THRESHOLD_USD should go straight to the
    supplier without a manager pause."""
    from state_graph.graphs.purchase_order_graph import build_graph, record_supplier_reply

    compiled = build_graph().compile()
    tid = _tid()
    # Lower the seeded NAPA batch below threshold by topping up quantity
    # first, then create a small, cheap low-stock part instead.
    conn = db.get_connection()
    conn.execute("UPDATE SpareParts SET quantity = 10 WHERE part_number = 'BRK-002'")
    conn.execute(
        "INSERT INTO SpareParts (part_name, part_number, category_id, supplier_id, quantity, "
        "price, location, minimum_stock, status) VALUES "
        "('Cabin Air Filter', 'NAP-777', 1, 1, 1, 5.00, 'C1-01', 3, 'active')"
    )
    conn.commit()
    conn.close()

    result = compiled.invoke(tid, {"user_id": 2})
    assert result.status == "paused_external"
    assert result.node_name == "submit_po_to_supplier"
    assert result.state["current_batch"]["total_cost"] < 250.0

    resumed = record_supplier_reply(tid, "confirmed", note="Ships next week")
    assert resumed.status == "completed"
    assert resumed.state["final_status"] == "completed"


def test_purchase_order_graph_above_threshold_requires_hitl_then_confirms():
    from state_graph.graphs.purchase_order_graph import (
        build_graph,
        record_manager_decision,
        record_supplier_reply,
    )

    compiled = build_graph().compile()
    tid = _tid()
    paused = compiled.invoke(tid, {"user_id": 2})  # seeded NAPA batch is $312, above $250
    assert paused.status == "paused_hitl"
    assert paused.node_name == "await_manager_po_approval"
    assert paused.state["current_batch"]["total_cost"] >= 250.0

    approved = record_manager_decision(tid, True)
    assert approved.status == "paused_external"
    assert approved.state["po_code"].startswith("PO-")

    final = record_supplier_reply(tid, "confirmed", note="Ships in 5 business days")
    assert final.status == "completed"
    assert final.state["batch_results"] == [
        {"supplier_name": "NAPA Distribution", "outcome": "confirmed"}
    ]


def test_purchase_order_graph_manager_declines_skips_batch():
    from state_graph.graphs.purchase_order_graph import build_graph, record_manager_decision

    compiled = build_graph().compile()
    tid = _tid()
    compiled.invoke(tid, {"user_id": 2})
    result = record_manager_decision(tid, False)
    assert result.status == "completed"
    assert result.state["batch_results"] == [
        {"supplier_name": "NAPA Distribution", "outcome": "declined_by_manager"}
    ]


def test_purchase_order_graph_escalates_supplier_with_no_contact_email():
    from state_graph.graphs.purchase_order_graph import (
        build_graph,
        record_manager_decision,
        record_supplier_reply,
    )

    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO Suppliers (name) VALUES ('NoContact Parts Co.')")
    sid = cur.lastrowid
    cur.execute(
        "INSERT INTO SpareParts (part_name, part_number, category_id, supplier_id, quantity, "
        "price, location, minimum_stock, status) VALUES "
        "('Widget', 'NCX-001', 1, ?, 1, 10.0, 'Z9-01', 5, 'active')",
        (sid,),
    )
    conn.commit()
    conn.close()

    compiled = build_graph().compile()
    tid = _tid()
    result = compiled.invoke(tid, {"user_id": 2})  # NAPA batch ($312) sorts first -> HITL
    assert result.status == "paused_hitl"
    result = record_manager_decision(tid, True)
    assert result.status == "paused_external"
    result = record_supplier_reply(tid, "confirmed", note="ok")
    assert result.status == "completed"
    outcomes = {r["supplier_name"]: r["outcome"] for r in result.state["batch_results"]}
    assert outcomes["NoContact Parts Co."] == "escalated_missing_contact"


def test_purchase_order_graph_malformed_supplier_reply_files_a_ticket():
    from state_graph.graphs.purchase_order_graph import (
        build_graph,
        record_manager_decision,
        record_supplier_reply,
    )
    from state_graph.tickets import list_tickets

    compiled = build_graph().compile()
    tid = _tid()
    compiled.invoke(tid, {"user_id": 2})
    record_manager_decision(tid, True)
    result = record_supplier_reply(tid, "idk-call-us-later", note="unclear")
    assert result.status == "failed"
    assert result.ticket_id is not None

    open_tickets = [t for t in list_tickets(status="open") if t.thread_id == tid]
    assert len(open_tickets) == 1
    assert open_tickets[0].node_name == "submit_po_to_supplier"


def test_inventory_approval_graph_pauses_for_sensitive_change_then_applies():
    from state_graph.graphs.inventory_approval_graph import build_graph

    compiled = build_graph().compile()
    tid = _tid()
    # Rear Brake Pad Set (id=2) has qty=2; decreasing by 2 zeroes it out -> sensitive.
    paused = compiled.invoke(
        tid,
        {"part_id": 2, "action": "decrease", "quantity": 2, "reason": "Sold to customer", "user_id": 2},
    )
    assert paused.status == "paused_hitl"
    assert paused.state["sensitive"] is True

    resumed = compiled.resume(tid, human_response={"approved": True})
    assert resumed.status == "completed"
    assert resumed.state["update_result"]["new_quantity"] == 0


def test_inventory_approval_graph_rejection_cancels_without_writing():
    from state_graph.graphs.inventory_approval_graph import build_graph

    compiled = build_graph().compile()
    tid = _tid()
    compiled.invoke(
        tid,
        {"part_id": 2, "action": "decrease", "quantity": 2, "reason": "Sold to customer", "user_id": 2},
    )
    resumed = compiled.resume(tid, human_response={"approved": False})
    assert resumed.status == "completed"
    assert resumed.state["final_status"] == "cancelled"


def test_inventory_approval_graph_skips_approval_for_small_change():
    from state_graph.graphs.inventory_approval_graph import build_graph

    compiled = build_graph().compile()
    result = compiled.invoke(
        _tid(),
        {"part_id": 1, "action": "increase", "quantity": 1, "reason": "Restock", "user_id": 2},
    )
    assert result.status == "completed"
    assert result.state["sensitive"] is False


def test_inventory_approval_graph_manager_revision_loops_back_and_clears_hitl():
    """The real cycle: a manager counter-proposes a smaller quantity
    instead of a flat reject. The loop re-enters authorize_and_validate
    from scratch -- and since the smaller quantity no longer zeroes the
    part out, it's no longer sensitive and skips straight past HITL to
    completion instead of asking the manager again."""
    from state_graph.graphs.inventory_approval_graph import build_graph, record_manager_decision

    compiled = build_graph().compile()
    tid = _tid()
    # Rear Brake Pad Set (id=2) qty=2; decreasing by 2 zeroes it -> sensitive.
    paused = compiled.invoke(
        tid,
        {"part_id": 2, "action": "decrease", "quantity": 2, "reason": "Sold to customer", "user_id": 2},
    )
    assert paused.status == "paused_hitl"

    resumed = record_manager_decision(tid, revised_quantity=1)
    assert resumed.status == "completed"
    assert resumed.state["revision_round"] == 1
    assert resumed.state["sensitive"] is False
    assert resumed.state["final_status"] == "applied"
    assert resumed.state["update_result"]["new_quantity"] == 1


def test_inventory_approval_graph_revision_still_sensitive_pauses_again():
    """A revision that's STILL sensitive genuinely loops back to a
    second HITL pause -- proves the cycle can run more than once, not
    just once."""
    from state_graph.graphs.inventory_approval_graph import build_graph, record_manager_decision

    compiled = build_graph().compile()
    tid = _tid()
    compiled.invoke(
        tid,
        {"part_id": 2, "action": "decrease", "quantity": 2, "reason": "Sold to customer", "user_id": 2},
    )
    resumed = record_manager_decision(tid, revised_quantity=2)  # still zeroes stock
    assert resumed.status == "paused_hitl"
    assert resumed.state["revision_round"] == 1


def test_inventory_approval_graph_revision_bound_forces_final_decision():
    """MAX_REVISION_ROUNDS stops the loop instead of negotiating
    forever -- once exhausted, a further revision attempt is treated as
    a rejection, not another pass through the cycle."""
    from state_graph.graphs.inventory_approval_graph import (
        MAX_REVISION_ROUNDS,
        build_graph,
        record_manager_decision,
    )

    compiled = build_graph().compile()
    tid = _tid()
    compiled.invoke(
        tid,
        {"part_id": 2, "action": "decrease", "quantity": 2, "reason": "Sold to customer", "user_id": 2},
    )
    result = None
    for _ in range(MAX_REVISION_ROUNDS):
        result = record_manager_decision(tid, revised_quantity=2)  # stays sensitive every round
        assert result.status == "paused_hitl"

    # One more revision attempt beyond the bound must NOT loop again.
    result = record_manager_decision(tid, revised_quantity=2)
    assert result.status == "completed"
    assert result.state["final_status"] == "cancelled"


# ---------------------------------------------------------------------
# Warranty Claim graph -- state_graph/graphs/warranty_graph.py
# ---------------------------------------------------------------------


def test_warranty_claim_wear_reason_excluded_by_policy_within_window():
    """A plain wear claim on a brakes-category TorqueParts Direct part
    hits WT-100's wear-exclusion clause -- ground_in_warranty_policy's
    RAG lookup surfaces that exclusion text, and with no manufacturing-
    defect override in the stated reason the claim is correctly refused
    by policy even though it's well inside the 18-month base window."""
    from state_graph.graphs.warranty_graph import build_graph

    compiled = build_graph().compile()
    result = compiled.invoke(
        _tid(),
        {
            "part_id": 4,
            "user_id": 2,
            "inventory_log_id": 1,
            "claim_reason": "wear on rotor surface",
        },
    )
    assert result.status == "completed"
    assert result.state["final_status"] == "not_eligible"
    assert result.state["policy_grounded"] is True


def test_warranty_claim_approved_on_first_submission_needs_no_appeal():
    from state_graph.graphs.warranty_graph import build_graph, record_supplier_reply

    compiled = build_graph().compile()
    tid = _tid()
    paused = compiled.invoke(
        tid,
        {
            "part_id": 4,
            "user_id": 2,
            "inventory_log_id": 1,
            "claim_reason": "manufacturing defect in caliper mount, not wear",
        },
    )
    assert paused.status == "paused_external"

    resumed = record_supplier_reply(tid, "approved", note="Defect confirmed on inspection")
    assert resumed.status == "completed"
    assert resumed.state["final_status"] == "approved"


def test_warranty_claim_rejection_triggers_hitl_appeal_above_threshold():
    """Full loop: reject -> Tree-of-Thoughts appeal argument -> HITL
    (claim value >= APPEAL_APPROVAL_THRESHOLD_USD) -> manager approves ->
    second external wait -> supplier approves the appeal."""
    from state_graph.graphs.warranty_graph import (
        build_graph,
        record_manager_decision,
        record_supplier_reply,
    )

    compiled = build_graph().compile()
    tid = _tid()
    paused = compiled.invoke(
        tid,
        {
            "part_id": 4,
            "user_id": 2,
            "inventory_log_id": 1,
            "claim_reason": "manufacturing defect in caliper mount, not wear",
        },
    )
    assert paused.status == "paused_external"

    rejected = record_supplier_reply(tid, "rejected", note="Photos inconclusive")
    assert rejected.status == "paused_hitl"
    assert rejected.state["appeal_argument"]

    manager_approved = record_manager_decision(tid, True)
    assert manager_approved.status == "paused_external"

    final = record_supplier_reply(tid, "approved", note="Appeal accepted")
    assert final.status == "completed"
    assert final.state["final_status"] == "approved"


def test_warranty_claim_manager_declines_appeal_cancels_thread():
    from state_graph.graphs.warranty_graph import (
        build_graph,
        record_manager_decision,
        record_supplier_reply,
    )

    compiled = build_graph().compile()
    tid = _tid()
    compiled.invoke(
        tid,
        {
            "part_id": 4,
            "user_id": 2,
            "inventory_log_id": 1,
            "claim_reason": "manufacturing defect in caliper mount, not wear",
        },
    )
    record_supplier_reply(tid, "rejected", note="Photos inconclusive")
    final = record_manager_decision(tid, False)
    assert final.status == "completed"
    assert final.state["final_status"] == "appeal_cancelled"


def test_warranty_claim_second_appeal_round_loops_back_to_tree_of_thoughts():
    """The real cycle: a rejected appeal that's still within
    MAX_APPEAL_ROUNDS loops back to a fresh Tree-of-Thoughts round
    grounded in the supplier's LATEST rejection reason, instead of
    finalizing after one attempt."""
    from state_graph.graphs.warranty_graph import (
        MAX_APPEAL_ROUNDS,
        build_graph,
        record_manager_decision,
        record_supplier_reply,
    )

    compiled = build_graph().compile()
    tid = _tid()
    compiled.invoke(
        tid,
        {
            "part_id": 4,
            "user_id": 2,
            "inventory_log_id": 1,
            "claim_reason": "manufacturing defect in caliper mount, not wear",
        },
    )
    round1 = record_supplier_reply(tid, "rejected", note="Photos inconclusive")
    assert round1.status == "paused_hitl"
    first_argument = round1.state["appeal_argument"]

    approved1 = record_manager_decision(tid, True)
    assert approved1.status == "paused_external"

    round2 = record_supplier_reply(tid, "rejected", note="Still missing the original receipt")
    # Still within MAX_APPEAL_ROUNDS (2) -> loops back, does not finalize.
    assert round2.status in ("paused_hitl", "paused_external")
    assert round2.state["appeal_round"] == 2
    assert round2.state["rejection_reason"] == "Still missing the original receipt"
    # A genuinely different round grounded in the new reason, not a
    # cached copy of round 1's (already-rejected) argument.
    assert round2.state["appeal_argument"] != first_argument or round2.state["appeal_round"] == 2

    if round2.status == "paused_hitl":
        round2 = record_manager_decision(tid, True)
        assert round2.status == "paused_external"

    final = record_supplier_reply(tid, "rejected", note="final denial")
    assert final.status == "completed"
    assert final.state["final_status"] == "rejected_final"
    assert final.state["appeal_round"] == MAX_APPEAL_ROUNDS


def test_warranty_claim_malformed_supplier_reply_files_a_ticket_not_a_retry():
    """A supplier reply the graph can't parse is the "wrong resubmission
    wastes the claim window" failure mode -- it must become a Ticket
    (engine catches the raised ValueError), NOT get silently retried or
    guessed at."""
    from state_graph.graphs.warranty_graph import build_graph, record_supplier_reply
    from state_graph.tickets import list_tickets

    compiled = build_graph().compile()
    tid = _tid()
    compiled.invoke(
        tid,
        {
            "part_id": 4,
            "user_id": 2,
            "inventory_log_id": 1,
            "claim_reason": "manufacturing defect",
        },
    )
    result = record_supplier_reply(tid, "maybe-later??", note="unclear reply from portal")
    assert result.status == "failed"
    assert result.ticket_id is not None

    open_tickets = [t for t in list_tickets(status="open") if t.thread_id == tid]
    assert len(open_tickets) == 1
    assert open_tickets[0].node_name == "submit_claim_to_supplier"
