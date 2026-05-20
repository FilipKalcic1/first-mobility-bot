"""
Tool routing constants — REMOVED in 11.0.4.

Per the architect's directive (FIRST.MD): the system must not hold
hardcoded knowledge about specific tools or their parameter mappings.
Routing decisions are made entirely from registry data
(`processed_tool_registry.json` + `tool_documentation.json`) at runtime.

What lived here before, and where it went:

  PRIMARY_TOOLS         — 22 tool→description pairs hand-curated.
                          DELETED. Same data lives in tool_documentation.json
                          for all 950 tools (purpose field). Search uses that
                          directly via FAISS+BM25.

  PRIMARY_ACTION_TOOLS  — keyword→tool boost dict for ~20 common tools.
                          DELETED. BM25 already indexes synonyms_hr from
                          tool_documentation; the boost was redundant.

  INTENT_CONFIG         — 30 intent→tool mappings used only by the dead
                          QueryRouter mediation path.
                          DELETED with `_mediation_route`.

  FLOW_TRIGGERS         — 4 tool→flow_type mappings, never imported anywhere.
                          DELETED.

This file is kept (rather than deleted) only as a tombstone — if a stale
import surfaces, the ImportError will point at this docstring and explain.
"""
