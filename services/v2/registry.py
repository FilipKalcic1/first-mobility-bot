"""V2 Tool Registry Adapter.

Thin read-only wrapper over `config/processed_tool_registry.json` that
exposes the contract used by L3 RecognitionEngine and L7 ToolExecutor:

    .tools                            # iterable of tool dicts
    .tool_id_of(tool)                 # str
    .anchor_text_for(tool)            # str — what L3 embeds for matching
    .has_tool(tool_id)                # bool — whitelist check
    .method_of(tool_id)               # "GET" | "POST" | ... | None
    .purpose_of(tool_id)              # human-readable description
    .spec_for(tool_id)                # full spec for executor (or None)

The legacy modules under `services/registry/` do similar work but pull in
a heavy chain (FAISS, BM25, boost engine, llm reranker). This adapter is
deliberately ~120 LOC and does nothing the JSON doesn't already say.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# Croatian action markers prepended to anchor text. Inject explicit
# action semantics so cosine separates {get_X, delete_X, post_X} —
# they otherwise share most of their embedding signal because the
# enrichment phrases describe the same entity.
_METHOD_MARKERS_HR = {
    "GET":    "DOHVAT ČITANJE",
    "POST":   "DODAVANJE KREIRANJE",
    "PUT":    "ZAMJENA AŽURIRANJE",
    "PATCH":  "AŽURIRANJE PROMJENA",
    "DELETE": "BRISANJE UKLANJANJE",
}

# Cardinality markers — distinguish "one specific" vs "filter many"
# vs "unconstrained" within the same method/entity family.
_SUFFIX_MARKERS = (
    ("_DeleteByCriteria", " VIŠE PO_KRITERIJU FILTRIRAJ"),
    ("_id_documents",     " ENTITETOV_DOKUMENT"),
    ("_id",               " JEDAN PO_ID"),
    ("_GroupBy",          " GRUPIRAJ_REZULTATE"),
    ("_Agg",              " AGREGAT STATISTIKA"),
    ("_ProjectTo",        " PODSKUP_POLJA"),
    ("_multipatch",       " VIŠE_PATCH"),
    ("_Defleet",          " IZ_VOZNOG_PARKA"),
    ("_Infleet",          " U_VOZNI_PARK"),
)


def _action_markers_for(tool: dict) -> str:
    """Build the Croatian action-prefix marker string for a tool.

    Includes:
      - HTTP-method marker (BRISANJE for DELETE, etc)
      - Suffix-based cardinality marker (_id → JEDAN; _DeleteByCriteria → VIŠE)

    These markers shift the embedding meaningfully toward action-aware
    space so e.g. delete_Vehicles_id and get_Vehicles_id sit far apart
    in cosine despite sharing entity vocabulary.
    """
    method = (tool.get("method") or "").upper()
    parts = [_METHOD_MARKERS_HR.get(method, "")]
    op = tool.get("operation_id", "")
    for suf, marker in _SUFFIX_MARKERS:
        if suf in op:
            parts.append(marker)
            break
    return " ".join(p for p in parts if p)


class ToolRegistry:
    """Read-only adapter over the processed tool registry JSON."""

    def __init__(
        self,
        tools: list[dict],
        anchor_enrichments: Optional[dict[str, list[str]]] = None,
        *,
        enable_action_markers: bool = False,
        categories_index: Optional[dict[str, Any]] = None,
        tool_to_category: Optional[dict[str, str]] = None,
    ):
        self._tools = list(tools)
        self._by_id: dict[str, dict] = {
            t["operation_id"]: t for t in self._tools
            if "operation_id" in t
        }
        self._enrichments = anchor_enrichments or {}
        self._enable_action_markers = enable_action_markers
        # Hierarchical pre-filter data — when populated, L3 can prune
        # the 950-tool search space by first matching the query against
        # ~100 category descriptions and only keeping tools within
        # the top-K matching categories. ada-002 distinguishes coarse
        # categories well; the per-tool noise within each category is
        # what hurts. This prefilter exploits that asymmetry.
        self._categories: dict[str, dict] = categories_index or {}
        self._tool_to_category: dict[str, str] = tool_to_category or {}

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        enrichments_path: Optional[str | Path] = None,
        categories_path: Optional[str | Path] = None,
    ) -> "ToolRegistry":
        p = Path(path)
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        tools = data.get("tools", []) if isinstance(data, dict) else data

        enrichments: dict[str, list[str]] = {}
        if enrichments_path is not None:
            ep = Path(enrichments_path)
            if ep.exists():
                try:
                    with ep.open(encoding="utf-8") as f:
                        enrichments = json.load(f) or {}
                except (OSError, ValueError) as e:
                    logger.warning("anchor enrichments unreadable: %s", e)
        else:
            default = p.parent / "tool_anchor_enrichments.json"
            if default.exists():
                try:
                    with default.open(encoding="utf-8") as f:
                        enrichments = json.load(f) or {}
                except (OSError, ValueError) as e:
                    logger.warning("anchor enrichments unreadable: %s", e)

        # Auto-load tool_categories.json sidecar (or explicit path)
        categories_index: dict[str, dict] = {}
        tool_to_category: dict[str, str] = {}
        cp = Path(categories_path) if categories_path else (
            p.parent / "tool_categories.json"
        )
        if cp.exists():
            try:
                with cp.open(encoding="utf-8") as f:
                    cat_data = json.load(f)
                categories_index = cat_data.get("categories", {}) or {}
                tool_to_category = cat_data.get("tool_to_category", {}) or {}
            except (OSError, ValueError) as e:
                logger.warning("tool_categories unreadable: %s", e)

        return cls(
            tools,
            anchor_enrichments=enrichments,
            categories_index=categories_index,
            tool_to_category=tool_to_category,
        )

    # ---- contract: hierarchical pre-filter -------------------------------

    @property
    def categories(self) -> dict[str, dict]:
        return self._categories

    def category_anchor_text(self, category_name: str) -> str:
        """Croatian description used to embed the category for matching.
        Falls back to the category name when description_hr missing."""
        cat = self._categories.get(category_name) or {}
        desc = cat.get("description_hr") or cat.get("description_en") or ""
        keywords = cat.get("keywords_hr") or []
        parts = [category_name.replace("_", " ")]
        if desc:
            parts.append(desc)
        if keywords:
            parts.append(" ".join(keywords[:10]))
        return " ".join(parts)

    def category_of(self, tool_id: str) -> Optional[str]:
        return self._tool_to_category.get(tool_id)

    def tools_in_categories(self, category_names) -> set[str]:
        wanted = set(category_names)
        if not wanted:
            return set()
        return {
            tid for tid, cat in self._tool_to_category.items()
            if cat in wanted
        }

    # ---- contract: tools / lookup ---------------------------------------

    @property
    def tools(self) -> Iterable[dict]:
        return self._tools

    @staticmethod
    def tool_id_of(tool: dict) -> str:
        return tool.get("operation_id", "")

    def has_tool(self, tool_id: str) -> bool:
        return tool_id in self._by_id

    def spec_for(self, tool_id: str) -> Optional[dict]:
        spec = self._by_id.get(tool_id)
        if spec is None:
            return None
        # Normalize the shape the executor expects.
        return {
            "service":   spec.get("service_name") or spec.get("swagger_name"),
            "path":      spec.get("path") or "",
            "method":    (spec.get("method") or "GET").upper(),
            "default_tenant_id": spec.get("default_tenant_id"),
            **spec,  # raw fields available too
        }

    def method_of(self, tool_id: str) -> Optional[str]:
        spec = self._by_id.get(tool_id)
        if spec is None:
            return None
        m = spec.get("method")
        return m.upper() if m else None

    def purpose_of(self, tool_id: str) -> str:
        spec = self._by_id.get(tool_id)
        if spec is None:
            return ""
        # description is often empty in the registry; embedding_text is
        # the curated anchor — strip the operation_id prefix for display.
        desc = spec.get("description") or spec.get("summary") or ""
        if desc:
            return desc
        text = spec.get("embedding_text") or ""
        prefix = (spec.get("operation_id") or "") + ". "
        if text.startswith(prefix):
            text = text[len(prefix):]
        return text[:300]

    # ---- contract: anchor text for L3 -----------------------------------

    def _base_anchor_text(self, tool: dict) -> str:
        """Tool's curated description prefixed with Croatian action
        markers (BRISANJE/DOHVAT/...) and cardinality markers
        (JEDAN/VIŠE/PO_KRITERIJU). Markers separate sibling tools
        in embedding space — measured to be the cleanest path to
        cosine-distinguishing get_X from delete_X."""
        text = tool.get("embedding_text")
        if not text:
            op = tool.get("operation_id", "")
            path = tool.get("path", "")
            method = tool.get("method", "")
            text = f"{op}. {method} {path}".strip()
        if not self._enable_action_markers:
            return text
        markers = _action_markers_for(tool)
        return f"{markers} {text}".strip() if markers else text

    def anchor_text_for(self, tool: dict) -> str:
        """Single concatenated string used by tests / legacy callers
        that expect one anchor per tool."""
        base = self._base_anchor_text(tool)
        tool_id = tool.get("operation_id", "")
        extras = self._enrichments.get(tool_id) or []
        if extras:
            return " ".join(extras) + " " + base
        return base

    def anchor_texts_for(self, tool: dict) -> list[str]:
        """Return EVERY phrase the recognition engine should embed for
        this tool. Each enrichment phrase becomes its own anchor entry
        so cosine matches the closest surface form rather than a
        smeared average. The base description is always included."""
        base = self._base_anchor_text(tool)
        tool_id = tool.get("operation_id", "")
        extras = self._enrichments.get(tool_id) or []
        out = [base]
        out.extend(p for p in extras if isinstance(p, str) and p.strip())
        return out
