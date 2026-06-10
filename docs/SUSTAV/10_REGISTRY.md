# 10 — REGISTRY (baza ~950 alata)

**Svrha**: Učitava 950 pred-procesiranih alata iz `config/processed_tool_registry.json` u memoriju i izlaže ih executoru kroz `spec_for`/`method_of`. SwaggerParser i EmbeddingEngine su offline build-helperi za regeneraciju registryja.

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/registry/__init__.py` | 225 | LIVE | `ToolRegistry` facade — initialize() učita 950 alata + 144 dependencyja; izlaže get_tool/spec_for/method_of/resolve_tool_id |
| `services/tool_contracts.py` | 182 | LIVE | Pydantic: `UnifiedToolDefinition`, `ParameterDefinition`, `DependencyGraph`, `DependencySource` |
| `services/registry/tool_store.py` | 106 | LIVE | In-memory dict alata + retrieval/mutation setovi + dependency_graph + `_lower_index` (O(1) resolve) |
| `services/registry/swagger_parser.py` | 660 | PARTIAL | SwaggerParser. Instanciran u ToolRegistry.__init__ ali parse path (parse_spec/_classify) ide samo preko `scripts/sync_tools.py` (offline) |
| `services/registry/embedding_engine.py` | 462 | **DEV_ONLY** | Gradi HR embedding_text + dependency_graph. NIJE u živom chainu; samo `scripts/sync_tools.py` |
| `services/registry/entity_mappings.py` | 547 | **DEV_ONLY** | HR rječnici (PATH_ENTITY_MAP, OUTPUT_KEY_MAP, CROATIAN_SYNONYMS). Importan samo od embedding_engine |

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| `ToolRegistry.initialize` | registry/__init__.py:83 | Učita `processed_tool_registry.json` (to_thread), malformani alat se preskoči (ne ruši cijeli registry), is_ready=True. **Nema live-Swagger fallbacka** |
| `ToolRegistry.spec_for` | registry/__init__.py:171 | Executor-kontrakt: `{service, path, method, tenant_scoped:True, param_locations, context_params, required_context_params}`. Zove ga executor.py:91 |
| `ToolRegistry.method_of` | registry/__init__.py:209 | HTTP metoda. Zove executor.py:82 + engine.py (1195,1497,2037,2117) |
| `ToolRegistry.get_tool` | registry/__init__.py:148 | exact match → UnifiedToolDefinition |
| `ToolRegistry.resolve_tool_id` | registry/__init__.py:151 | O(1) case-insensitive (preko `_lower_index`) |
| `SwaggerParser.parse_spec` | swagger_parser.py:164 | **Offline**: fetch+parse Swagger → UnifiedToolDefinition |
| `EmbeddingEngine.build_embedding_text` | embedding_engine.py:90 | **Offline**: HR embedding tekst (cap 1500) |

## Ključne komponente

| Simbol | Lokacija | Što radi |
|---|---|---|
| `UnifiedToolDefinition` | tool_contracts.py:80 | operation_id, service_name, path, method, parameters, required_params, output_keys, is_retrieval, is_mutation, tags, embedding_text, version_hash. `validate_method` (:123) uppercase + ograniči na 5 metoda. `model_post_init` (:131) auto is_retrieval/is_mutation + version_hash (md5[:16]) |
| `ParameterDefinition.preferred_operator` coerce | tool_contracts.py:44 | Optional[str] default "(=)"; validator None→"(=)" (:53). is_filterable Optional[bool] None→False (:58). **CRIT-4 fix**: registry je serijalizirao null-ove → 25 alata padalo kao string_type |
| `ParameterDefinition.validate_location` | tool_contracts.py:71 | location ∈ {query, body, path, header}, default "body" |
| `_classify_context_parameter` | swagger_parser.py:317 | Klasificira param kao context (auto-inject UUID) vs user_input (weighted scoring + strong-signal prag) |
| `_parse_parameter` | swagger_parser.py:412 | location iz `param['in']`; "header" → None (odbaci) |
| `ToolStore` | tool_store.py:15 | tools dict + `_lower_index` + add_tool puni retrieval/mutation setove |
| tool_data derivacija (factory) | engine.py:2552 | Učita `config/tool_data.json` (union-merge) → registry-shape + TKB-shape + anchors-shape. dependency_graph prazan → čita iz processed (CRIT-2) |

## Dva registry filea (KLJUČNO)

- **`config/processed_tool_registry.json`** (950 alata + 144 deps) → učita `ToolRegistry.initialize` (registry/__init__.py:94). Izvor za executor spec.
- **`config/tool_data.json`** (950 alata, dict po op_id, **prazan dependency_graph**) → učita V2Engine factory (engine.py:2552), NE ToolRegistry. Izvor za routing (anchors, intent_summary).

## Config

`processed_tool_registry.json`, `tool_data.json`, `context_param_schemas.json`, `domain/path_entity_map.json`.

## Što NE radi

- Ne radi vektorski retrieval (to je anchor_index).
- **Ne dohvaća Swagger uživo u runtimeu** — `initialize()` prima `swagger_sources` ali ih ignorira; jedini izvor = pred-procesirani JSON. Live fetch radi samo `scripts/sync_tools.py`.
- Ne izvršava HTTP (to je executor).
- Ne validira parametre po vrijednosti (param_validator obrisan 2026-05-09).
- ToolRegistry NE učita `tool_data.json` — to radi V2Engine factory direktno.

## Caveati

- **swagger_parser PARTIAL**: SwaggerParser se instancira (registry/__init__.py:48) ali u runtimeu se čita samo `context_param_fallback`; cijeli parse path zove samo scripts + testovi.
- **embedding_engine + entity_mappings DEV_ONLY**: grep nije našao import iz worker/main/v2/router; samo `scripts/sync_tools.py` + embedding_engine + testovi.
- 950 alata + 144 dependencyja potvrđeno `json.load`-om. tool_data.json isto 950, prazan dependency_graph.
- Malformani alat pri initialize se preskoči s warningom — ne ruši cijeli registry (FAZA 12.6 fix).
