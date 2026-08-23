"""
Crash-and-Resume test (Execution Roadmap requirement, Phase 3): proves
recovery across a REAL killed process, not just a resume() call in the
same Python session.

Strategy: a subprocess runs one of the real graphs (purchase_order_graph)
just past its first node's checkpoint, then calls `os.kill(getpid(),
SIGKILL)` on itself -- an actual unrecoverable crash, not
sys.exit()/an exception the subprocess could clean up after. The parent
test process then starts a completely fresh Python process that resumes
the SAME thread_id and asserts it completes correctly from where the
killed process left off.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_CRASH_SCRIPT = textwrap.dedent(
    """
    import os
    import signal
    import sys
    sys.path.insert(0, {repo_root!r})

    from state_graph.bootstrap import ensure_wired
    import agent.demo_db as demo_db
    demo_db.reset_demo_database()
    import databases.db as db
    db.get_connection = demo_db.build_demo_connection

    from state_graph.graphs.purchase_order_graph import build_graph

    graph = build_graph().compile()

    # Run the entry node manually and checkpoint it (mirrors what
    # CompiledGraph.invoke does internally for the first node), then kill
    # this process immediately -- BEFORE process_next_batch or any later
    # node ever runs in this process.
    from state_graph.graphs.purchase_order_graph import decompose_into_supplier_batches

    state = {{
        "thread_id": {thread_id!r},
        "user_id": 2,
    }}
    update = decompose_into_supplier_batches(state)
    state.update(update)

    from state_graph.checkpointer import Checkpointer
    Checkpointer().save(
        thread_id={thread_id!r},
        graph_name="purchase_order",
        node_name="decompose_into_supplier_batches",
        status="running",
        state=state,
    )
    sys.stdout.write("CHECKPOINT_WRITTEN\\n")
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)  # real, unrecoverable crash
    """
)

_RESUME_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {repo_root!r})

    from state_graph.bootstrap import ensure_wired
    import agent.demo_db as demo_db
    import databases.db as db
    db.get_connection = demo_db.build_demo_connection

    from state_graph.graphs.purchase_order_graph import build_graph
    from state_graph.checkpointer import Checkpointer

    graph = build_graph().compile()

    history_before = Checkpointer().history({thread_id!r})
    assert [c.node_name for c in history_before] == ["decompose_into_supplier_batches"], history_before

    result = graph.resume({thread_id!r})
    print("RESUME_STATUS", result.status)
    print("RESUME_NODE", result.node_name)
    print("RESUME_HAS_BATCHES", "batches" in result.state)
    """
)


def test_crash_and_resume_across_real_processes():
    thread_id = "crash-resume-real-subprocess-1"

    crash_code = _CRASH_SCRIPT.format(repo_root=str(REPO_ROOT), thread_id=thread_id)
    crashed = subprocess.run(
        [sys.executable, "-c", crash_code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    # A SIGKILL'd process has no normal return code; on POSIX it's negative
    # the signal number. It must NOT have exited cleanly (0).
    assert crashed.returncode != 0
    assert "CHECKPOINT_WRITTEN" in crashed.stdout

    resume_code = _RESUME_SCRIPT.format(repo_root=str(REPO_ROOT), thread_id=thread_id)
    resumed = subprocess.run(
        [sys.executable, "-c", resume_code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert "RESUME_STATUS completed" in resumed.stdout or "RESUME_STATUS paused_hitl" in resumed.stdout
    assert "RESUME_HAS_BATCHES True" in resumed.stdout
