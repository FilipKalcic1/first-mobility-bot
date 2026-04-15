"""
Embedding Engine - Generate and manage embeddings for tool discovery.

Single responsibility: Generate embeddings using Azure OpenAI.

ARCHITECTURE:
    This engine solves the problem of 34% of API tools having no description
    in their Swagger definitions. It auto-generates searchable text from:
    1. URL path segments (/vehicles/{id}/mileage)
    2. OperationId (GetVehicleMileage)
    3. Input parameters
    4. Output keys

CROATIAN LANGUAGE SUPPORT:
    Users query in Croatian ("daj mi kilometražu") but API terms are English.
    This is addressed through three comprehensive dictionaries:

    1. PATH_ENTITY_MAP (350+ entries) [v3.4 expanded]
       - Maps English path segments to Croatian (nominative, genitive)
       - Example: "vehicle" -> ("vozilo", "vozila")
       - Coverage: Fleet, vehicles, people, bookings, locations, documents,
         maintenance, financial, status, access, equipment, categories, time,
         rentals, loyalty, vehicle specs, dimensions, fuel types

    2. OUTPUT_KEY_MAP (200+ entries) [v3.3 expanded]
       - Maps output field names to Croatian descriptions
       - Example: "mileage" -> "kilometražu"
       - Coverage: Vehicle data, location, status, documents, time, financial,
         identity, contact, person data, booking, lists, technical

    3. CROATIAN_SYNONYMS (60+ groups) [v3.3 expanded]
       - Maps Croatian roots to alternative user queries
       - Example: "vozil" -> ["auto", "automobil", "kola", "car", "autić"]
       - Categories: Fleet (10), People (8), Bookings (6), Locations (4),
         Documents (8), Financial (8), Maintenance (6), Communication (5),
         Data (5), Handover (5), Access (4), Categories (3)

FALLBACK MECHANISM:
    When Croatian mapping is unavailable, English terms are used with
    readable formatting (camelCase split). This ensures ALL tools contribute
    to embedding quality, not just mapped ones.

EVALUATION (embedding_evaluator.py):
    - Evaluation dataset: 200 queries across 14 categories
    - Industry-standard metrics: MRR, NDCG@5, NDCG@10, Hit@K
    - Coverage tracking via embedding_coverage.py

LIMITATIONS (Known Issues):
    - Dictionaries are manually maintained (not auto-generated)
    - Croatian morphology (case forms) are approximated, not linguistically verified
    - Requires periodic coverage analysis to identify gaps

RECOMMENDED IMPROVEMENTS:
    - Replace hardcoded translation with Croatian NLP (classla library)
    - Use translation API (DeepL) for unmapped terms
    - Add automated coverage tracking in CI
    - Implement MRR/NDCG evaluation on real user queries
"""

import logging
import re
from typing import Dict, List

from services.registry.entity_mappings import (
    PATH_ENTITY_MAP,
    OUTPUT_KEY_MAP,
    CROATIAN_SYNONYMS,
    SKIP_SEGMENTS,
)
from services.tool_contracts import (
    UnifiedToolDefinition,
    ParameterDefinition,
    DependencyGraph
)

logger = logging.getLogger(__name__)


class EmbeddingEngine:
    """
    Offline build helpers for tool embedding text and dependency graph.

    Used by scripts/sync_tools.py to regenerate config/processed_tool_registry.json.
    Runtime embedding generation now lives in services/faiss_vector_store.py.
    """

    def __init__(self) -> None:
        logger.debug("EmbeddingEngine initialized")

    def build_embedding_text(
        self,
        operation_id: str,
        service_name: str,
        path: str,
        method: str,
        description: str,
        parameters: Dict[str, ParameterDefinition],
        output_keys: List[str] = None
    ) -> str:
        """
        Build embedding text with auto-generated PURPOSE description.

        Enhanced inference from path, operationId, params, and outputs.

        Strategy:
        1. Generate PURPOSE from: path + operationId + method + params + outputs
        2. Include original description from Swagger
        3. List output fields for semantic matching

        Example:
            GET /vehicles/{id}/mileage + GetVehicleMileage
            → "Dohvaća kilometražu vozila. Vraća podatke o prijeđenim kilometrima."
        """
        # 1. Auto-generate purpose from structure (enhanced v3.0)
        purpose = self._generate_purpose(method, parameters, output_keys, path, operation_id)

        # 2. Build embedding text
        # FIXED: Removed operation_id (English) from embedding text
        # to ensure pure Croatian embeddings that match user queries
        parts = [
            purpose,  # Croatian auto-generated purpose
            description if description else "",
            # Removed: f"{method} {path}" - English, not helpful for Croatian queries
        ]

        # 3. Add output fields (human-readable)
        if output_keys:
            readable = [
                re.sub(r'([a-z])([A-Z])', r'\1 \2', k)
                for k in output_keys[:10]
            ]
            parts.append(f"Returns: {', '.join(readable)}")

        # 4. Add synonyms for better query matching
        synonyms = self._get_synonyms_for_purpose(purpose)
        if synonyms:
            parts.append(f"Sinonimi: {', '.join(synonyms)}")

        text = ". ".join(p for p in parts if p)

        if len(text) > 1500:
            text = text[:1500]

        return text

    # Imported from services.registry.entity_mappings
    PATH_ENTITY_MAP = PATH_ENTITY_MAP
    OUTPUT_KEY_MAP = OUTPUT_KEY_MAP
    CROATIAN_SYNONYMS = CROATIAN_SYNONYMS

    def _generate_purpose(
        self,
        method: str,
        parameters: Dict[str, ParameterDefinition],
        output_keys: List[str],
        path: str = "",
        operation_id: str = ""
    ) -> str:
        """
        Auto-generate purpose from API structure (v3.0 - Enhanced).

        Infers from:
        - HTTP method → action (Dohvaća/Kreira/Ažurira/Briše)
        - PATH → entity (iz /vehicles/ → vozilo)
        - operationId → action + entity (GetVehicleMileage → dohvaća kilometražu vozila)
        - Input params → context (za vozilo/korisnika/period)
        - Output keys → result (kilometražu/registraciju/status)
        """
        # 1. Action from method
        actions = {
            "GET": "Dohvaća",
            "POST": "Kreira",
            "PUT": "Ažurira",
            "PATCH": "Ažurira",
            "DELETE": "Briše"
        }
        action = actions.get(method.upper(), "Obrađuje")

        # 2. Extract entities from PATH (most reliable source)
        path_entities = self._extract_entities_from_path(path)

        # 3. Extract entities from operationId
        op_entities, op_action_hint = self._parse_operation_id(operation_id)

        # 4. Context from input parameters
        param_context = []
        has_time = False

        if parameters:
            names = [p.name.lower() for p in parameters.values()]

            # Check each parameter name against entity map
            for name in names:
                for key, (singular, _) in self.PATH_ENTITY_MAP.items():
                    if key in name and singular not in param_context:
                        param_context.append(singular)
                        break

            has_time = (
                any(x in n for n in names for x in ["from", "start", "begin"]) and
                any(x in n for n in names for x in ["to", "end", "until"])
            )

        # 5. Result from output keys
        result = []

        if output_keys:
            keys_lower = [k.lower() for k in output_keys]

            for key in keys_lower:
                # Check against output key map
                for pattern, translation in self.OUTPUT_KEY_MAP.items():
                    if pattern in key and translation not in result:
                        result.append(translation)
                        if len(result) >= 4:
                            break
                if len(result) >= 4:
                    break

        # 6. Combine all sources to build purpose
        # Priority: path_entities > op_entities > param_context
        all_entities = []
        seen = set()

        for entity in path_entities + op_entities + param_context:
            if entity.lower() not in seen:
                all_entities.append(entity)
                seen.add(entity.lower())

        # Build the sentence
        purpose = action

        # Add result/what we're getting
        if result:
            purpose += " " + ", ".join(result[:3])
        elif op_action_hint:
            purpose += " " + op_action_hint
        elif method == "GET":
            purpose += " podatke"
        elif method == "POST":
            purpose += " novi zapis"
        elif method in ("PUT", "PATCH"):
            purpose += " postojeće podatke"
        elif method == "DELETE":
            purpose += " zapis"

        # Add context (what entity)
        if all_entities:
            # Use genitive form for "za X"
            entity_genitives = []
            for entity in all_entities[:2]:
                # Try to find genitive form
                for key, (singular, genitive) in self.PATH_ENTITY_MAP.items():
                    if singular == entity:
                        entity_genitives.append(genitive)
                        break
                else:
                    entity_genitives.append(entity)

            purpose += " za " + ", ".join(entity_genitives)

        if has_time:
            purpose += " u zadanom periodu"

        return purpose

    # Imported from services.registry.entity_mappings
    SKIP_SEGMENTS = SKIP_SEGMENTS

    def _extract_entities_from_path(self, path: str) -> List[str]:
        """
        Extract entities from API path segments.

        Uses Croatian mapping when available, falls back to English
        (with space-separated camelCase) for unmapped terms.
        This ensures ALL paths contribute to embedding quality.
        """
        if not path:
            return []

        entities = []
        # Remove path parameters like {vehicleId}
        clean_path = re.sub(r'\{[^}]+\}', '', path)
        # Split by / and -
        segments = re.split(r'[/\-_]', clean_path.lower())

        for segment in segments:
            if not segment or len(segment) < 3:
                continue

            # Skip common API prefixes
            if segment in self.SKIP_SEGMENTS:
                continue

            # Check against entity map (Croatian translation available)
            if segment in self.PATH_ENTITY_MAP:
                singular, _ = self.PATH_ENTITY_MAP[segment]
                if singular not in entities:
                    entities.append(singular)
            else:
                # Try partial match for compound words
                found = False
                for key, (singular, _) in self.PATH_ENTITY_MAP.items():
                    if key in segment and singular not in entities:
                        entities.append(singular)
                        found = True
                        break

                # FALLBACK: Use English term with readable formatting
                # This ensures unmapped terms still contribute to embedding
                if not found and segment not in entities:
                    # Convert camelCase/compound to readable: "vehicleinfo" -> "vehicle info"
                    readable = self._make_readable(segment)
                    if readable not in entities:
                        entities.append(readable)

        return entities[:4]  # Increased limit for fallback terms

    def _make_readable(self, term: str) -> str:
        """
        Convert technical term to human-readable format.

        Examples:
            vehicleinfo -> vehicle info
            fuelconsumption -> fuel consumption
            getbyid -> get by id
        """
        # Insert space before uppercase letters (camelCase)
        readable = re.sub(r'([a-z])([A-Z])', r'\1 \2', term)
        # Insert space between letters and numbers
        readable = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', readable)
        readable = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', readable)
        return readable.lower()

    def _parse_operation_id(self, operation_id: str) -> tuple:
        """
        Parse operationId to extract action and entities.

        Uses Croatian mapping when available, falls back to English
        for unmapped terms to ensure all operation IDs contribute.
        """
        if not operation_id:
            return [], ""

        # Split CamelCase: GetVehicleMileage -> ['Get', 'Vehicle', 'Mileage']
        words = re.findall(r'[A-Z][a-z]*|[a-z]+', operation_id)

        if not words:
            return [], ""

        entities = []
        action_hint = ""

        # Skip common action verbs
        action_verbs = {"get", "create", "update", "delete", "post", "put",
                        "patch", "list", "find", "search", "add", "remove",
                        "set", "fetch", "retrieve", "check", "validate",
                        "by", "for", "all", "id", "ids", "the", "and", "or"}

        for word in words:
            word_lower = word.lower()

            if word_lower in action_verbs or len(word_lower) < 3:
                continue

            # Check if word maps to an entity (Croatian translation)
            if word_lower in self.PATH_ENTITY_MAP:
                singular, _ = self.PATH_ENTITY_MAP[word_lower]
                if singular not in entities:
                    entities.append(singular)
            # Check output key map for action hints
            elif word_lower in self.OUTPUT_KEY_MAP:
                if not action_hint:
                    action_hint = self.OUTPUT_KEY_MAP[word_lower]
            # FALLBACK: Use English word as-is (readable format)
            # This ensures unmapped operation IDs still contribute
            elif word_lower not in entities:
                entities.append(word_lower)

        return entities[:3], action_hint  # Increased limit for fallback

    def _get_synonyms_for_purpose(self, purpose: str) -> List[str]:
        """
        Extract synonyms for entities mentioned in the purpose.

        This helps RAG match user queries that use alternative words.
        E.g., user says "auto" but API uses "vozilo" - synonyms bridge this gap.
        """
        if not purpose:
            return []

        synonyms = []
        purpose_lower = purpose.lower()

        # Check each entity in CROATIAN_SYNONYMS
        for entity, syn_list in self.CROATIAN_SYNONYMS.items():
            # If entity appears in purpose, add its synonyms
            if entity.lower() in purpose_lower:
                for syn in syn_list:
                    if syn.lower() not in purpose_lower and syn not in synonyms:
                        synonyms.append(syn)

        return synonyms[:8]  # Limit to 8 synonyms

    def build_dependency_graph(
        self,
        tools: Dict[str, UnifiedToolDefinition]
    ) -> Dict[str, DependencyGraph]:
        """
        Build dependency graph for automatic tool chaining.

        Identifies which tools can provide outputs needed by other tools.

        Args:
            tools: Dict of all tools

        Returns:
            Dict of DependencyGraph by tool_id
        """
        logger.info("Building dependency graph...")
        graph = {}

        for tool_id, tool in tools.items():
            # Find parameters that need FROM_TOOL_OUTPUT
            output_params = tool.get_output_params()
            required_outputs = list(output_params.keys())

            # Find tools that provide these outputs
            provider_tools = []
            for req_output in required_outputs:
                providers = self._find_providers(req_output, tools)
                provider_tools.extend(providers)

            if required_outputs:
                graph[tool_id] = DependencyGraph(
                    tool_id=tool_id,
                    required_outputs=required_outputs,
                    provider_tools=list(set(provider_tools))
                )

        logger.info(f"Built dependency graph: {len(graph)} tools with dependencies")
        return graph

    def _find_providers(
        self,
        output_key: str,
        tools: Dict[str, UnifiedToolDefinition]
    ) -> List[str]:
        """Find tools that provide given output key."""
        providers = []

        for tool_id, tool in tools.items():
            if output_key in tool.output_keys:
                providers.append(tool_id)
            # Case-insensitive match
            elif any(
                ok.lower() == output_key.lower()
                for ok in tool.output_keys
            ):
                providers.append(tool_id)

        return providers
