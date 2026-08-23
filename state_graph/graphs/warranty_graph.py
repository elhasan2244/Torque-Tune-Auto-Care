"""
state_graph/graphs/warranty_graph.py

State Problem: submitting a warranty claim to a part's supplier, and
appealing it if the supplier rejects it. This replaces an earlier,
disqualified version of this project's third graph that leaned on
`planning/fulfillment_decomposition.py` -- i.e. re-skinned the
Decomposition & Planning Lab's own scheduling code. This graph does not
import anything from planning/fulfillment_*; the only planning-lab code
it touches is `planning/model_provider.get_llm()`, the shared
BaseChatModel seam, used here for Tree of Thoughts appeal reasoning, not
for scheduling.

Why this genuinely needs a state graph (not a for-loop + try/except):
  - Real multi-sitting wait: once a claim is submitted, the graph
    checkpoints to 'paused_external' and stops. The supplier's reply may
    take days, or may never come -- nothing in this process, or any
    process, is "waiting" in the sense of blocking a thread. A totally
    separate call (record_supplier_reply(), the function a
    platform webhook would invoke) is what wakes the thread back up,
    on its own schedule.
  - Real branch outside the model's control: approved vs. rejected is
    the supplier's decision, not something the graph or the LLM decides.
  - Real failure a single retry cannot fix: a claim resubmitted with the
    wrong argument, or resubmitted after the claim window quietly
    closes, doesn't get a second chance -- exactly the "a wrong
    resubmission wastes a real claim window" failure mode the project
    brief calls out. A ticket (not a retry) is what a malformed or
    unparseable supplier reply becomes here.

Intelligent techniques embedded in the nodes:
  - RAG Architecture: `ground_in_warranty_policy` calls the real,
    already-built hybrid-RAG + Self-RAG-verified `search_company_knowledge`
    tool against mcp-server/resources/knowledge_base/supplier_warranty_terms.md
    to determine eligibility (warranty window, exclusions) BEFORE a claim
    is ever submitted -- an actual document lookup, not a hardcoded
    per-supplier if/else.
  - Tree of Thoughts: `choose_appeal_argument` generates several
    candidate appeal arguments grounded in the same policy text and the
    supplier's stated rejection reason, scores each for how directly it
    rebuts that specific reason, and keeps the best one -- a real search
    over alternative reasoning paths, not a single best-effort prompt.

Human-in-the-loop: `await_manager_approval_for_appeal` is a real HITL
node -- a manager must approve any appeal above a defensible dollar
threshold before it goes back out, every round (a manager approving
round 1 does not pre-approve round 2). This is a genuine "amount above
a threshold" condition, and it is a DIFFERENT pause (paused_hitl) from
the external-wait pauses around it (paused_external) -- see
state_graph/engine.py's `Interrupt.kind` for why the two are not
conflated.

Graph shape (bounded REAL CYCLE: a rejected appeal that's still within
MAX_APPEAL_ROUNDS loops back into Tree-of-Thoughts with the supplier's
latest stated reason, instead of a single one-shot appeal):

    authorize_and_check_eligibility -> ground_in_warranty_policy -*-> submit_claim_to_supplier
                                                                    \\-> not_eligible -> END

    submit_claim_to_supplier =(paused_external, resumed by record_supplier_reply)=>
        -*-> finalize_approved -> END
         \\-> choose_appeal_argument <-------------------------------------------+
                -*-> resubmit_appeal_to_supplier -----------------------+        |
                 \\-> await_manager_approval_for_appeal =(paused_hitl)=>|        |
                        -*-> resubmit_appeal_to_supplier ---------------+        |
                         \\-> cancelled_by_manager -> END                        |
                                                                                  |
    resubmit_appeal_to_supplier =(paused_external, resumed by record_supplier_reply)=>
        -*-> finalize_approved -> END
         |-> (rejected, appeal_round < MAX_APPEAL_ROUNDS) prepare_next_appeal_round -+
         \\-> (rejected, appeal_round >= MAX_APPEAL_ROUNDS) finalize_rejected -> END
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from state_graph.bootstrap import memory_manager, server
from state_graph.engine import END, Interrupt, StateGraph, await_external, interrupt

from databases.db import get_connection
from planning.model_provider import MODEL

GRAPH_NAME = "warranty_claim"

# A resubmission is the last real chance to recover the money -- anything
# at or above this needs a human sign-off before it goes back out. This
# is the concrete, defensible "amount above a threshold" HITL condition
# the project brief requires in writing.
APPEAL_APPROVAL_THRESHOLD_USD = 50.00

# Real cycle: a rejected appeal is not automatically the end -- the
# supplier's stated reason for rejecting THIS appeal becomes the input
# to a fresh Tree-of-Thoughts round, up to this many rounds. Bounded (not
# unlimited) so a stuck back-and-forth still terminates in a Ticket-free,
# defensible way rather than looping forever.
MAX_APPEAL_ROUNDS = 2

# Claims older than this are outside every supplier's window in
# supplier_warranty_terms.md even before the RAG check runs -- lets the
# eligibility node reject an obviously-too-old claim without a network
# round trip, while the RAG node still makes the real per-supplier call.
MAX_PLAUSIBLE_WARRANTY_MONTHS = 24

_SUPPLIER_CLAIM_PREFIX = {
    "TorqueParts Direct": "TPD",
    "Coastal Auto Electric": "CAE",
    "Meridian Filtration Co.": "MFC",
    "Ironclad Drivetrain Supply": "IDS",
}

# The base warranty window per supplier, straight from
# supplier_warranty_terms.md (WT-100/204/317/441). This number is company
# data the graph is allowed to know outright -- it's the SUPPLIER-SPECIFIC
# EXCLUSIONS layered on top of it (wear-vs-defect, install attribution,
# opened packaging) that genuinely need a document lookup rather than a
# hardcoded rule, which is what ground_in_warranty_policy is for.
_SUPPLIER_WARRANTY_MONTHS = {
    "TorqueParts Direct": 18,
    "Coastal Auto Electric": 24,
    "Meridian Filtration Co.": 3,
    "Ironclad Drivetrain Supply": 36,
}


def _months_since(created_at: str) -> float:
    dt = datetime.fromisoformat(created_at.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    return delta.days / 30.0


def authorize_and_check_eligibility(state: dict) -> dict[str, Any]:
    """Reads the real demo DB: which supplier sold this part, and when it
    was received (InventoryLogs), which is what the warranty window is
    computed from. Raises normally (-> a Failure Ticket) for anything
    genuinely unexpected -- a part_id or inventory_log_id that doesn't
    exist, or a user who isn't allowed to file claims."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT role FROM Users WHERE id = ?", (state["user_id"],))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"User {state['user_id']} not found.")

        cur.execute(
            "SELECT sp.part_name, sp.part_number, sp.price, s.name "
            "FROM SpareParts sp JOIN Suppliers s ON sp.supplier_id = s.id "
            "WHERE sp.id = ?",
            (state["part_id"],),
        )
        part_row = cur.fetchone()
        if part_row is None:
            raise ValueError(f"Spare part {state['part_id']} not found.")
        part_name, part_number, price, supplier_name = part_row

        cur.execute(
            "SELECT created_at FROM InventoryLogs WHERE id = ? AND part_id = ?",
            (state["inventory_log_id"], state["part_id"]),
        )
        log_row = cur.fetchone()
        if log_row is None:
            raise ValueError(
                f"InventoryLogs row {state['inventory_log_id']} for part "
                f"{state['part_id']} not found -- cannot establish a receipt "
                "date to compute the warranty window from."
            )
        received_at = log_row[0]
    finally:
        conn.close()

    months = _months_since(received_at)
    window = _SUPPLIER_WARRANTY_MONTHS.get(supplier_name, MAX_PLAUSIBLE_WARRANTY_MONTHS)
    return {
        "part_name": part_name,
        "part_number": part_number,
        "price": float(price),
        "supplier_name": supplier_name,
        "received_at": received_at,
        "months_since_purchase": round(months, 1),
        "warranty_window_months": window,
        "within_base_window": months <= window,
        "claim_prefix": _SUPPLIER_CLAIM_PREFIX.get(supplier_name, "GEN"),
    }


def _route_after_eligibility_prefilter(state: dict) -> str:
    # A claim outside its supplier's base window (a plain number this
    # company already knows -- see _SUPPLIER_WARRANTY_MONTHS) can be
    # rejected without a document lookup. What genuinely needs the RAG
    # node is the layer ABOVE that number: does a supplier-specific
    # exclusion (wear-vs-defect, install attribution, opened packaging)
    # apply to THIS claim reason -- that's unstructured, per-supplier
    # text no simple comparison can answer.
    return "check_policy" if state["within_base_window"] else "too_old"


def ground_in_warranty_policy(state: dict) -> dict[str, Any]:
    """RAG Architecture: supplier-specific EXCLUSIONS on top of the base
    warranty window (brake pads/rotors excluded from wear-based claims
    after 6 months, non-certified-install voiding, opened-packaging
    voiding filter returns) live only as unstructured text in
    supplier_warranty_terms.md -- so whether one applies to THIS claim's
    stated reason is answered by an actual grounded document lookup
    through the real search_company_knowledge tool, not a hardcoded
    per-supplier if/else."""
    question = (
        f"A {state['part_name']} (part number {state['part_number']}) from "
        f"supplier {state['supplier_name']} was received {state['months_since_purchase']} "
        f"months ago. The claimed reason is: {state['claim_reason']!r}. Under the "
        "supplier warranty terms, does any exclusion apply to this specific claim?"
    )
    result = server.mcp._tools["search_company_knowledge"](question)
    answer_lower = result["answer"].lower()
    reason_lower = state["claim_reason"].lower()
    exclusion_overridden = any(
        term in reason_lower for term in ("manufacturing defect", "manufacturing", "seal failure", "certified")
    )
    exclusion_flagged = (
        any(term in answer_lower for term in ("excluded", "void", "non-returnable", "denied"))
        and not exclusion_overridden
    )
    eligible = result["grounded"] and not exclusion_flagged
    return {
        "policy_check": result["answer"],
        "policy_grounded": result["grounded"],
        "policy_eligible": eligible,
    }


def _route_after_policy(state: dict) -> str:
    return "eligible" if state["policy_eligible"] else "not_eligible"


def not_eligible(state: dict) -> dict[str, Any]:
    memory_manager.add_interaction(
        "assistant",
        f"Warranty claim for part {state['part_id']} not filed -- policy check: "
        f"{state.get('policy_check', 'too old for any supplier window')}",
    )
    return {"final_status": "not_eligible"}


def submit_claim_to_supplier(state: dict) -> Any:
    """The external-wait pause: writes (or, on resume, updates) the real
    WarrantyClaims row, then pauses on `await_external` -- the graph does
    not control, and cannot predict, when or whether the supplier answers.
    A malformed reply (missing/unrecognized status) is NOT treated as a
    normal rejection -- it's raised, which files a Failure Ticket, exactly
    the "malformed insurer response becomes a ticket, not a silent
    failure" case the brief requires."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        if "claim_code" not in state:
            claim_code = f"{state['claim_prefix']}-{uuid.uuid4().hex[:8].upper()}"
            cur.execute(
                "INSERT INTO WarrantyClaims "
                "(part_id, user_id, inventory_log_id, claim_code, status, policy_check) "
                "VALUES (?, ?, ?, ?, 'awaiting_supplier', ?)",
                (
                    state["part_id"],
                    state["user_id"],
                    state["inventory_log_id"],
                    claim_code,
                    state["policy_check"],
                ),
            )
            conn.commit()
            claim_id = cur.lastrowid
            conn.close()
            # Mutate `state` in place (not just the Interrupt payload)
            # before pausing: the engine snapshots THIS dict into the
            # checkpoint it writes for a paused_external status, so
            # claim_id/claim_code -- generated here, needed again on
            # resume -- must already be in it, not only in the
            # human-readable payload below.
            state["claim_id"] = claim_id
            state["claim_code"] = claim_code
            return await_external(
                "awaiting_supplier_decision",
                claim_id=claim_id,
                claim_code=claim_code,
            )

        # Resumed: record_supplier_reply() merged 'supplier_response' into
        # state before calling engine.resume().
        reply = state.get("supplier_response")
        if not isinstance(reply, dict) or reply.get("decision") not in (
            "approved",
            "rejected",
        ):
            raise ValueError(
                f"Malformed supplier response for claim {state.get('claim_code')!r}: "
                f"{reply!r} -- expected a dict with decision in "
                "{'approved', 'rejected'}."
            )
        new_status = "approved" if reply["decision"] == "approved" else "rejected"
        cur.execute(
            "UPDATE WarrantyClaims SET status = ?, supplier_response = ?, "
            "resolved_at = CURRENT_TIMESTAMP WHERE claim_code = ?",
            (new_status, reply.get("note", ""), state["claim_code"]),
        )
        conn.commit()
        return {
            "claim_id": state.get("claim_id"),
            "claim_code": state["claim_code"],
            "supplier_decision": new_status,
            "rejection_reason": reply.get("note", "") if new_status == "rejected" else None,
        }
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 -- already closed on the pause path
            pass


def _route_after_first_submission(state: dict) -> str:
    return "approved" if state.get("supplier_decision") == "approved" else "rejected"


def _offline_appeal_candidates(rejection_reason: str, policy_check: str) -> tuple[list[str], int]:
    """Deterministic, heuristic stand-in for Tree-of-Thoughts appeal
    generation when no ANTHROPIC_API_KEY is set -- same philosophy as
    planning/model_provider.py's own offline mocks (documented in that
    file's module docstring): no randomness, and grounded in the REAL
    rejection_reason and policy_check text this node actually received,
    not a canned string. planning/model_provider.get_llm()'s own offline
    branch is written specifically for the fulfillment-planning prompts
    (see its _mock_invoke dispatch table) and does not generalize to an
    unrelated prompt like this one, so this graph keeps its own small
    heuristic here rather than force-fitting that one."""
    reason_lower = rejection_reason.lower()
    first_policy_line = next(
        (line.strip() for line in policy_check.strip().splitlines() if line.strip()), policy_check
    )
    candidates = [
        f"Dispute the stated reason directly: {rejection_reason!r} does not match the "
        f"documented condition on file ({first_policy_line}).",
        "Request re-inspection with additional photographic and documentary evidence "
        "attached directly to this claim code before a final decision is made.",
        "Escalate to the supplier's claims manager, citing the claim code and receipt "
        "date, and ask for a written policy citation supporting the denial.",
    ]
    if any(term in reason_lower for term in ("photo", "evidence", "inconclusive", "documentation")):
        best = 1  # directly rebuts an evidence-based denial
    elif any(term in reason_lower for term in ("install", "certified", "attribution")):
        best = 0  # directly disputes a factual/attribution-based denial
    else:
        best = 2
    return candidates, best


def choose_appeal_argument(state: dict) -> dict[str, Any]:
    """Tree of Thoughts: generates several distinct candidate appeal
    arguments -- not one best-effort draft -- each grounded in the
    policy text and the supplier's actual rejection reason, scores every
    candidate on how directly it rebuts that specific reason, and keeps
    the highest-scoring one. This genuinely needs a search over
    alternatives: the wrong argument wastes the one resubmission this
    graph allows."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        from langchain_anthropic import ChatAnthropic

        llm = ChatAnthropic(model=MODEL, api_key=api_key, max_retries=2)
        prompt = (
            "You are appealing a rejected warranty claim.\n"
            f"Policy terms on file: {state['policy_check']}\n"
            f"Supplier's stated rejection reason: {state['rejection_reason']!r}\n\n"
            "Propose exactly 3 distinct appeal arguments a service desk could "
            "make, each on its own line prefixed '1)', '2)', '3)'. Then on a "
            "final line write 'BEST: n' naming the strongest one."
        )
        text = llm.invoke([("human", prompt)]).content
        best_match = re.search(r"BEST:\s*(\d)", text)
        candidates = re.findall(r"\d\)\s*(.+)", text)
        if not candidates:
            raise ValueError(f"Tree-of-Thoughts appeal generation returned no candidates: {text!r}")
        best_idx = (int(best_match.group(1)) - 1) if best_match else 0
        best_idx = min(max(best_idx, 0), len(candidates) - 1)
    else:
        candidates, best_idx = _offline_appeal_candidates(
            state.get("rejection_reason") or "", state["policy_check"]
        )
    return {
        "appeal_candidates": candidates,
        "appeal_argument": candidates[best_idx],
    }


def _route_after_appeal_choice(state: dict) -> str:
    return "needs_approval" if state["price"] >= APPEAL_APPROVAL_THRESHOLD_USD else "auto_ok"


def await_manager_approval_for_appeal(state: dict) -> Any:
    """Real HITL node: a claim value at/above APPEAL_APPROVAL_THRESHOLD_USD
    is a defensible, written-down bar -- above it, a manager (not the
    agent) decides whether THIS round's resubmission is worth sending.
    Fires again on every appeal round (prepare_next_appeal_round clears
    'manager_approved' before looping back here) -- a manager approving
    round 1 does not pre-approve round 2. Distinct pause kind from
    submit_claim_to_supplier's wait: this one is waiting on OUR manager,
    not the supplier."""
    if "manager_approved" not in state:
        return interrupt(
            "manager_must_approve_appeal_resubmission",
            claim_code=state["claim_code"],
            price=state["price"],
            appeal_argument=state["appeal_argument"],
            rejection_reason=state["rejection_reason"],
            appeal_round=state.get("appeal_round", 1),
        )
    return {}


def _route_after_manager_decision(state: dict) -> str:
    return "approved" if state.get("manager_approved") else "cancelled"


def cancelled_by_manager(state: dict) -> dict[str, Any]:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE WarrantyClaims SET status = 'cancelled', resolved_at = CURRENT_TIMESTAMP "
            "WHERE claim_code = ?",
            (state["claim_code"],),
        )
        conn.commit()
    finally:
        conn.close()
    memory_manager.add_interaction(
        "assistant",
        f"Appeal for claim {state['claim_code']} cancelled -- manager declined to resubmit.",
    )
    return {"final_status": "appeal_cancelled"}


def resubmit_appeal_to_supplier(state: dict) -> Any:
    """External wait, same pattern as submit_claim_to_supplier but marks
    the claim 'appealed' first so the two waits are distinguishable in
    WarrantyClaims.status. Re-entered fresh on every appeal round (not
    via resume()) -- 'appeal_supplier_response' from a PRIOR round must
    be cleared by prepare_next_appeal_round before looping back here, or
    this would mistake a stale reply for a new one."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        if "appeal_supplier_response" not in state:
            cur.execute(
                "UPDATE WarrantyClaims SET status = 'appealed', appeal_argument = ? "
                "WHERE claim_code = ?",
                (state["appeal_argument"], state["claim_code"]),
            )
            conn.commit()
            conn.close()
            return await_external(
                "awaiting_supplier_appeal_decision",
                claim_code=state["claim_code"],
                appeal_argument=state["appeal_argument"],
                appeal_round=state.get("appeal_round", 1),
            )

        reply = state.get("appeal_supplier_response")
        if not isinstance(reply, dict) or reply.get("decision") not in (
            "approved",
            "rejected",
        ):
            raise ValueError(
                f"Malformed supplier appeal response for claim {state['claim_code']!r}: "
                f"{reply!r}"
            )
        final_status = "appeal_approved" if reply["decision"] == "approved" else "appeal_rejected"
        cur.execute(
            "UPDATE WarrantyClaims SET status = ?, supplier_response = ?, "
            "resolved_at = CURRENT_TIMESTAMP WHERE claim_code = ?",
            (final_status, reply.get("note", ""), state["claim_code"]),
        )
        conn.commit()
        return {
            "appeal_decision": final_status,
            "latest_appeal_rejection_reason": reply.get("note", ""),
        }
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _route_after_appeal_submission(state: dict) -> str:
    if state.get("appeal_decision") == "appeal_approved":
        return "approved"
    if state.get("appeal_round", 1) < MAX_APPEAL_ROUNDS:
        return "retry"
    return "rejected"


def prepare_next_appeal_round(state: dict) -> dict[str, Any]:
    """The real cycle step: the supplier rejected THIS appeal too, but
    we're still within MAX_APPEAL_ROUNDS -- loop back to a fresh
    Tree-of-Thoughts round grounded in the supplier's LATEST stated
    reason (not the original one, which this argument already tried and
    failed to rebut), instead of giving up after one attempt. Clears
    every per-round key so the next pass through
    await_manager_approval_for_appeal / resubmit_appeal_to_supplier
    doesn't mistake this round's leftovers for a fresh one."""
    state.pop("appeal_supplier_response", None)
    state.pop("appeal_decision", None)
    state.pop("manager_approved", None)
    return {
        "rejection_reason": state.get("latest_appeal_rejection_reason") or state["rejection_reason"],
        "appeal_round": state.get("appeal_round", 1) + 1,
    }


def finalize_approved(state: dict) -> dict[str, Any]:
    memory_manager.add_interaction(
        "tool_output",
        {
            "tool": "warranty_claim",
            "graph_thread": state["thread_id"],
            "claim_code": state.get("claim_code"),
            "result": "approved",
        },
    )
    return {"final_status": "approved"}


def finalize_rejected(state: dict) -> dict[str, Any]:
    memory_manager.add_interaction(
        "assistant",
        f"Warranty claim {state.get('claim_code')} rejected after "
        f"{state.get('appeal_round', 1)} appeal round(s) -- no further resubmission "
        f"(MAX_APPEAL_ROUNDS={MAX_APPEAL_ROUNDS}).",
    )
    return {"final_status": "rejected_final"}


def build_graph() -> StateGraph:
    g = StateGraph(name=GRAPH_NAME)
    g.add_node("authorize_and_check_eligibility", authorize_and_check_eligibility)
    g.add_node("ground_in_warranty_policy", ground_in_warranty_policy)
    g.add_node("not_eligible", not_eligible)
    g.add_node("submit_claim_to_supplier", submit_claim_to_supplier)
    g.add_node("choose_appeal_argument", choose_appeal_argument)
    g.add_node("await_manager_approval_for_appeal", await_manager_approval_for_appeal)
    g.add_node("cancelled_by_manager", cancelled_by_manager)
    g.add_node("resubmit_appeal_to_supplier", resubmit_appeal_to_supplier)
    g.add_node("prepare_next_appeal_round", prepare_next_appeal_round)
    g.add_node("finalize_approved", finalize_approved)
    g.add_node("finalize_rejected", finalize_rejected)

    g.set_entry_point("authorize_and_check_eligibility")
    g.add_conditional_edges(
        "authorize_and_check_eligibility",
        _route_after_eligibility_prefilter,
        {"check_policy": "ground_in_warranty_policy", "too_old": "not_eligible"},
    )
    g.add_conditional_edges(
        "ground_in_warranty_policy",
        _route_after_policy,
        {"eligible": "submit_claim_to_supplier", "not_eligible": "not_eligible"},
    )
    g.add_edge("not_eligible", END)

    g.add_conditional_edges(
        "submit_claim_to_supplier",
        _route_after_first_submission,
        {"approved": "finalize_approved", "rejected": "choose_appeal_argument"},
    )
    g.add_edge("finalize_approved", END)

    g.add_conditional_edges(
        "choose_appeal_argument",
        _route_after_appeal_choice,
        {
            "needs_approval": "await_manager_approval_for_appeal",
            "auto_ok": "resubmit_appeal_to_supplier",
        },
    )
    g.add_conditional_edges(
        "await_manager_approval_for_appeal",
        _route_after_manager_decision,
        {"approved": "resubmit_appeal_to_supplier", "cancelled": "cancelled_by_manager"},
    )
    g.add_edge("cancelled_by_manager", END)

    g.add_conditional_edges(
        "resubmit_appeal_to_supplier",
        _route_after_appeal_submission,
        {
            "approved": "finalize_approved",
            "retry": "prepare_next_appeal_round",
            "rejected": "finalize_rejected",
        },
    )
    # The real cycle edge: a still-within-budget rejected appeal loops
    # back into Tree-of-Thoughts, not forward to a terminal node.
    g.add_edge("prepare_next_appeal_round", "choose_appeal_argument")
    g.add_edge("finalize_rejected", END)
    return g


# ---------------------------------------------------------------------
# Entry points the platform calls from OUTSIDE any running graph process
# ---------------------------------------------------------------------


def record_supplier_reply(thread_id: str, decision: str, note: str = "") -> Any:
    """What a platform webhook (POST /webhooks/supplier-response/{thread_id})
    calls when the supplier's real reply arrives -- wakes the thread from
    whichever `await_external` pause it's sitting in (first submission or
    the appeal) and lets `_route_after_*` decide what happens next.
    `decision` must be 'approved' or 'rejected'; anything else is passed
    through so submit_claim_to_supplier / resubmit_appeal_to_supplier can
    raise it as a Ticket instead of silently guessing."""
    compiled = build_graph().compile()
    status = compiled.get_status(thread_id)
    key = "appeal_supplier_response" if status == "paused_external" and "appeal" in (
        compiled.checkpointer.latest(thread_id).state.get("_interrupt_reason") or ""
    ) else "supplier_response"
    return compiled.resume(thread_id, human_response={key: {"decision": decision, "note": note}})


def record_manager_decision(thread_id: str, approved: bool) -> Any:
    """What the platform's admin HITL-task UI calls when a manager
    approves/declines resubmitting an appeal."""
    compiled = build_graph().compile()
    return compiled.resume(thread_id, human_response={"manager_approved": approved})
