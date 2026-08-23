"""
mcp-server/rag/chunking.py

Step 1 of the RAG pipeline: load every document in the knowledge base and
split each one into section-sized chunks with metadata attached.

Knowledge base = the existing `resources/company_policy.md` (reused, not
duplicated -- it stays the single MCP Resource it already was) PLUS three
new documents under `resources/knowledge_base/` that did not exist as
tools or resources before:

    - supplier_warranty_terms.md      (warranty windows, claim codes)
    - technical_service_bulletins.md  (known defects, bulletin numbers)
    - diagnostic_repair_procedures.md (multi-step repair decision trees)

Why these three: front-desk/service-writer questions about "is this part
still under warranty", "is this a known TSB issue", or "what's the right
repair sequence" currently have no home -- they live in supplier PDFs and
staff memory. That's the real ungoverned-knowledge gap this RAG system
grounds.

Chunking strategy: split on '## ' headings, same as the section-based
approach already used for company_policy.md. Each heading in these
documents is already one self-contained rule or bulletin, so splitting on
headings keeps a rule/bulletin whole instead of cutting it mid-sentence.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"
KB_DIR = RESOURCES_DIR / "knowledge_base"

# doc_type is used later as a metadata-index field for pre-filtering.
#
# Discovered dynamically (not a fixed list) so a document an admin adds
# or removes at runtime through rag/registry.py is picked up the next
# time the index is rebuilt -- no code change, no restart.
_KNOWN_DOC_TYPES = {
    "company_policy.md": "policy",
    "supplier_warranty_terms.md": "warranty",
    "technical_service_bulletins.md": "tsb",
    "diagnostic_repair_procedures.md": "procedure",
}


def _discover_documents() -> list[dict]:
    docs = [{"path": RESOURCES_DIR / "company_policy.md", "doc_type": "policy"}]
    for path in sorted(KB_DIR.glob("*.md")):
        docs.append({"path": path, "doc_type": _KNOWN_DOC_TYPES.get(path.name, "kb")})
    return docs


DOCUMENTS = _discover_documents()

# Pulls out exact identifiers (TSB-2024-118, WT-100, supplier prefixes like
# CAE-, IDS-, TPD-, MFC-) so they land in metadata, not just body text.
# This is what lets hybrid/keyword search hit an exact code even if the
# embedding similarity for that chunk is mediocre.
_IDENTIFIER_PATTERN = re.compile(
    r"\b(TSB-\d{4}-\d{3}|WT-\d{3}|TPD-|CAE-|MFC-|IDS-)\b"
)


@dataclass
class Chunk:
    chunk_id: str
    text: str
    source: str          # filename, e.g. "technical_service_bulletins.md"
    doc_type: str        # "policy" | "warranty" | "tsb" | "procedure"
    section: str         # the "## " heading title
    identifiers: list = field(default_factory=list)


def _split_into_sections(markdown_text: str) -> list[dict]:
    """Split on '## ' headings. parts[0] is the '# Title' preamble/intro
    before the first '##' and is dropped -- it's framing, not a rule."""
    parts = re.split(r"(?m)^## ", markdown_text)
    sections = []
    for part in parts[1:]:
        lines = part.strip().splitlines()
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        sections.append({"title": title, "body": body})
    return sections


def load_chunks() -> list[Chunk]:
    chunks: list[Chunk] = []
    for doc in _discover_documents():
        path: Path = doc["path"]
        text = path.read_text(encoding="utf-8")
        for i, section in enumerate(_split_into_sections(text)):
            chunk_text = f"{section['title']}\n{section['body']}"
            identifiers = sorted(set(_IDENTIFIER_PATTERN.findall(chunk_text)))
            chunks.append(
                Chunk(
                    chunk_id=f"{path.stem}::{i}",
                    text=chunk_text,
                    source=path.name,
                    doc_type=doc["doc_type"],
                    section=section["title"],
                    identifiers=identifiers,
                )
            )
    return chunks

if __name__ == "__main__":
    cs = load_chunks()
    print(f"Loaded {len(cs)} chunks from {len(DOCUMENTS)} documents.\n")
    for c in cs:
        tag = f"[{c.doc_type}] {c.source} :: {c.section}"
        ids = f"  identifiers={c.identifiers}" if c.identifiers else ""
        print(f"{tag}{ids}")
