# 09 — FORMATTER (JSON → hrvatska poruka)

**Svrha**: Dvostazni formatter — LLM-grounded (primarni) za prirodne odgovore, ili deterministički template-i (fallback) ako LLM padne. Plus prijevod 4xx grešaka u HR.

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/formatter/llm_formatter.py` | 204 | LIVE | Primarni: LLM-grounded JSON→HR + output_sanitizer + PII scrub |
| `services/v2/formatter.py` | 403 | LIVE | Fallback: 8 determinističkih template-a + "nije točno" hint appender |
| `services/v2/api_error_translator.py` | 207 | LIVE | LLM prijevod 4xx → kratka HR poruka (prompt ≤200, hard-cap 300 znakova) + Redis cache |
| `services/text_normalizer.py` | 337 | LIVE | Normalizacija dijakritika (č→c) + sinonimi; **formatter ga koristi** za field_hint rezoluciju (smjer: formatter → text_normalizer) |
| `services/v2/output_sanitizer.py` | 141 | LIVE | (dijeli s [04_GUARDS]) defang [SYSTEM:] stringova prije LLM-a |
| `services/v2/pii_scrubber.py` | 102 | LIVE | (dijeli s [04_GUARDS]) PII na LLM izlazu + API error body |

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| `V2Engine._format_reply` | engine.py:801 | Async entry: zove `formatter_llm.format()`, pada na template fallback ako LLM padne (engine.py:813-826) |
| `LLMFormatter.format` | llm_formatter.py:67 | sanitize (79) → prune ako >6000 znakova (85) → LLM poziv s konfigurabilnim `self._deployment` (default gpt-4o-mini), temp=0 (128-136) → PII scrub (156) |
| `format_response` | formatter.py:102 | Render template_id; append "nije točno" hint na READ/MUTATE template-e |
| `ApiErrorTranslator.translate` | api_error_translator.py:73 | 4xx→HR (prompt traži ≤200 znakova; kod hard-cap reže na 300, api_error_translator.py:207), Redis cache TTL 3600s |

## Ključne komponente

| Simbol | Lokacija | Što radi |
|---|---|---|
| `LLMFormatter` klasa | llm_formatter.py:42 | init s llm_client, deployment, registry (output_keys hint), pii_scrubber |
| `_prune` | llm_formatter.py:165 | >6000 znakova: liste→prvih 15; dict→projekcija na output_keys; fallback drop >1500-znak polja |
| `format_response` | formatter.py:102 | 8 template-a (130-175) ili smart-default |
| `_format_smart_default` | formatter.py:271 | template_id=None: **DIO 6 fix (2026-05-29) unwrap envelope** (Result/Results/Items/items/data/Data/value na :284) → list vs dict → vehicle_summary ili list_with_count |
| `_maybe_append_hint` | formatter.py:82 | "Ako nije točno, napiši 'nije točno'." na READ (:75); MUTATE varijanta (:76-79) |
| `ApiErrorTranslator` | api_error_translator.py:60 | 4xx status+body → LLM (ŠTA krivo + KAKO popraviti). Cache sha1(status|tool_id|body[:300])[:16] |
| `normalize_diacritics` | text_normalizer.py:243 | č→c, đ→d, š→s, ž→z; za field_hint (formatter.py:366) |
| `_resolve_field_hint_to_key` | formatter.py:376 | "koliko km godišnje" → YearlyMileage (diacritic-stripped _HINT_ALIASES + combo-rules) |

## 8 template-a (formatter.py:130-175)

vehicle_data_summary, vehicle_data_field, list_with_count, empty_result, mutation_success, mutation_failed, generic_value, fallback.

## Redis ključ

- `api_err_translate:{h}` — ApiErrorTranslator cache, TTL 3600s (1h).

## Config

- `config/linguistic/typo_synonyms.json` — optional, lazy u text_normalizer (graceful degradation ako missing).

## Tok (primarni vs fallback)

1. **Primarni**: `LLMFormatter.format` — grounded JSON→HR, bira polje po upitu, verbatim vrijednosti, output_sanitizer + PII scrub.
2. **Fallback**: ako LLM padne/timeout → `formatter.format_response` (template). DIO 6 envelope-unwrap štiti list endpointe na fallbacku.
3. **Greška**: 4xx → `ApiErrorTranslator` (HR), 5xx → generička "Tehnički problem".

## Što NE radi

- Ne generira novi JSON — samo prikaz postojećeg backend JSON-a.
- Ne routa zahtjeve (to je L3), ne izvršava (to je L7).
- Ne cachira odgovore (samo 4xx prijevode).
- Hardkodirano hrvatski (nije multi-language).

## Caveati

- **DIO 6 fix (formatter.py:279-288, 2026-05-29)**: unwrap envelope ključeva PRIJE list-vs-dict odluke; ranije bi list[] u dict wrapperu bio tretiran kao dict → "Nemam podataka o vozilu" za list upite.
- Hint se appenda SAMO na READ + MUTATE template-e; empty/fallback/failed preskoče (da se poruke ne kumuliraju).
- `date_value` i `raw_passthrough` su u `_READ_TEMPLATES` (za hint), ali se **nikad ne renderiraju** kao zasebne grane — padaju kroz unknown-template fallback.
- ApiErrorTranslator silent: LLM greška/cache miss → None → engine generička poruka (best-effort).
- LLMFormatter truncation: >6000 znakova i nakon pruninga → "... (truncated)".
