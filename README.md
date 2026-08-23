# Torque Tune Auto Care

An **MCP-based Spare Parts Inventory Management System** for automotive repair
businesses, extended with a long-term **memory system**, a **grounded
retrieval (RAG) layer**, a **task decomposition & planning module**, and a set
of **durable, resumable state graphs** for long-running business workflows.

## Description

Torque Tune Auto Care exposes spare-parts inventory operations (search, stock
checks, alternatives, updates, reporting) through an MCP (Model Context
Protocol) server backed by a real relational database, with role-based
authorization, input validation, human confirmation for sensitive changes,
notifications, and progress reporting.

On top of that base inventory system, the project solves problems a simple
tool-calling agent cannot:

- **Session amnesia** — a technician has to re-explain a customer's contact
  preference or a previously declined repair on every call because nothing
  the agent knew persisted once the session ended. A **memory system**
  (short-term buffer, episodic memory, and consolidated semantic memory)
  closes this gap.
- **Hallucinated answers from unstructured knowledge** — warranty terms,
  technical service bulletins, and diagnostic procedures live in Markdown
  documents, not the database, and naively asking a model to answer from them
  risks fabrication. A **RAG layer** (naive, hybrid, and agentic retrieval,
  verified with a Self-RAG-style check) grounds answers in the real
  knowledge base.
- **Multi-step fulfillment with real branching** — preparing spare parts for
  a repair job when a required part is out of stock requires evaluating
  alternatives and their trade-offs. A **planning module** applies
  decomposition, lookahead search, and self-correction algorithms to this
  problem.
- **Long-running, human-gated business processes** — purchase orders,
  inventory-change approvals, and warranty claims can pause for days waiting
  on a supplier or a manager, and must survive process restarts. A
  **state-graph engine** with SQL-backed checkpointing models these as
  resumable workflows instead of blocking calls.

## Key Features

**Core inventory system (MCP server)**

- 🔍 Search for spare parts by name
- 📦 Check spare-part stock levels
- 🔄 Suggest alternative spare parts
- ➕ Add new spare parts, ✏️ update quantities (increase/decrease), 🗑️ delete parts
- 📊 Generate inventory reports with real-time progress reporting
- 🔐 Role-based authorization and server-side role lookup for inventory changes
- ✅ Input validation for all write operations
- 💬 MCP Elicitation (human-in-the-loop confirmation) for sensitive stock changes
- 🔔 Inventory change notifications
- 🤝 MCP capability negotiation (`initialize`/`initialized` handshake)
- 👁️ Role-based tool visibility
- 🧪 Automated test suite (`pytest`)

**Memory extension**

- 🧠 Short-term rolling buffer plus a separate scratchpad for active plans
- 🚦 LLM-backed (or mocked) promote-or-drop routing on buffer overflow
- 📚 Episodic memory (chronological ledger) and semantic memory (versioned,
  consolidated facts), written by two clearly separate paths
- ♻️ Periodic semantic consolidation with conflict resolution, versioning,
  and fact expiration

**RAG extension**

- 🔎 A real vector store (HNSW ANN index + metadata payload store + metadata
  pre-filtering)
- 🧩 Three retrieval architectures: naive, hybrid (vector + BM25), and
  agentic (multi-hop)
- ✅ Self-RAG-style verification: relevance-checks retrieved chunks and
  support-checks the generated answer before it is shown to the user

**Planning extension**

- 🧭 Decomposition-first and dynamic decomposition strategies for spare-parts
  fulfillment plans
- 🌳 Plan-and-Solve and Tree-of-Thoughts search for comparing candidate
  alternatives
- 🎯 Grounded LATS (Language Agent Tree Search) for high-impact proceed/delay
  decisions
- 🔁 Self-correction via Reflexion and Self-Refine
- 📈 A reproducible evaluation harness comparing all methods

**State-graph extension**

- 🛒 **Purchase Order** graph — batches low-stock parts per supplier, waits
  (possibly for days) for supplier confirmation, with manager approval above
  a cost threshold
- 🔧 **Inventory Approval** graph — pauses sensitive stock changes for
  manager sign-off, grounded in company policy via RAG, with revision loops
- 🛡️ **Warranty Claim** graph — grounds claim eligibility in supplier
  warranty terms, generates Tree-of-Thoughts appeal arguments, and loops
  through bounded appeal rounds
- 💾 Durable, database-backed checkpointing so a workflow can resume from a
  completely different process after a crash
- 🎫 Failure-ticket filing when a node raises mid-run, instead of crashing the
  whole process

**Experimental UI**

- 🖥️ A Streamlit-based platform (`platform_streamlit/Home.py`) listing the
  Memory/RAG agent, Planning agent, and the three state-graph workflows

## Tech Stack

| Category | Technology |
|---|---|
| Language | Python 3 |
| Protocol | Model Context Protocol (MCP) — `mcp-server/fastmcp.py` (local FastMCP-style implementation) |
| Database | SQL Server-oriented schema (`databases/schema.sql`), with a SQLite demo database for local runs (`agent/demo_db.py`) |
| LLM provider | Anthropic Claude via the `anthropic` SDK (optional; falls back to documented mocks) |
| Planning models | `langchain-anthropic` / `langchain-core` (`ChatAnthropic`), offline fallback otherwise |
| Vector search | `numpy`, `scikit-learn` (TF-IDF + Truncated SVD embeddings), `hnswlib`-style HNSW ANN index |
| Keyword search | `rank_bm25` |
| DAG/graph utilities | `networkx` (used by the vendored planning toolkit) |
| Validation | `pydantic` |
| Config | `python-dotenv` |
| Testing | `pytest` |
| UI (experimental) | `streamlit` |

## Project Structure

```text
mcp-server/
  tools/            # read_tools.py, write_tools.py — the MCP tools
  auth/             # authorization.py
  validation/       # schemas.py, validators.py
  elicitation/       # human-confirmation flow
  notifications/    # notifier.py
  progress/         # progress.py
  negotiation/      # capability negotiation (initialize/initialized)
  resources/        # static resources + resources/knowledge_base/*.md (RAG corpus)
  memory/           # short-term buffer, scratchpad, episodic + semantic
                     # memory, promote-or-drop router, consolidation job
  rag/              # chunking, embeddings, vector store, naive/hybrid/
                     # agentic RAG, Self-RAG check, LLM client seam
  tool_registry.py  # enable/disable tools per agent
  app.py            # FastMCP app instance + MemoryManager wiring
  server.py         # registers tools/resources/negotiation, entry point
  client.py         # example MCP client
  config.py         # server name, DB path, roles, stock threshold

planning/
  vendor/planning_lab/          # vendored decomposition & planning toolkit
  model_provider.py             # real Claude / offline model seam
  fulfillment_decomposition.py  # decomposition-first & dynamic decomposition
  fulfillment_planning.py       # planning entry points
  self_correction.py            # Reflexion / Self-Refine
  routing.py                    # routes sub-tasks to the right algorithm
  grounded_environment.py       # database-grounded environment for LATS
  fulfillment_demo.py           # runnable demo script
  tests/                        # planning-specific tests

planning_eval/
  scenarios.py, metrics.py, harness.py, run_eval.py   # evaluation harness
  results/                                             # generated comparison tables & run artifacts

state_graph/
  graphs/
    purchase_order_graph.py
    inventory_approval_graph.py
    warranty_graph.py
  engine.py          # graph execution engine
  checkpointer.py    # durable checkpointing
  tickets.py         # failure-ticket handling
  bootstrap.py       # wiring helper
  db.py              # state-graph persistence
  run_demo.py        # narrated end-to-end demo
  tests/             # engine, crash/resume, and graph tests

context_eval/        # context-window-management strategy comparison + tests
retrieval_eval/      # retrieval-architecture comparison + test questions
agent/               # agent/client.py (CLI MCP client demo), agent/demo_db.py (seeded SQLite DB)
databases/           # schema.sql, seed.sql, erd.mmd, db.py
platform_streamlit/  # Home.py — experimental Streamlit UI
mcp/                 # shim package exposing mcp-server/ as `mcp`
tests/               # top-level automated test suite
requirements.txt
.env.example
```

## Prerequisites

- Python 3.10+ (recommended)
- `pip` for installing dependencies
- A relational database if you intend to run against a real backend
  (the schema targets SQL Server); a seeded **SQLite** demo database is
  provided for local development and requires no external setup
- An **Anthropic API key** (optional) — without it, the memory router,
  semantic consolidation, all RAG architectures, and the planning module run
  against documented, deterministic offline mocks

## Installation

```bash
# 1. Clone the repository
git clone <repository-url>
cd Torque-Tune-Auto-Care

# 2. (Recommended) create and activate a virtual environment
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the environment template
cp .env.example .env
```

## Environment Variables

All variables are defined in [`.env.example`](.env.example). Copy it to
`.env` and fill in real values locally — `.env` is git-ignored and must
never be committed.

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | No | If unset, the memory router, semantic consolidation, all RAG architectures, and the planning module fall back to documented offline mocks instead of calling the real Claude API. |
| `DB_CONNECTION_STRING` | No | Only needed if `databases/db.py` is pointed at a real SQL Server instance instead of the seeded SQLite demo database. |
| `PLANNING_INPUT_USD_PER_1M` | No | Price (USD per 1M input tokens) for the model used in the planning evaluation, used to populate the Cost column when live usage metadata is available. |
| `PLANNING_OUTPUT_USD_PER_1M` | No | Price (USD per 1M output tokens), same purpose as above. |

`.env.example` template:

```env
# Copy this file to .env and fill in real values. .env is gitignored --
# never commit real credentials.

# Optional. If unset, the memory router, semantic consolidation,
# naive/hybrid/agentic RAG, Self-RAG verification, and the planning module
# fall back to documented mock responders instead of calling the real
# Claude API.
ANTHROPIC_API_KEY=

# Only needed if databases/db.py is pointed at a real SQL Server instance
# instead of the seeded SQLite demo database (agent/demo_db.py).
# DB_CONNECTION_STRING=

# Optional planning evaluation pricing. Set these to the actual Claude model
# price you are using (USD per 1M input/output tokens) to populate the
# comparison table's Cost column when real usage metadata is available.
PLANNING_INPUT_USD_PER_1M=
PLANNING_OUTPUT_USD_PER_1M=
```

## Usage

### End-to-end MCP agent demo

Runs capability negotiation, tool discovery, a read call, a role change,
a resource read, an elicitation-gated write, and a progress-reporting call,
finishing with a memory write and consolidation pass:

```bash
python agent/client.py
```

### Memory consolidation in isolation

Runs the promote-or-drop router and semantic consolidation, including a
real resolved conflict:

```bash
python mcp-server/memory/run_consolidation.py
```

### Planning demo

Shows decomposition-first and dynamic decomposition diverging on a concrete
fulfillment scenario:

```bash
python planning/fulfillment_demo.py
```

### State-graph demo

Runs the purchase-order, inventory-approval, and warranty-claim graphs end
to end, including a live crash-and-resume simulation and a deliberately
failing node:

```bash
PYTHONPATH=. python3 state_graph/run_demo.py
```

### MCP server (standalone)

```bash
python mcp-server/server.py
```

### Experimental Streamlit platform

```bash
streamlit run platform_streamlit/Home.py
```

> `streamlit` is imported by `platform_streamlit/Home.py` but is not listed
> in `requirements.txt` — install it separately (`pip install streamlit`) if
> you want to run this UI.

## Available Scripts / Commands

| Command | Purpose |
|---|---|
| `python agent/client.py` | End-to-end MCP client demo against the seeded database |
| `python mcp-server/server.py` | Start the MCP server standalone |
| `python mcp-server/memory/run_consolidation.py` | Run promote-or-drop + semantic consolidation |
| `python planning/fulfillment_demo.py` | Run the decomposition/planning demo |
| `python context_eval/run_eval.py` | Run the context-window-management strategy comparison |
| `python retrieval_eval/run_eval.py` | Run the retrieval-architecture comparison |
| `python planning_eval/run_eval.py` | Run the planning-algorithm comparison |
| `PYTHONPATH=. python3 state_graph/run_demo.py` | Run the state-graph narrated demo |
| `PYTHONPATH=. python3 -m pytest state_graph/tests/ -q` | Run state-graph tests |
| `pytest tests/` or `pytest -q` | Run the full automated test suite |
| `streamlit run platform_streamlit/Home.py` | Launch the experimental UI |

## API Documentation (MCP Tools)

Torque Tune does not expose a REST API; it exposes tools over the **Model
Context Protocol**. Tools are discovered via `tools/list` and invoked via
`tools/call`, subject to role-based visibility and the capability
negotiation described in `mcp-server/negotiation/negotiation.py`.

### Read Tools

| Tool | Parameters | Description |
|---|---|---|
| `search_spare_part` | `part_name: str` | Search for spare parts by name (partial match). Raises an error if no results are found. |
| `check_stock` | `part_id: int` | Return the current quantity for a spare part. Raises an error if the part does not exist. |
| `suggest_alternative` | `part_id: int` | Return alternative parts registered for a given part ID. |
| `search_company_knowledge` | *(RAG query)* | Hybrid RAG + Self-RAG-verified answer over the knowledge base (warranty terms, technical service bulletins, diagnostic procedures, company policy). Returns an explicit refusal instead of an unsupported answer when verification fails. |

### Write Tools

| Tool | Description |
|---|---|
| `update_inventory` | Increases or decreases stock for a part, following the authorization/validation/elicitation flow below. |
| `add_spare_part` | Adds a new spare part to the catalog. |
| `delete_spare_part` | Deletes an existing spare part. |
| `generate_inventory_report` | Generates an inventory summary while reporting progress to the client. |

### `update_inventory` flow

1. Look up the requesting user's role from the `Users` table via `user_id`.
2. Authorize the operation based on the stored role.
3. Read the current part quantity and status.
4. Validate the requested action, quantity, status, and reason.
5. If the change is sensitive (e.g. reducing a part to zero), request human
   confirmation through MCP Elicitation.
6. Apply the inventory update.
7. Insert an `InventoryLogs` record with the old/new quantity, action,
   reason, part ID, and user ID.
8. Commit the transaction and return an inventory notification payload.

Authorization and business validation are enforced server-side rather than
trusted from client-supplied values.

## Configuration

Server-level configuration lives in [`mcp-server/config.py`](mcp-server/config.py):

```python
SERVER_NAME = "Torque Tune Auto Care"
DATABASE_PATH = "databases/auto_care.db"
ALLOWED_ROLES = {"admin", "manager", "technician"}
MIN_STOCK_THRESHOLD = 10
```

Server capability declaration (`mcp-server/negotiation/negotiation.py`):

```python
SERVER_INFO = {"name": "auto-care-inventory-mcp-server", "version": "0.1.0"}
SERVER_CAPABILITIES = {
    "resources": {"listChanged": False},
    "elicitation": {},
    "tools": {"listChanged": True},
}
```

Per-agent tool visibility is managed through `mcp-server/tool_registry.py`
(backed by `mcp-server/_data/tool_registry.json`), which is what the
Streamlit platform's "MCP Tools" controls toggle.

The database schema (`databases/schema.sql`) includes core inventory tables
(`Users`, `Categories`, `Suppliers`, `SpareParts`, `AlternativeParts`,
`InventoryLogs`), long-running-workflow tables (`WarrantyClaims`,
`PurchaseOrders`), and agent-memory tables (`EpisodicMemory`,
`SemanticMemory`). Seed data is provided in `databases/seed.sql`, and an
entity-relationship diagram is available at `databases/erd.mmd`.

## Architecture

```text
Client (agent/client.py)
  │
  ▼
MCP Server (mcp-server/)
  ├── Authentication & Authorization
  ├── Read Tools ─────────────────────── search_company_knowledge ──┐
  ├── Write Tools                                                   │
  ├── Validation                                                    │
  ├── Elicitation                                                   │
  ├── Notifications                                                 │
  ├── Progress Tracking                                             │
  ├── Resources (knowledge_base/*.md) ───────────────────────────── ▼
  ├── Capability Negotiation                                   rag/ (RAG layer)
  │                                                             naive / hybrid / agentic
  ▼                                                             + Self-RAG verification
SQLite / SQL Server Database (databases/)
  ├── SpareParts, Users, InventoryLogs, WarrantyClaims, PurchaseOrders, ...
  └── EpisodicMemory, SemanticMemory (memory/ writes here)

Every interaction -> memory/ (short-term buffer + scratchpad)
  └── on overflow -> promote-or-drop router -> EpisodicMemory
                                    │
                     memory/run_consolidation.py (separate, periodic job)
                                    ▼
                              SemanticMemory (versioned, expiring, conflict-logged)

Repair/spare-parts requests -> planning/ (decomposition, ToT/LATS, Reflexion/Self-Refine)
Long-running workflows -> state_graph/ (Purchase Order, Inventory Approval, Warranty Claim)
                                    -> checkpointed to the database, resumable across processes
```

## Evaluation Reports

The repository ships reproducible comparisons, generated by the scripts
under `context_eval/`, `retrieval_eval/`, and `planning_eval/`:

- **Context-window management** — sliding window vs. observation masking vs.
  recursive summarization vs. zone-based pruning, evaluated on 5 fixed
  long-context transcripts (`context_eval/README_SECTION.md`).
- **Retrieval architectures** — naive vs. hybrid vs. agentic RAG, evaluated
  on 9 domain-specific test questions (`retrieval_eval/README_SECTION.md`).
- **Planning algorithms** — decomposition-first vs. dynamic decomposition,
  Plan-and-Solve vs. Tree of Thoughts, grounded vs. ungrounded LATS, and
  single-retry vs. Reflexion vs. Self-Refine, evaluated on fixed fulfillment
  scenarios (`planning_eval/results/`).

Without `ANTHROPIC_API_KEY` set, all of the above run against deterministic
offline mocks and still produce complete, reproducible tables; token/cost
figures are explicitly labelled as offline estimates in that case.

## Testing

```bash
pip install -r requirements.txt
pytest -q
```

State-graph tests specifically require the repository root on `PYTHONPATH`:

```bash
PYTHONPATH=. python3 -m pytest state_graph/tests/ -q
```

## Deployment

No deployment configuration (Dockerfile, CI/CD pipeline, or hosting config)
is present in the repository. To deploy, provision a Python environment with
the dependencies in `requirements.txt`, configure `DB_CONNECTION_STRING`
against a real database, and run `mcp-server/server.py` as a long-lived
process; the Streamlit UI can be served separately via `streamlit run
platform_streamlit/Home.py`.

## Troubleshooting

- **`RuntimeError: No database connection configured`** — `databases/db.py`
  raises this by design when no connection has been wired. Use
  `agent/client.py`'s `wire_demo_database()` helper (which points
  `get_connection` at the seeded SQLite database in `agent/demo_db.py`) for
  local runs, or set `DB_CONNECTION_STRING` and implement a real connection
  for production use.
- **RAG / memory / planning calls run through mocks with noisy results** —
  this is expected without `ANTHROPIC_API_KEY` set in `.env`; set the key to
  use real Claude calls instead of the documented offline fallbacks.
- **`ModuleNotFoundError: streamlit`** — `streamlit` is not listed in
  `requirements.txt`; install it manually to run `platform_streamlit/Home.py`.
- **State-graph tests fail to import modules** — run them with
  `PYTHONPATH=.` set to the repository root, as shown above.

## Contributing

No `CONTRIBUTING.md` or contribution guidelines file is present in this
repository. If you plan to contribute, open an issue or pull request
describing the change; run `pytest -q` before submitting to ensure the
existing test suite still passes.

## Attribution

The `planning/` module is built on top of a vendored, third-party
decomposition-and-planning toolkit located at
`planning/vendor/planning_lab/`. See
[`planning/vendor/ATTRIBUTION.md`](planning/vendor/ATTRIBUTION.md) for
details and the original source.

## License

No license file was found in this repository. Add a `LICENSE` file to
clarify the terms under which this project may be used, modified, or
distributed.
