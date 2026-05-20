"""L8 LLM formatter (Phase 3 rewrite, 2026-05-12).

Replaces services/v2/formatter.py (template-based, scaled to ~20 tools).
The LLM formatter takes raw backend JSON for any of 950 tools and
generates a concise Croatian natural-language reply, grounded in the
data with no template assumptions.

Used by services/v2/engine.py at the L8 step. Built in Phase 3.
"""
