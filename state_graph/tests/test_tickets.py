import uuid

import pytest

from state_graph.db import reset_db
from state_graph.engine import StateGraph, END
from state_graph.tickets import get_ticket, list_tickets, resolve_ticket


@pytest.fixture(autouse=True)
def clean_db():
    reset_db()
    yield


def _thread_id() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def test_unexpected_node_exception_files_a_ticket_and_halts_thread():
    def ok(state):
        return {"ok": True}

    def boom(state):
        raise RuntimeError("downstream service unreachable")

    g = StateGraph("ticketable")
    g.add_node("ok", ok).add_node("boom", boom)
    g.set_entry_point("ok").add_edge("ok", "boom").add_edge("boom", END)
    compiled = g.compile()

    tid = _thread_id()
    result = compiled.invoke(tid, {})

    assert result.status == "failed"
    assert result.node_name == "boom"
    assert result.ticket_id is not None

    ticket = get_ticket(result.ticket_id)
    assert ticket.thread_id == tid
    assert ticket.graph_name == "ticketable"
    assert ticket.node_name == "boom"
    assert ticket.error_type == "RuntimeError"
    assert "downstream service unreachable" in ticket.error_message
    assert ticket.status == "open"

    open_tickets = list_tickets(status="open")
    assert any(t.id == ticket.id for t in open_tickets)


def test_business_outcome_is_not_a_ticket():
    """A normal branch (e.g. 'out of stock') must NOT create a ticket --
    only genuinely unexpected exceptions should."""

    def decide(state):
        return {"outcome": "out_of_stock"}  # a normal return, not a raise

    g = StateGraph("normal_branch").add_node("decide", decide)
    g.set_entry_point("decide").add_edge("decide", END)
    compiled = g.compile()

    tid = _thread_id()
    result = compiled.invoke(tid, {})

    assert result.status == "completed"
    assert list_tickets(status="open") == [] or all(
        t.thread_id != tid for t in list_tickets(status="open")
    )


def test_resolve_ticket_updates_status_and_note():
    def boom(state):
        raise ValueError("bad input")

    g = StateGraph("resolvable").add_node("boom", boom)
    g.set_entry_point("boom").add_edge("boom", END)
    compiled = g.compile()

    result = compiled.invoke(_thread_id(), {})
    resolve_ticket(result.ticket_id, "fixed upstream data and re-ran")

    ticket = get_ticket(result.ticket_id)
    assert ticket.status == "resolved"
    assert ticket.resolution_note == "fixed upstream data and re-ran"
