"""
LLM Re-ranker for FAISS search results.

Uses LLM to pick the best tool from FAISS top-N candidates.
This improves accuracy by leveraging LLM's understanding of context.
"""

import json
import logging
from typing import List, Optional, Dict
from dataclasses import dataclass

from config import get_settings
from services.config_loader import load_text
from services.text_normalizer import sanitize_for_llm
from services.utils.llm_json import parse_llm_json

logger = logging.getLogger(__name__)
def _get_settings():
    return get_settings()


@dataclass
class RerankResult:
    """Result from LLM reranking."""
    tool_id: str
    confidence: float
    reasoning: str


async def rerank_with_llm(
    query: str,
    candidates: List[Dict],  # List of {tool_id, score, description}
    top_k: int = 3,
    tool_documentation: Optional[Dict] = None,
    cost_tracker=None,
) -> List[RerankResult]:
    """
    Use LLM to rerank FAISS candidates and pick the best matches.

    Args:
        query: User query
        candidates: List of candidate tools from FAISS
        top_k: Number of results to return
        tool_documentation: Optional tool docs for context

    Returns:
        List of RerankResult with best matches
    """
    if not candidates:
        return []

    # Build candidate descriptions
    candidate_info = []
    for i, c in enumerate(candidates[:20]):  # Max 20 candidates
        tool_id = c.get('tool_id', '')

        # Get tool description: prefer tool_documentation, fall back to candidate's own description
        description = ""
        if tool_documentation and tool_id in tool_documentation:
            doc = tool_documentation[tool_id]
            description = doc.get('purpose', '')
        if not description:
            description = c.get('description', '')

        candidate_info.append({
            "index": i + 1,
            "tool_id": tool_id,
            "description": description[:300] if description else "N/A"
        })

    # Build prompt
    sanitized_query = sanitize_for_llm(query) if query else ""
    prompt = f"""Korisnik je postavio upit: "{sanitized_query}"

FAISS pretraga je vratila sljedeće kandidate (sortirano po sličnosti):

{json.dumps(candidate_info, ensure_ascii=False, indent=2)}

Tvoj zadatak je POREDATI kandidate po relevantnosti za korisnikov upit.

VAŽNA PRAVILA:
{load_text("flow", "reranker_domain_rules.txt")}

Vrati JSON odgovor u formatu:
{{
    "ranking": [<index1>, <index2>, <index3>, <index4>, <index5>],
    "confidence": <0.0-1.0>,
    "reasoning": "<kratko obrazloženje>"
}}

"ranking" sadrži indekse (1-{len(candidate_info)}) od najboljeg do najgoreg. Navedi barem 5 indeksa.
Odgovori SAMO s JSON-om, bez dodatnog teksta."""

    try:
        settings = _get_settings()
        from services.openai_client import get_reranker_client
        client = get_reranker_client()

        # Use gpt-4o via direct OpenAI if configured, else Azure gpt-4o-mini
        model = (settings.OPENAI_RERANKER_MODEL
                 if settings.OPENAI_RERANKER_API_KEY
                 else settings.AZURE_OPENAI_DEPLOYMENT_NAME)

        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Ti si ekspert za odabir pravog API alata. Odgovaraš SAMO u JSON formatu."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=400
        )

        # Persist to Redis via CostTracker
        if cost_tracker and hasattr(response, 'usage') and response.usage:
            try:
                await cost_tracker.record_usage(
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                )
            except Exception as e:
                logger.warning(f"CostTracker record failed in reranker: {e}")

        response_text = response.choices[0].message.content.strip()
        result = parse_llm_json(response_text)
        if result is None:
            raise json.JSONDecodeError("No JSON object found in LLM response", response_text, 0)

        confidence = result.get("confidence", 0.5)
        reasoning = result.get("reasoning", "")

        ranking = result.get("ranking")
        if not ranking:
            best_idx = result.get("best_match", 1) - 1
            ranking = [best_idx + 1]

        reranked = []
        seen = set()
        for idx_1based in ranking:
            idx = int(idx_1based) - 1
            if 0 <= idx < len(candidates) and idx not in seen:
                seen.add(idx)
                reranked.append(RerankResult(
                    tool_id=candidates[idx].get('tool_id', ''),
                    confidence=confidence,
                    reasoning=reasoning if len(reranked) == 0 else "LLM ranked"
                ))

        for i, c in enumerate(candidates[:top_k]):
            if i not in seen:
                reranked.append(RerankResult(
                    tool_id=c.get('tool_id', ''),
                    confidence=c.get('score', 0),
                    reasoning="FAISS candidate"
                ))

        return reranked[:top_k]

    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse LLM rerank response: {e}")
    except Exception as e:
        logger.warning(f"LLM rerank failed: {e}")

    # Fallback: return original order
    return [
        RerankResult(
            tool_id=c.get('tool_id', ''),
            confidence=c.get('score', 0),
            reasoning="FAISS fallback"
        )
        for c in candidates[:top_k]
    ]
