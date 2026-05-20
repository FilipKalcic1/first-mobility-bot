"""L3 LLM router (Phase 2 rewrite, 2026-05-12).

Components:
- anchor_index — embed-once cosine retrieval over per-tool anchor phrases
- tool_schema_builder — map registry tool entries to OpenAI tools=[] schema
- llm_router — anchor top-K → LLM tool-calling → RouterResult

Used by services/v2/engine.py at the L3 routing step. Replaces the older
services/v2/recognition.py + services/v2/formatter.py templates.
"""
