from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "_data"
DATA_DIR.mkdir(exist_ok=True)
REGISTRY_PATH = DATA_DIR / "tool_registry.json"

DEFAULT_AGENT_TOOLS: dict[str, list[str]] = {
    "memory_rag": [
        "search_spare_part", "check_stock", "suggest_alternative",
        "search_company_knowledge", "update_inventory",
        "add_spare_part", "delete_spare_part", "generate_inventory_report",
    ],
    "planning": ["search_spare_part", "check_stock", "suggest_alternative"],
    "purchase_order": ["search_spare_part", "check_stock"],
    "inventory_approval": ["check_stock", "update_inventory", "search_company_knowledge"],
    "warranty": ["search_company_knowledge", "check_stock"],
}


def _all_registered_tools() -> list[str]:
    import server  # noqa: F401
    from app import mcp
    return sorted(mcp._tools.keys())


def _load() -> dict[str, list[str]]:
    if not REGISTRY_PATH.exists():
        return {agent: list(tools) for agent, tools in DEFAULT_AGENT_TOOLS.items()}
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _save(data: dict[str, list[str]]) -> None:
    REGISTRY_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_agents() -> list[str]:
    return list(DEFAULT_AGENT_TOOLS.keys())


def list_tools(agent_id: str | None = None) -> list[dict]:
    registered = _all_registered_tools()
    data = _load()
    agents = [agent_id] if agent_id else list(DEFAULT_AGENT_TOOLS.keys())
    out = []
    for agent in agents:
        enabled = set(data.get(agent, []))
        for name in registered:
            out.append({"agent_id": agent, "tool": name, "enabled": name in enabled})
    return out


def set_tool_enabled(agent_id: str, tool_name: str, enabled: bool) -> None:
    registered = _all_registered_tools()
    if tool_name not in registered:
        raise ValueError(f"{tool_name!r} is not a registered MCP tool")
    data = _load()
    current = set(data.get(agent_id, []))
    if enabled:
        current.add(tool_name)
    else:
        current.discard(tool_name)
    data[agent_id] = sorted(current)
    _save(data)


def enabled_tools_for_agent(agent_id: str) -> list[str]:
    registered = _all_registered_tools()
    data = _load()
    return sorted(set(data.get(agent_id, [])) & set(registered))