"""L8 LLM formatter — backend JSON → concise Croatian reply.

Replaces the template-based formatter (only ~20 templates for 950 tools).
The LLM sees the raw (sanitized) JSON and writes a short Croatian answer
grounded in the data. No template registry, no per-tool special casing.

Pipeline:
1. Output-sanitize api_data → defang any [SYSTEM:...] / "ignore previous"
   strings embedded in fields the user controls upstream.
2. Truncate huge payloads via registry.output_keys hint (prefer those
   fields when the JSON is too large to fit in the prompt).
3. LLM call (gpt-4o-mini, temperature 0). System prompt: strict grounding.
4. Optional: scrub PII patterns on the way out (cheap defense in depth).

Cost: ~$0.0005 per response. Latency +600-1000ms.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from services.v2 import output_sanitizer
from services.v2.pii_scrubber import PIIScrubber

logger = logging.getLogger(__name__)


FORMAT_TEMPERATURE = 0.0
FORMAT_MAX_TOKENS = 500
MAX_JSON_PROMPT_CHARS = 6000  # truncate api_data prompt at this size


@dataclass
class FormatResult:
    text: str
    error: Optional[str] = None
    truncated: bool = False  # set when api_data was pruned to fit budget


class LLMFormatter:
    """Stateless. One instance per engine."""

    def __init__(
        self,
        *,
        llm_client,
        deployment_name: str,
        registry: dict,
        pii_scrubber: Optional[PIIScrubber] = None,
    ):
        self._llm = llm_client
        self._deployment = deployment_name
        self._registry = registry
        # Pre-index registry tools by id for fast output_keys lookup
        self._output_keys_by_tool: dict[str, list[str]] = {}
        for t in registry.get("tools", []):
            op = t.get("operation_id")
            keys = t.get("output_keys") or []
            if op:
                self._output_keys_by_tool[op] = list(keys)
        # Defense-in-depth output PII scrub. Defaults to PIIScrubber()
        # using its standard Croatian patterns.
        self._scrubber = pii_scrubber or PIIScrubber()

    async def format(
        self,
        *,
        query: str,
        tool_id: str,
        api_data: Any,
        identity_summary: str = "",
    ) -> FormatResult:
        """Format api_data as Croatian reply. Never raises."""
        # Step 1 — sanitize attacker-controlled strings in api_data.
        # output_sanitizer.sanitize returns (sanitized_copy, warnings).
        try:
            safe_data, _warnings = output_sanitizer.sanitize(api_data)
        except Exception as e:  # noqa: BLE001
            logger.warning("output sanitizer failed: %s", e)
            safe_data = api_data

        # Step 2 — truncate to keep prompt size manageable
        pruned, truncated = self._prune(safe_data, tool_id)

        # Step 3 — LLM call
        try:
            data_json = json.dumps(pruned, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            data_json = str(pruned)
        if len(data_json) > MAX_JSON_PROMPT_CHARS:
            data_json = data_json[:MAX_JSON_PROMPT_CHARS] + "\n... (truncated)"
            truncated = True

        system = (
            "Ti si Croatian fleet-management WhatsApp bot. Generiraj KRATAK "
            "(1-5 rečenica), pristojan, prirodan Croatian odgovor iz "
            "priloženog JSON-a od MobilityOne backenda. PRAVILA: "
            "1) KORISTI SAMO podatke iz JSON-a. NE izmišljaj. NE izmišljaj "
            "polja koja nisu u JSON-u. "
            "2) Ako je polje prazno ili null, kaži 'nema podatka' za to polje. "
            "3) Ako JSON sadrži listu, daj sažetak (broj stavki + 3-5 ključnih "
            "primjera). NE nabrajaj sve stavke. "
            "4) Datumi: format 'dd.mm.yyyy.' Brojevi: tisuće s razmakom (1 234). "
            "5) Polja koja vraćaš biraj prema upitu korisnika — ne ispisuj "
            "cijeli JSON; izdvoji ono što je relevantno. "
            "6) Bez markdown ograde, bez code-block, bez '*' i '**'. "
            "Čisti tekst. "
            "7) Bez ispričavanja, bez 'Naravno', bez 'Evo' — odmah daj odgovor."
        )

        identity_block = (
            f"Identity korisnika: {identity_summary}"
            if identity_summary else ""
        )
        user_prompt = (
            (identity_block + "\n\n" if identity_block else "")
            + f"Tool koji je pozvan: {tool_id}\n\n"
            + f'Korisnikov upit:\n"""\n{query}\n"""\n\n'
            + f"JSON od backenda:\n```json\n{data_json}\n```\n\n"
            + "Napiši Croatian odgovor."
        )

        try:
            response = await self._llm.chat.completions.create(
                model=self._deployment,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=FORMAT_TEMPERATURE,
                max_tokens=FORMAT_MAX_TOKENS,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("LLM formatter call failed: %s", e)
            return FormatResult(
                text="Tehnički problem s formatiranjem odgovora. Pokušaj ponovo.",
                error=f"llm_error:{type(e).__name__}",
                truncated=truncated,
            )

        text = (response.choices[0].message.content or "").strip()
        if not text:
            return FormatResult(
                text="(prazan odgovor)",
                error="empty_response",
                truncated=truncated,
            )

        # Step 4 — defense-in-depth: scrub PII patterns the LLM might
        # have echoed (e.g. OIB from a person record). The scrubber
        # already produces user-readable placeholders.
        scrubbed = self._scrubber.scrub(text)
        return FormatResult(
            text=scrubbed.scrubbed_text,
            error=None,
            truncated=truncated,
        )

    # ---- internals ----

    def _prune(self, data: Any, tool_id: str) -> tuple[Any, bool]:
        """If api_data is huge, project to output_keys-listed fields only.

        Returns (pruned_data, was_truncated).
        """
        if data is None:
            return None, False
        # Estimate size cheaply
        try:
            est = len(json.dumps(data, ensure_ascii=False))
        except (TypeError, ValueError):
            return data, False
        if est <= MAX_JSON_PROMPT_CHARS:
            return data, False

        # If it's a list, keep first 15 items (LLM doesn't need 200 rows
        # to write "imaš 200 stavki, evo prvih...")
        if isinstance(data, list):
            head = data[:15]
            return head, True

        # If it's a dict and we have output_keys, project to those keys.
        if isinstance(data, dict):
            keys = self._output_keys_by_tool.get(tool_id) or []
            if keys:
                projected = {k: data[k] for k in keys if k in data}
                if projected:
                    return projected, True
            # Fallback: drop any field whose value is huge
            small = {}
            for k, v in data.items():
                try:
                    s = len(json.dumps(v, ensure_ascii=False))
                except (TypeError, ValueError):
                    s = 0
                if s < 1500:
                    small[k] = v
            return small or data, True

        return data, False
