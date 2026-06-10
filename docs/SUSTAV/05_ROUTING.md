# 05 — ROUTING (NL → 1 od ~950 alata)

**Svrha**: Pronaći točan alat iz prirodnog jezika. Glavno usko grlo cijelog sustava (~35% na nasumičnom uzorku, ~90% na driver-rutini).

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/v2/intent_type.py` | 159 | LIVE | L2a: 4-way klasifikacija (question_about_self / action_or_complaint / flow_request / other), threshold 0.6 |
| `services/v2/driver_basics.py` | 279 | LIVE | L2b: cosine anchor za "vozač pita za vlastite podatke", threshold 0.78, circuit breaker |
| `services/router/catalog_scoper.py` | 210 | LIVE | Filtriranje kataloga: 950 → ~50-100 (tenant subset + metoda + drop_internal regex) |
| `services/router/anchor_index.py` | 200 | LIVE | L3 retrieval: top_k=50 po max-cosine preko ~11.400 anchor fraza, SHA256 cache |
| `services/router/anchor_vocab.py` | 340 | **DEV_ONLY** | HR rječnik (162 entity sinonima + verb sinonimi) — koristi samo `scripts/` za generiranje anchora |
| `services/router/llm_router.py` | 375 | LIVE | L3 LLM router: gpt-4o-mini tool-calling top-50, retry 3x, hallucination check |
| `services/router/tool_schema_builder.py` | 266 | LIVE | OpenAI schema builder: 64-char alias, supresija Filter/UseANDFor |
| `services/v2/confidence_gate.py` | 132 | **DEAD** | Stari L5 gate. **NIJE u živom putu** — zamijenjen Model A cascade-om + pending_clarify |
| `services/utils/vector.py` | 24 | LIVE | Cosine similarity bez numpy (dot/(norm·norm)), tolerira None/empty |

> ⚠️ **confidence_gate.py je DEAD**: engine.py:519 ga spominje SAMO u komentaru ("...replace the recognition + confidence_gate path entirely"). Nema `import confidence_gate` u živom kodu (potvrđeno grep-om). Kod je realan i kvalitetan, ali se ne izvršava. Prvi agent ga je krivo označio LIVE — ispravljeno ručnom provjerom.

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| `IntentTypeClassifier.classify` | intent_type.py:75 | query → (kind, confidence). LLM, safe fallback 0.6 |
| `DriverBasicsAnchor.match` | driver_basics.py:213 | query → BasicsMatch. Circuit breaker 3 fail/60s, keyword fallback |
| `CatalogScoper.scope` | catalog_scoper.py:88 | (tenant_id, methods, drop_internal) → frozenset operation_id |
| `AnchorIndex.top_k` | anchor_index.py:116 | (query, k=50) → [(tool_id, cosine_score)] |
| `LLMRouter.route` | llm_router.py:116 | query + history → RouterResult. Tool-calling, hallucination check, retry 3x |
| `ToolSchemaBuilder.build_for_tools` | tool_schema_builder.py:82 | tool[] → OpenAI tools=[]. 64-char limit, suppressed params |

## Ključni detalji (verificirano)

- **intent_type**: 4-way; `SAFE_FALLBACK_CONFIDENCE=0.6` (intent_type.py:46) prevodi nisko povjerenje u safe default.
- **driver_basics**: `STRONG_THRESHOLD=0.78` + `MIN_GAP_TO_NOISE=0.02` (driver_basics.py:106-107), **19** keyword regexa kao fallback (driver_basics.py:57-96). Circuit breaker: 3 fail u 60s → 60s lockout na keyword.
- **catalog_scoper**: čita `config/tenants/{id}/tool_subset.json`, fallback `_default` (`_tenant_subset` @ catalog_scoper.py:158). Internal helper regex (`_INTERNAL_HELPER_SUFFIX` @ catalog_scoper.py:55-58): `_(Distinct[A-Z][A-Za-z0-9]*|GroupBy|ProjectTo|metadata|DeleteByCriteria|multipatch)$`.
- **anchor_index**: `build` (anchor_index.py:54) embeds ~11.400 fraza (950 alata × ~12, log @ :90), SHA256 fingerprint cache. `top_k` vraća top-50 po **max cosine** preko svih fraza tog alata.
- **llm_router**: `TOP_K_RETRIEVAL=50` (llm_router.py:55, hardkodiran), `ROUTER_CALL_TIMEOUT_S=30.0` (:63), retry 3x na 429/5xx, hallucination check (pick mora biti u top-50).
- **tool_schema_builder**: `MAX_TOOL_NAME_LEN=64` (tool_schema_builder.py:38, aliasing), `_SUPPRESSED_PARAMS={Filter, UseANDFor}` (:46).
- **vector** (vector.py:12): `dot(a,b)/(norm_a·norm_b)`, vraća 0.0 na mismatch/None/empty.

## Izlaz

- `RouterResult` (llm_router.py:66-83, svih 9 polja kodnim redom): `tool_id` (Optional), `params` (dict), `confidence` [0-1], `rationale`, `alternatives` (list[dict] `[{tool_id, score}]` — u error putanjama `top[:5]` (≤5, llm_router.py:303/316), u success putanji filtrirano bez izabranog toola pa `[:3]` (≤3, :367-371)), `missing_required[]`, `error` (Optional; npr. `anchor_error:…` @ :140 ili `llm_error:…` @ :275), `top_candidates`, `anchor_score` (cosine najboljeg anchora).
- `BasicsMatch`: matched bool, score, gap, reasoning.
- top_k: `[(tool_id, cosine_score), ...]`.

## Config

- `config/processed_tool_registry.json` (950 alata)
- `config/tool_data.json` (anchor fraze)
- `config/tenants/{id}/tool_subset.json` (per-tenant scoping)

## Što NE radi

- Nije izvršavanje API poziva (to je L7 executor).
- Nije parser korisničkog jezika — samo bira alat.
- Nije sigurnosna provjera (L6 mutation gate + backend 403).
- Bira SAMO jedan alat po query (nema multi-tool sekvenciranja).
- Detektira `KIND_FLOW_REQUEST` ali ne izvršava flow (to je [06_FLOWS](06_FLOWS.md)).

## Caveati

- `anchor_score` može biti negativan [-1,1]; potrošači normaliziraju kao `(score+1)/2`.
- `TOP_K_RETRIEVAL=50`, drop_internal regex i confidence pragovi su HARDKODIRANI — promjena traži redeploy.
- Tenant catalog scoping se čita pri factory init(), ne honorira runtime promjene file-a bez restart/cache invalidacije.
- DriverBasicsAnchor: ako embedder padne 3x/60s, svi pozivi idu na keyword fallback do isteka lockouta.
