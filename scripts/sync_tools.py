"""Regenerate config/processed_tool_registry.json from live Swagger sources.

============================================================================
 REGEN RUNBOOK — Phase 2 consolidation (2026-05-15)
============================================================================

The bot now routes via TWO configs:

  config/processed_tool_registry.json    ← THIS script writes it (from Swagger)
  config/tool_data.json                  ← single source of truth for routing
                                           (intent_summary, use_when,
                                           do_not_use_when, anchors)

History (kept for archaeology, NO LONGER READ AT RUNTIME):
  config/tool_knowledge_base.archive.json
  config/tool_anchor_enrichments.archive.json

CORRECT ORDER when MobilityOne backend changes (e.g. new tools added):

    1.  python scripts/sync_tools.py
            cost: ~2-5 min, $0 (only Swagger HTTP fetches)
            writes: config/processed_tool_registry.json
            requires: .env populated (swagger_sources URLs reachable)

    2.  pytest tests/test_config_parity.py
            cost: free, ~0.1s
            verifies: tool_data.json operation_ids match registry; every
                      tool has all 4 content fields, ≥5 anchors, etc.
            If new tools appeared in step 1, this FAILS LOUDLY — they are
            in the registry but missing from tool_data.json.

    3.  If step 2 failed: HAND-EDIT config/tool_data.json to add the new
        tools' content fields (intent_summary, use_when, do_not_use_when,
        anchors). For each new tool add an entry under "tools":
            "<operation_id>": {
              "operation_id": "...",  "method": "...",  "service": "...",
              "path": "...",          "parameters": {...},
              "intent_summary": "Croatian — what this tool does, entity-specific",
              "use_when": ["...", "..."],
              "do_not_use_when": [{"alt_tool": "...", "razlog": "..."}],
              "anchors": ["12 diverse Croatian phrases users might say", ...]
            }
        Then re-run step 2. Repeat until green.

    4.  scripts/build_tool_data.py — EMERGENCY REBUILD ONLY (from archives).
        Don't use in normal flow.

WHY HAND-EDIT (not LLM regen): A Phase 3 LLM-regen attempt was verified
broken by a 5-tool smoke test on 2026-05-15. Feeding gpt-4o-mini just
(op_id, path, params, sibling list) produces literal-name slop — e.g.
anchors mentioning "MasterData" or "AddCase" that no real user would
type. The hand-curated content is BETTER than what context-blind regen
produces. See scripts/regenerate_tool_data.py for the broken skeleton.

SAFETY NETS:
- tests/test_config_parity.py blocks CI if registry and tool_data drift.
- services/v2/engine.py::_log_config_freshness emits a `config_freshness`
  log line at every engine boot. Grep production logs for
  `tool_data_stale=True` to spot stale data.

DELETING this runbook = losing the only doc of how to keep accuracy
from silently dropping. Don't.
============================================================================
"""
import asyncio
import json
import logging
import os
import sys

# Add project root to path to allow imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from pathlib import Path

from config import get_settings
from services.registry.swagger_parser import SwaggerParser
from services.registry.embedding_engine import EmbeddingEngine
from services.v2.atomic_io import atomic_write_json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sync_tools")

async def main():
    """
    Parses Swagger specifications, enriches them with custom rules,
    and saves them to a single JSON file for fast startup.
    """
    logger.info("Starting tool registry sync...")
    
    try:
        settings = get_settings()
    except Exception as e:
        logger.error(f"Failed to load settings. Make sure .env file is configured. Error: {e}")
        return

    parser = SwaggerParser()
    embedding_engine = EmbeddingEngine()

    all_tools_map = {}
    for source in settings.swagger_sources:
        logger.info(f"Parsing {source}...")
        
        tools = await parser.parse_spec(source, embedding_engine.build_embedding_text)
        for tool in tools:
            if tool.operation_id not in all_tools_map:
                all_tools_map[tool.operation_id] = tool

    logger.info(f"Parsed {len(all_tools_map)} unique tools in total.")

    # Build dependency graph
    logger.info("Building dependency graph...")
    dep_graph = embedding_engine.build_dependency_graph(all_tools_map)
    
    # Prepare for serialization
    tools_as_dict = [tool.model_dump(mode='json') for tool in all_tools_map.values()]
    dep_graph_as_dict = [dep.model_dump(mode='json') for dep in dep_graph.values()]

    output_data = {
        "version": "1.0",
        "tools": tools_as_dict,
        "dependency_graph": dep_graph_as_dict
    }

    # Save to JSON file in config directory
    output_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'processed_tool_registry.json')
    
    # Atomic write — the bot reads this file at startup; a half-written file
    # would crash the factory. atomic_write_json writes temp + os.replace.
    try:
        atomic_write_json(Path(output_path), output_data)
        logger.info(f"Successfully saved tool registry to {output_path}")
    except OSError as e:
        logger.error(f"Failed to write to {output_path}. Error: {e}")

if __name__ == "__main__":
    # This script requires environment variables to be set (e.g., from .env file)
    # to access MOBILITY_API_URL etc.
    asyncio.run(main())
