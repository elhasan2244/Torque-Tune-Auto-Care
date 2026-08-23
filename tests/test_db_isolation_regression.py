"""Task 4 -- regression protection for the Task 3 database-wiring fix.

Two separate guarantees are checked here, deliberately with two different
techniques, because they have different (and sometimes conflicting)
order-sensitivity:

1. databases/db.py must still raise RuntimeError when nothing has wired a
   real connection. This can ONLY be checked reliably in a brand-new
   subprocess: within a single pytest session, importing any of the
   state_graph graph modules (state_graph/graphs/*.py) transitively
   imports state_graph/bootstrap.py, which -- by design, for the CLI demo
   and the Streamlit platform -- wires databases.db.get_connection (and,
   via `import server`, tools.read_tools/write_tools too) to the demo
   SQLite connection as a permanent, unconditional import-time side
   effect. That's legitimate for those entry points, but it means an
   in-process assertion of "raises when unwired" would pass or fail
   depending on whether a state_graph test happened to run first in this
   session -- exactly the kind of order-dependent test this task exists
   to prevent. A subprocess that never imports state_graph.bootstrap
   sidesteps that entirely and is genuinely order-independent (see
   state_graph/tests/test_crash_resume.py for the existing precedent of
   this pattern in this repo).

2. The `demo_db_connection` fixture (root conftest.py) must not leak rows
   between tests that use it. This one IS safe to check in-process,
   because it only concerns the fixture's own reseed-per-test behavior,
   not the bootstrap.py quirk above.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_unwired_get_connection_raises_in_a_clean_process():
    """A fresh interpreter that imports ONLY databases.db (no test
    fixture, no bootstrap.py, no CLI wiring) must see the genuinely
    unwired RuntimeError -- regardless of what any other test in this
    session has patched."""
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(REPO_ROOT)!r}); "
        "import databases.db as db\n"
        "try:\n"
        "    db.get_connection()\n"
        "    print('DID_NOT_RAISE')\n"
        "except RuntimeError as e:\n"
        "    print('RAISED:' + str(e))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "RAISED:" in result.stdout, result.stdout
    assert "No database connection configured" in result.stdout


def test_demo_db_connection_seeds_a_marker_row(demo_db_connection):
    """First half of the leak check: insert a row nothing else created."""
    import tools.read_tools as read_tools

    conn = read_tools.get_connection()
    try:
        conn.execute(
            "INSERT INTO Categories (name) VALUES ('__isolation_marker__')"
        )
        conn.commit()
        cur = conn.execute(
            "SELECT COUNT(*) FROM Categories WHERE name = '__isolation_marker__'"
        )
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_demo_db_connection_does_not_see_the_previous_tests_marker_row(
    demo_db_connection,
):
    """Second half: a later test requesting the same fixture must get a
    freshly reseeded database, not the previous test's connection/rows.
    Relies on pytest's default top-to-bottom execution order within this
    file (no randomization plugin is installed -- see requirements.txt)."""
    import tools.read_tools as read_tools

    conn = read_tools.get_connection()
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM Categories WHERE name = '__isolation_marker__'"
        )
        assert cur.fetchone()[0] == 0, (
            "found the previous test's marker row -- demo_db_connection "
            "leaked state between tests"
        )
    finally:
        conn.close()
