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
    for i, c in enumerate(candidates[:10]):  # Max 10 candidates
        tool_id = c.get('tool_id', '')
        score = c.get('score', 0)

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
            "faiss_score": round(score, 4),
            "description": description[:200] if description else "N/A"
        })

    # Build prompt
    sanitized_query = sanitize_for_llm(query) if query else ""
    prompt = f"""Korisnik je postavio upit: "{sanitized_query}"

FAISS pretraga je vratila sljedeće kandidate (sortirano po sličnosti):

{json.dumps(candidate_info, ensure_ascii=False, indent=2)}

Tvoj zadatak je odabrati TOČAN alat za korisnikov upit.

VAŽNA PRAVILA:
1. Ako korisnik traži "NAJNOVIJE" ili "LATEST" - odaberi alat s "Latest" u imenu
2. Ako korisnik traži "DOKUMENT" nekog entiteta - odaberi alat s "_documents" ili "_documentId"
3. Ako korisnik traži "METAPODATKE" - odaberi alat s "_metadata"
4. Ako korisnik traži "POTPUNO ZAMIJENI" - odaberi PUT, ne PATCH
5. Ako korisnik traži "DJELOMIČNO AŽURIRAJ" - odaberi PATCH, ne PUT
6. Ako korisnik traži "VIŠE STAVKI ODJEDNOM" - odaberi "_multipatch"

Vrati JSON odgovor u formatu:
{{
    "best_match": <index najboljeg alata (1-10)>,
    "confidence": <0.0-1.0>,
    "reasoning": "<kratko obrazloženje>"
}}

Odgovori SAMO s JSON-om, bez dodatnog teksta."""

    try:
        from services.openai_client import get_openai_client
        client = get_openai_client()

        response = await client.chat.completions.create(
            model=_get_settings().AZURE_OPENAI_DEPLOYMENT_NAME,
            messages=[
                {"role": "system", "content": "Ti si ekspert za odabir pravog API alata. Odgovaraš SAMO u JSON formatu."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=200
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

        best_idx = result.get("best_match", 1) - 1  # Convert to 0-indexed
        confidence = result.get("confidence", 0.5)
        reasoning = result.get("reasoning", "")

        if 0 <= best_idx < len(candidates):
            best_candidate = candidates[best_idx]

            # Return reranked results with best match first
            reranked = [
                RerankResult(
                    tool_id=best_candidate.get('tool_id', ''),
                    confidence=confidence,
                    reasoning=reasoning
                )
            ]

            # Add other candidates in original order
            for i, c in enumerate(candidates[:top_k]):
                if i != best_idx:
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
