# state_graph/

State-graph layer for the Final Project. Three real, recurring company
problems modeled as **state graphs** (not DAGs): each one can pause for
however long a human or an outside system takes to respond, is
checkpointed to durable storage after every meaningful transition, and
can resume on a **completely different process** from exactly where it
left off. This README is the map a grader (or a teammate) needs to find
every required concern without reading every file end to end.

Run the tests and the narrated demo from the **repo root** (the folder
that directly contains `state_graph/`, `mcp-server/`, `agent/`,
`databases/`):

```bash
PYTHONPATH=. python3 -m pytest state_graph/tests/ -q
PYTHONPATH=. python3 state_graph/run_demo.py
```

(Windows / PowerShell: `$env:PYTHONPATH="."` then `py -m pytest
state_graph\tests\ -q` and `py state_graph\run_demo.py`.)

Expected: **26 passed**, and the demo script prints five sections
end to end with no errors (see "Demo evidence" below).

---

## The three Stateful Problems

Each graph lives in `state_graph/graphs/` and is genuinely new agent
scope — none of them is a re-skin of the scheduling problem from the
Decomposition & Planning Lab or the retrieval problem from the Memory &
RAG Lab (two earlier graphs, `fulfillment_graph.py` and
`knowledge_graph.py`, were disqualified for exactly that reason and
have been removed).

| Graph | File | Why it needs a state graph, not a for-loop |
|---|---|---|
| **Purchase Order** | `graphs/purchase_order_graph.py` | Once a PO is sent it waits on a real supplier reply that can take days or never arrive — nothing in the process is blocked waiting, a thread checkpoints and stops. Committing the wrong quantity/supplier wastes real money; a retry can't undo a purchase already sent. |
| **Inventory Approval** | `graphs/inventory_approval_graph.py` | A sensitive stock change (e.g. one that zeroes out a part) must not be decided by the agent alone — it pauses for a manager who may take hours, checkpointed so the request survives a process restart instead of being lost with it. |
| **Warranty Claim** | `graphs/warranty_graph.py` | A claim's fate depends on a supplier's decision outside the model's control, and can be rejected and appealed — the wrong resubmission wastes the one real appeal window, which no single retry recovers. |

Each graph's own module docstring has the full rationale, the exact
node-by-node breakdown of its two required LLM-call techniques (Task
Decomposition, Tree of Thoughts, Constrained ReAct, or RAG — two per
graph, picked for what that specific node's job needed), and an ASCII
diagram of its shape. Read those before this file if you need the
detail; this file is the index.

| Graph | Two LLM-call techniques |
|---|---|
| Purchase Order | Task Decomposition (`decompose_into_supplier_batches` groups low-stock parts into one PO per supplier) + Constrained ReAct (`react_decide_batch_action`'s bounded `{verify_stock, draft_po, escalate_missing_contact}` action set) |
| Inventory Approval | RAG Architecture (`ground_in_policy` checks the real knowledge base for a policy exception) + Constrained ReAct (the node sequence itself is a fixed `{check_policy, request_approval, apply, reject, revise}` action set — the graph's edges *are* the constraint) |
| Warranty Claim | RAG Architecture (`ground_in_warranty_policy` grounds eligibility in `mcp-server/resources/knowledge_base/supplier_warranty_terms.md`) + Tree of Thoughts (`choose_appeal_argument` generates and scores several candidate appeal arguments, not one best-effort draft) |

---

## Real cycles (not just branches)

A DAG runs start to a topological end. Every graph here has at least
one edge that loops back to an earlier node — genuinely revisiting a
decision, not just branching once:

- **Purchase Order** — `process_next_batch` loops until every
  per-supplier batch is handled (a real multi-iteration cycle across
  however many suppliers have low stock), and
  `react_decide_batch_action` has its own bounded self-loop
  (`verify_stock` re-observes current quantities, capped at
  `MAX_REACT_STEPS = 3`, before it's allowed to reach `draft_po`).
- **Inventory Approval** — a manager can counter-propose a revised
  quantity instead of a flat reject (`apply_manager_revision`), which
  loops all the way back through `authorize_and_validate` (and,
  if still sensitive, `ground_in_policy`) with the new number — a
  smaller revision may no longer even be sensitive, in which case the
  loop skips a second HITL pause entirely. Bounded by
  `MAX_REVISION_ROUNDS = 2`.
- **Warranty Claim** — a rejected appeal that's still within
  `MAX_APPEAL_ROUNDS = 2` loops back to a fresh Tree-of-Thoughts round
  (`prepare_next_appeal_round` → `choose_appeal_argument`) grounded in
  the supplier's *latest* stated rejection reason, instead of
  finalizing after one attempt.

Every cycle above is **bounded** — a graph that could loop forever on a
stuck negotiation isn't a recovery path, it's a new failure mode. Once
the bound is hit, the graph is forced to a terminal decision
(`cancelled` / `finalize_rejected`), not another pass through the loop.

---

## Human-in-the-loop vs. external wait vs. Failure Ticket

Three genuinely different reasons a thread can stop, and this codebase
keeps them as **three different statuses** on purpose — conflating any
two of them would make it impossible for an admin's queue (or a grader)
to tell "waiting on us" apart from "waiting on someone else" apart from
"something broke":

| Status | Means | Who/what resumes it | Example |
|---|---|---|---|
| `paused_hitl` | An **expected** pause for a decision the agent isn't allowed to make alone — always part of the graph's design. | A human, through `record_manager_decision(...)` | Manager approving a $312 PO |
| `paused_external` | An **expected** pause on an outside system's reply, on a timeline the graph doesn't control and that may never arrive. | An outside reply, through `record_supplier_reply(...)` | Supplier confirming a warranty claim |
| `failed` (+ open Ticket) | An **unexpected** error the graph did not know how to handle as a normal branch — a bug, a malformed reply, a missing dependency. | A person, after fixing the root cause, via `resolve_ticket()` then `resume()` | A supplier reply the parser can't recognize at all |

This distinction is implemented, not just described: `state_graph/engine.py`'s
`Interrupt` carries a `kind` (`'hitl'` or `'external'`), and the two
helper functions `interrupt()` / `await_external()` are what a node
calls for each case — see `engine.py`'s docstring on `Interrupt` for
the full reasoning. `state_graph/tickets.py`'s module docstring spells
out the HITL-vs-Ticket distinction from the failure-handling side.

### Failure Tickets — `state_graph/tickets.py`

When a node raises an exception the graph did **not** expect as a
normal branch (a malformed tool response, a bug, a downed dependency —
never a normal business outcome like "out of stock", which is a regular
state transition, not a failure), `engine.py`'s `_run_from` catches it,
via `file_ticket()`:

1. writes a `Ticket` row (`error_type`, `error_message`, a full
   traceback, and a snapshot of the state as of just before the failing
   node),
2. checkpoints the thread as `status='failed'` so it stops advancing
   silently,
3. leaves the thread genuinely stuck until a human acts.

Ticket status is `open` → `investigating` → `resolved`
(`mark_investigating()`, `resolve_ticket()`), separate from and
never conflated with `paused_hitl`. Every graph demonstrates this path
with a real, graph-detected failure — not a manually inserted database
row — for example a malformed supplier reply in
`submit_po_to_supplier` / `submit_claim_to_supplier` /
`resubmit_appeal_to_supplier` (see each file's `ValueError` on an
unrecognized `decision` value).

---

## Checkpointing — `state_graph/checkpointer.py` + `engine.py`

Snapshot-per-step, not an execution log written after the fact:
`engine.py`'s `_run_from` calls `Checkpointer.save()` after **every**
node finishes — success, pause, or failure — writing the node just run,
the resulting status, and the **full** state dict as one row in the
`Checkpoints` table (`state_graph/db.py`). Resuming a thread means:
read the latest row for that `thread_id`, restore `state` from
`state_json`, and ask the graph what comes after that node. This is
durable storage (SQLite on disk), not in-memory — a brand new Python
process with nothing but the `thread_id` string can resume it.

**Proof it survives an actual process death, not just a pause within
the same process**: `state_graph/tests/test_crash_resume.py` spawns a
real subprocess that runs the Purchase Order graph's first node,
writes its checkpoint, and calls `os.kill(os.getpid(), SIGKILL)` on
itself — an unrecoverable crash, not a caught exception the subprocess
could clean up after. A second, completely fresh subprocess then
`resume()`s the same `thread_id` and the test asserts it continues
correctly. `run_demo.py`'s `demo_crash_and_resume()` shows the same
idea inline (throwing away the in-memory graph object between steps)
for a quick visual walkthrough; the subprocess test is the real proof.

---

## Repository layout

```
state_graph/
├── engine.py              StateGraph / CompiledGraph — cycles, conditional
│                           edges, Interrupt (kind='hitl'|'external'),
│                           checkpointing on every transition, Ticket
│                           filing on unexpected exceptions
├── checkpointer.py         Durable Checkpoints table read/write
├── tickets.py              Failure Ticket lifecycle (open/investigating/resolved)
├── db.py                   SQLite schema for Checkpoints + Tickets, reset_db()
├── bootstrap.py            One-time wiring: makes mcp-server/ importable as
│                           server/app/tools, and wires the demo database
├── run_demo.py             Narrated end-to-end walkthrough of all five concerns
├── graphs/
│   ├── purchase_order_graph.py
│   ├── inventory_approval_graph.py
│   └── warranty_graph.py
└── tests/
    ├── test_engine.py          Engine-level HITL/external/ticket/resume unit tests
    ├── test_tickets.py         Ticket lifecycle unit tests
    ├── test_crash_resume.py    Real os.kill'd subprocess crash-and-resume proof
    └── test_real_graphs.py     End-to-end tests for all three graphs, including
                                 every cycle, every HITL threshold, every ticket
                                 path, against the real seeded demo database
```

## Demo evidence

`python3 state_graph/run_demo.py` (from repo root, with `PYTHONPATH=.`)
narrates all five required concerns in one run:

1. **Purchase Order** — HITL threshold pause → manager approves →
   external wait → supplier confirms → completes.
2. **Inventory Approval** — RAG-grounded policy check → HITL pause →
   manager approves → real write through the actual MCP `update_inventory`
   tool.
3. **Warranty Claim** — RAG eligibility check → external wait →
   supplier rejects → Tree-of-Thoughts appeal → HITL threshold →
   manager approves → external wait → supplier approves → completes.
4. **Live crash-and-resume** — a thread's first checkpoint written,
   the in-memory graph object discarded (simulating a crash), a fresh
   graph object resumes the same thread from disk alone.
5. **Failure Ticket** — a deliberately broken node raises mid-run; a
   real Ticket is filed and the thread halts instead of the process
   crashing.

For the *real* (not simulated) process-kill proof, see
`tests/test_crash_resume.py::test_crash_and_resume_across_real_processes`.
