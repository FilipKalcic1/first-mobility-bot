"""Convert registry tool entries to OpenAI tool-calling schemas.

OpenAI's `tools` parameter expects:
    {
      "type": "function",
      "function": {
        "name": "<tool_id>",                  # ^[a-zA-Z0-9_-]{1,64}$
        "description": "<what it does, Croatian preferred>",
        "parameters": {
          "type": "object",
          "properties": {
            "<param_name>": {
              "type": "string"|"integer"|"number"|"boolean"|"array"|"object",
              "description": "<...>",
              "enum": [...]?       # if applicable
            },
            ...
          },
          "required": ["..."]
        }
      }
    }

Constraints we handle:
- Tool name max 64 chars — 3 tools exceed this, we alias them with a stable hash.
- Parameters with `dependency_source != "user_input"` are SKIPPED (the executor
  injects them from identity context — LLM should not try to fill them).
- Croatian descriptions preferred. Falls back to English from registry.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_TOOL_NAME_LEN = 64


@dataclass
class ToolSchemaBuilder:
    """Pure transformer: registry tool → OpenAI schema. No state across calls."""

    # operation_id → alias (only populated for tools whose id exceeds 64 chars)
    _name_aliases: dict[str, str]
    # alias → operation_id (reverse map for tool-call result decoding)
    _alias_to_op: dict[str, str]

    @classmethod
    def from_registry(cls, registry: dict) -> "ToolSchemaBuilder":
        aliases: dict[str, str] = {}
        rev: dict[str, str] = {}
        for tool in registry.get("tools", []):
            op = tool.get("operation_id")
            if not op:
                continue
            if len(op) > MAX_TOOL_NAME_LEN:
                alias = _make_alias(op)
                aliases[op] = alias
                rev[alias] = op
            else:
                rev[op] = op
        return cls(_name_aliases=aliases, _alias_to_op=rev)

    def schema_name_for(self, operation_id: str) -> str:
        """Map operation_id → OpenAI-safe tool name."""
        return self._name_aliases.get(operation_id, operation_id)

    def operation_id_for(self, schema_name: str) -> str | None:
        """Map LLM-returned tool_call.function.name back to operation_id."""
        return self._alias_to_op.get(schema_name)

    def build_for_tools(
        self,
        tools: list[dict],
        tkb: dict,
        max_desc_len: int = 600,
    ) -> list[dict]:
        """Build OpenAI tools=[] entries for the given registry tools."""
        out: list[dict] = []
        for tool in tools:
            entry = self._build_one(tool, tkb, max_desc_len)
            if entry is not None:
                out.append(entry)
        return out

    def _build_one(
        self, tool: dict, tkb: dict, max_desc_len: int,
    ) -> dict | None:
        op_id = tool.get("operation_id")
        if not op_id:
            return None
        tkb_entry = tkb.get(op_id, {})

        # Build description: intent_summary + use_when bullets + cardinality hint.
        # Description is what the LLM uses to decide if this tool matches the
        # query — invest heavily in clarity.
        parts: list[str] = []
        intent = (tkb_entry.get("intent_summary") or "").strip()
        if intent:
            parts.append(intent)

        use_when_raw = tkb_entry.get("use_when") or []
        use_when_lines: list[str] = []
        for item in use_when_raw[:5]:
            if isinstance(item, str) and item.strip():
                use_when_lines.append(f"- {item.strip()}")
            elif isinstance(item, dict):
                txt = item.get("text") or item.get("scenario") or ""
                if txt:
                    use_when_lines.append(f"- {txt}")
        if use_when_lines:
            parts.append("Use when:\n" + "\n".join(use_when_lines))

        do_not_raw = tkb_entry.get("do_not_use_when") or []
        do_not_lines: list[str] = []
        for item in do_not_raw[:3]:
            if isinstance(item, dict):
                alt = item.get("alt_tool", "")
                reason = item.get("razlog") or item.get("reason") or ""
                if reason:
                    do_not_lines.append(f"- NE: {reason} → koristi {alt}")
            elif isinstance(item, str):
                do_not_lines.append(f"- NE: {item}")
        if do_not_lines:
            parts.append("Do NOT use when:\n" + "\n".join(do_not_lines))

        # Cardinality hint from operation_id suffixes (single source of truth).
        card = _cardinality_hint(op_id)
        if card:
            parts.append(card)

        method = tool.get("method") or tkb_entry.get("method") or "GET"
        is_mutation = tool.get("is_mutation", False)
        if is_mutation:
            parts.append(
                "MUTATING TOOL: changes backend state. User confirmation will "
                "be requested before execution."
            )

        description = "\n\n".join(parts)
        if len(description) > max_desc_len:
            description = description[:max_desc_len].rstrip() + "..."

        # Append HTTP method as a soft hint at the end
        description = f"[{method}] {description}"

        # Build parameters schema — only user_input params.
        params_raw = tool.get("parameters") or {}
        properties: dict[str, dict] = {}
        for pname, pdef in params_raw.items():
            if not isinstance(pdef, dict):
                continue
            ds = pdef.get("dependency_source")
            if ds and ds != "user_input":
                # Auto-injected by executor (tenant_id, person_id, etc).
                continue
            prop = _param_to_jsonschema(pdef)
            if prop is None:
                continue
            properties[pname] = prop

        # ANTI-FABRICATION (Filip 2026-05-24): emit an EMPTY `required` to the
        # LLM on purpose. OpenAI tool-calling fills `required` properties even
        # when the value isn't in the user's message → the model FABRICATES
        # (e.g. a required enum with no options → guesses "1"; measured:
        # "izmijeni rezervaciju 12" → {id:12, AssigneeType:1}). The system
        # prompt already instructs "fill only what the user explicitly
        # mentions" — the schema's required[] was fighting that prompt and
        # winning. Required-ness is still enforced downstream from the registry
        # (engine._compute_missing_required → param-ask), which ASKS the user
        # rather than letting the LLM guess. The registry stays the source of
        # truth for required; the LLM is just an extractor of STATED values.
        return {
            "type": "function",
            "function": {
                "name": self.schema_name_for(op_id),
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [],
                },
            },
        }


# ---- helpers ----


def _make_alias(op_id: str) -> str:
    """Stable 64-char-or-less alias for an oversized tool name.

    Strategy: keep first 50 chars + 8-char hash, total 58. Hash makes it
    deterministic and human-recognisable (you can still see what entity it's
    about). The 64 limit comes from OpenAI's tool-name regex.
    """
    head = op_id[:50].rstrip("_")
    h = hashlib.sha1(op_id.encode("utf-8")).hexdigest()[:8]
    alias = f"{head}_{h}"
    assert len(alias) <= MAX_TOOL_NAME_LEN
    return alias


def _cardinality_hint(op_id: str) -> str:
    """Map operation_id suffix → cardinality hint string."""
    if op_id.endswith("_metadata"):
        return "Kardinalnost: METADATA — vraća strukturu/polja, ne podatke."
    if op_id.endswith("_id"):
        return "Kardinalnost: BY_ID — vraća specifičnu stavku po njenom ID-u."
    if "_Agg" in op_id:
        return "Kardinalnost: AGGREGATE — agregirana statistika (sum/min/max/avg)."
    if "_GroupBy" in op_id:
        return "Kardinalnost: GROUPBY — grupiranje rezultata po polju."
    if "_tree" in op_id:
        return "Kardinalnost: HIERARCHY — hijerarhijski/stablo prikaz."
    if "_ProjectTo" in op_id:
        return "Kardinalnost: PROJECTION — vraća podskup polja."
    if "_DeleteByCriteria" in op_id:
        return "Kardinalnost: MULTI — briše više stavki po kriteriju."
    if "_multipatch" in op_id:
        return "Kardinalnost: MULTI — ažurira više stavki odjednom."
    return ""


def _param_to_jsonschema(pdef: dict) -> dict | None:
    """Map a single registry parameter entry to JSON Schema."""
    raw_type = (pdef.get("param_type") or "string").lower()
    type_map = {
        "string": "string",
        "integer": "integer",
        "number": "number",
        "float": "number",
        "boolean": "boolean",
        "array": "array",
        "object": "object",
    }
    json_type = type_map.get(raw_type, "string")
    prop: dict = {"type": json_type}

    desc = (pdef.get("description") or "").strip()
    if desc:
        prop["description"] = desc[:300]

    if json_type == "array":
        items_type = (pdef.get("items_type") or "string").lower()
        prop["items"] = {"type": type_map.get(items_type, "string")}

    enum_vals = pdef.get("enum_values")
    if enum_vals and isinstance(enum_vals, list):
        prop["enum"] = enum_vals

    return prop
