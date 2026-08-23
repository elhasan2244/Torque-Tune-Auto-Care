import sys
import json
from pathlib import Path

import pytest

# Setup paths to ensure imports work correctly
ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER_ROOT = ROOT / "mcp-server"
AGENT_ROOT = ROOT / "agent"

# Add root, mcp-server, and agent to sys.path
for path in (str(ROOT), str(MCP_SERVER_ROOT), str(AGENT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

# Importing the memory modules no longer requires wiring the database
# first: get_connection() is only called once a test actually calls into
# EpisodicMemory/SemanticMemory, and the `demo_db_connection` fixture below
# (from the root conftest.py) patches those modules' get_connection at
# test time instead of once, permanently, at import time -- see conftest.py
# for why that used to leak between test modules.
from memory.short_term_memory import ShortTermMemory
from memory.scratchpad import Scratchpad
from memory.episodic_memory import EpisodicMemory
from memory.semantic_memory import SemanticMemory
from memory.memory_manager import MemoryManager


@pytest.fixture(autouse=True)
def _wire_demo_database(demo_db_connection):
    """Every test in this module gets a fresh, isolated demo database;
    the two that don't touch the DB (STM/Scratchpad) just ignore it."""
    yield

# ---------------------------------------------------------
# Test Short-Term Memory
# ---------------------------------------------------------
def test_short_term_memory_capacity():
    """Test that STM correctly identifies when it is full and clears properly."""
    stm = ShortTermMemory(max_capacity=2)
    stm.add_message("user", "Hello")
    assert not stm.is_full()
    
    stm.add_message("assistant", "Hi there")
    assert stm.is_full()
    
    old_messages = stm.clear()
    assert len(old_messages) == 2
    assert not stm.is_full()

# ---------------------------------------------------------
# Test Scratchpad
# ---------------------------------------------------------
def test_scratchpad_state_tracking():
    """Test if Scratchpad correctly tracks goals, plans, and intermediate results."""
    sp = Scratchpad()
    sp.set_goal("Update Inventory", ["search_part", "update_qty"])
    
    sp.complete_step("search_part", result={"part_id": 1}, result_key="search_res")
    state = sp.get_state()
    
    assert "search_part" in state["completed_steps"]
    assert state["next_action"] == "update_qty"
    assert state["intermediate_results"]["search_res"]["part_id"] == 1

# ---------------------------------------------------------
# Test Episodic Memory (Requires DB Connection)
# ---------------------------------------------------------
def test_episodic_memory_db_insertion():
    """Test inserting and retrieving an episode from the database."""
    ep_mem = EpisodicMemory()
    test_content = {"test_key": "test_value"}
    
    # Insert a dummy episode
    ep_mem.add_episode(
        event_type="test_event",
        content=test_content,
        promotion_reason="Testing DB integration"
    )
    
    # Retrieve recent episodes
    recent = ep_mem.get_recent_episodes(limit=1)
    assert len(recent) > 0
    assert recent[0]["event_type"] == "test_event"
    assert recent[0]["content"]["test_key"] == "test_value"

# ---------------------------------------------------------
# Test Semantic Memory (Requires DB Connection)
# ---------------------------------------------------------
def test_semantic_memory_versioning():
    """Test that updating a fact correctly increments the version and deactivates the old one."""
    sem_mem = SemanticMemory(llm_client=None)
    
    # Insert first version
    sem_mem.update_fact("test_fact", "Version 1")
    active_facts = sem_mem.get_active_facts()
    assert active_facts["test_fact"] == "Version 1"
    
    # Update to second version
    sem_mem.update_fact("test_fact", "Version 2")
    active_facts = sem_mem.get_active_facts()
    
    # The active fact should now be Version 2
    assert active_facts["test_fact"] == "Version 2"

# ---------------------------------------------------------
# Test Full Pipeline via Memory Manager
# ---------------------------------------------------------
def test_memory_manager_integration():
    """Test the MemoryManager context retrieval structure."""
    manager = MemoryManager(llm_client=None)
    
    # Simulate a single interaction
    manager.add_interaction("user", "Check stock for brakes")
    
    # Retrieve context for LLM
    context = manager.retrieve_for_llm()
    
    # Validate the structure of the retrieved context
    assert "short_term_memory" in context
    assert "scratchpad" in context
    assert "episodic_memory" in context
    assert "semantic_memory" in context
