from __future__ import annotations

from pathlib import Path

from chunking import RESOURCES_DIR, KB_DIR
import search_knowledge_tool
from hybrid_rag import build_hybrid_rag

_KNOWN_DOC_TYPES = {
    "company_policy.md": "policy",
    "supplier_warranty_terms.md": "warranty",
    "technical_service_bulletins.md": "tsb",
    "diagnostic_repair_procedures.md": "procedure",
}


def list_documents() -> list[dict]:
    docs = [RESOURCES_DIR / "company_policy.md"] + sorted(KB_DIR.glob("*.md"))
    out = []
    for path in docs:
        if not path.exists():
            continue
        out.append({
            "name": path.name,
            "doc_type": _KNOWN_DOC_TYPES.get(path.name, "kb"),
            "size_bytes": path.stat().st_size,
            "editable": path.parent == KB_DIR,
        })
    return out


def add_document(filename: str, content: str) -> dict:
    if not filename.endswith(".md"):
        raise ValueError("only .md documents are supported")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError("invalid filename")
    path = KB_DIR / filename
    path.write_text(content, encoding="utf-8")
    search_knowledge_tool._rag = build_hybrid_rag()
    return {"name": path.name, "size_bytes": path.stat().st_size} 

def remove_document(filename: str) -> None:
    path = KB_DIR / filename
    if path.parent != KB_DIR:
        raise ValueError("only documents under resources/knowledge_base/ can be removed")
    if path.exists():
        path.unlink()
    search_knowledge_tool._rag = build_hybrid_rag()