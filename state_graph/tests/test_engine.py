"""Unit tests for the state graph engine itself, with tiny synthetic
graphs -- not the three real Torque Tune graphs (those are covered in
test_real_graphs.py). Keeping these synthetic keeps them fast and keeps
engine bugs from being masked by real-graph complexity."""

import uuid

import pytest

from state_graph.checkpointer import Checkpointer
from state_graph.db import reset_db
from state_graph.engine import END, StateGraph, interrupt


@pytest.fixture(autouse=True)
def clean_db():
    reset_db()
    yield


def _thread_id() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def test_linear_graph_completes_and_checkpoints_every_node():
    calls = []

    def a(state):
        calls.append("a")
        return {"a": 1}

    def b(state):
        calls.append("b")
        return {"b": 2}

    g = StateGraph("linear").add_node("a", a).add_node("b", b)
    g.set_entry_point("a").add_edge("a", "b").add_edge("b", END)
    compiled = g.compile()

    tid = _thread_id()
    result = compiled.invoke(tid, {})

    assert result.status == "completed"
    assert result.state["a"] == 1 and result.state["b"] == 2
    assert calls == ["a", "b"]

    history = Checkpointer().history(tid)
    assert [c.node_name for c in history] == ["a", "b"]
    assert [c.status for c in history] == ["running", "completed"]


def test_conditional_edge_routes_correctly():
    def choose(state):
        return {"picked": state["want"]}

    def left(state):
        return {"path": "left"}

    def right(state):
        return {"path": "right"}

    g = StateGraph("branch")
    g.add_node("choose", choose).add_node("left", left).add_node("right", right)
    g.set_entry_point("choose")
    g.add_conditional_edges("choose", lambda s: s["picked"], {"L": "left", "R": "right"})
    g.add_edge("left", END)
    g.add_edge("right", END)
    compiled = g.compile()

    r_left = compiled.invoke(_thread_id(), {"want": "L"})
    assert r_left.state["path"] == "left"

    r_right = compiled.invoke(_thread_id(), {"want": "R"})
    assert r_right.state["path"] == "right"


def test_hitl_interrupt_pauses_then_resume_continues_same_node():
    entered_gate_count = {"n": 0}

    def gate(state):
        entered_gate_count["n"] += 1
        if "human_ok" not in state:
            return interrupt("need_approval", question="ok?")
        return {"approved": state["human_ok"]}

    def finish(state):
        return {"done": True}

    g = StateGraph("hitl")
    g.add_node("gate", gate).add_node("finish", finish)
    g.set_entry_point("gate").add_edge("gate", "finish").add_edge("finish", END)
    compiled = g.compile()

    tid = _thread_id()
    r1 = compiled.invoke(tid, {})
    assert r1.status == "paused_hitl"
    assert r1.node_name == "gate"
    assert entered_gate_count["n"] == 1

    r2 = compiled.resume(tid, human_response={"human_ok": True})
    assert r2.status == "completed"
    assert r2.state["approved"] is True
    assert r2.state["done"] is True
    assert entered_gate_count["n"] == 2  # gate really did re-run on resume


def test_resume_on_completed_thread_raises():
    def a(state):
        return {}

    g = StateGraph("done").add_node("a", a)
    g.set_entry_point("a").add_edge("a", END)
    compiled = g.compile()

    tid = _thread_id()
    compiled.invoke(tid, {})
    with pytest.raises(ValueError):
        compiled.resume(tid)


def test_invoke_twice_on_same_thread_raises():
    def a(state):
        return {}

    g = StateGraph("dup").add_node("a", a)
    g.set_entry_point("a").add_edge("a", END)
    compiled = g.compile()

    tid = _thread_id()
    compiled.invoke(tid, {})
    with pytest.raises(ValueError):
        compiled.invoke(tid, {})
