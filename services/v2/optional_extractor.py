"""LLM-based extractor for OPTIONAL parameter values.

Used by engine after the user has answered the "Opcionalno možeš dodati: X, Y"
prompt with free-text. The LLM extracts whichever fields the user actually
mentioned and returns them as a dict; engine merges this into collected params
before the mutation gate.

Why LLM (not regex):
  Filip 2026-05-17 decision. Real registry has 628/950 tools with optional
  params (avg 4.3, max 54). Iteration "ask each one by one" is a UX
  catastrophe. A single LLM call with a dynamically-built tool schema lets
  the user say "kasnim po pola sata, tip hitno" and the bot pulls
  {Notes: "kasnim po pola sata", Type: "hitno"} out cleanly.

Safety:
  This module NEVER raises. LLM errors, network timeouts, malformed JSON,
  empty tool call — all return {}. Caller then executes with required params
  only. No "tehnički problem" message because of optional extract failure.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "Ti si ekstraktor opcionalnih polja iz hrvatske korisničke poruke. "
    "Pozovi tool 'fill_optionals' SAMO sa onim poljima koja je korisnik "
    "izričito spomenuo. Ako korisnik ništa konkretno nije rekao ili je "
    "odgovorio nečim negativnim/odbacujućim, pozovi tool s praznim args-ima "
    "ili ne pozivaj tool uopće."
)

_EXTRACT_TOOL_NAME = "fill_optionals"


# JSON Schema type for a given registry `param_type`. Defensive — anything
# we don't recognize falls through to string.
_TYPE_MAP = {
    "integer": "integer",
    "number":  "number",
    "boolean": "boolean",
    "string":  "string",
    "array":   "array",
    "object":  "object",
}


def _build_tool_schema(optional_specs: dict) -> dict:
    """Build an OpenAI function-tool schema where every param is optional
    (no `required` list) — we want the LLM to fill only what the user said."""
    properties: dict = {}
    for pname, pdef in optional_specs.items():
        if not isinstance(pdef, dict):
            continue
        ptype = _TYPE_MAP.get(
            (pdef.get("param_type") or "string").lower(),
            "string",
        )
        prop: dict = {"type": ptype}
        desc = (pdef.get("description") or "").strip()
        if desc:
            prop["description"] = desc[:200]
        properties[pname] = prop
    return {
        "type": "function",
        "function": {
            "name": _EXTRACT_TOOL_NAME,
            "description": (
                "Fill any optional fields the user mentioned. "
                "Omit fields the user did not mention."
            ),
            "parameters": {
                "type": "object",
                "properties": properties,
                # Deliberately NO "required" — everything is optional.
            },
        },
    }


class OptionalParamExtractor:
    """Tanki LLM wrapper. Mirror pattern of IntentTypeClassifier."""

    def __init__(self, llm_client, deployment_name: str):
        self._client = llm_client
        self._deployment = deployment_name

    async def extract(
        self,
        user_text: str,
        optional_specs: dict,
    ) -> dict:
        """Return {param_name: value} for fields the user mentioned.

        Args:
          user_text: free-text Croatian message from user.
          optional_specs: subset of registry parameters dict (only the
            optional user_input params we offered).

        Returns: dict (possibly empty). NEVER raises.
        """
        if not user_text or not user_text.strip():
            return {}
        if not optional_specs:
            return {}

        try:
            tool_schema = _build_tool_schema(optional_specs)
        except Exception as e:  # noqa: BLE001 — schema build failure is silent
            logger.warning("optional schema build failed: %s", e)
            return {}

        if not tool_schema["function"]["parameters"]["properties"]:
            # No usable properties (all specs invalid) — nothing to extract.
            return {}

        try:
            response = await self._client.chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                tools=[tool_schema],
                tool_choice="auto",
                temperature=0,
                max_tokens=400,
            )
        except Exception as e:  # noqa: BLE001 — silent fallback
            logger.warning("optional extract LLM error: %s", e)
            return {}

        return _parse_response(response, optional_specs)


def _parse_response(response, optional_specs: dict) -> dict:
    """Pull tool_call args out of the LLM response. Drop unknown / empty
    fields. Always returns a dict (possibly empty)."""
    try:
        msg = response.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return {}

    tool_calls: Optional[list] = getattr(msg, "tool_calls", None) or []
    if not tool_calls:
        return {}

    call = tool_calls[0]
    fn = getattr(call, "function", None)
    if fn is None or getattr(fn, "name", "") != _EXTRACT_TOOL_NAME:
        return {}

    args_raw = getattr(fn, "arguments", "") or "{}"
    try:
        args = json.loads(args_raw)
    except (json.JSONDecodeError, TypeError):
        return {}

    if not isinstance(args, dict):
        return {}

    allowed = set(optional_specs.keys())
    cleaned: dict = {}
    for k, v in args.items():
        if k not in allowed:
            continue
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        cleaned[k] = v
    return cleaned
