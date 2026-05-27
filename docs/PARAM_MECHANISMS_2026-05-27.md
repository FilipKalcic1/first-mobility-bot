# Mehanizmi: formatiranje / dobro mjesto / pravilno / traženje parametara

Referenca "što i kako" za unos parametara. Svaka stavka = konkretan mehanizam + gdje u kodu. Na dnu: exhaustivan test preko svih 950 toolova.

> Dva puta kojima vrijednost ulazi u poziv: **LLM-extract** (LLM izvuče iz poruke) i **param-ask** (bot pita kad fali). Oba završe u istom executoru.

---

## A) FORMATIRANJE (vrijednost u ispravan oblik)

| Mehanizam | Gdje | Što radi |
|---|---|---|
| `parse_param_value` | [param_ui.py](../services/v2/param_ui.py) | param-ask odgovori → tip: **int** (skini ne-znamenke; `12,5`/`5, 30000` → re-ask), **number** (HR: `1.500,75`→1500.75, `1.500`→re-ask, `12,5`→12.5), **bool** (da/ne setovi, dvosmisleno→re-ask), **datum** → `parse_datetime_hr` |
| `parse_datetime_hr` | param_ui.py | `17.05.2026`/`sutra`/`u petak`/`u 2 popodne`/ISO → ISO 8601; `datetime.now(Europe/Zagreb)` |
| `_coerce_llm_params` | [engine.py](../services/v2/engine.py) | LLM-izvučene string-vrijednosti → kroz `parse_param_value` (improve-only) + whole-float→int. LLM put inače preskače coercion |
| `_build_url` bool | [api_gateway.py](../services/api_gateway.py) | query bool → `true`/`false` (ne `True`) |
| formatter "doslovno" | [llm_formatter.py](../services/formatter/llm_formatter.py) | izlaz: prikaži vrijednosti TOČNO, ne zaokružuj |

## B) UNOS NA DOBRO MJESTO (path/query/body/header)

| Mehanizam | Gdje | Što radi |
|---|---|---|
| Derivacija lokacije | [swagger_parser.py:423,502](../services/registry/swagger_parser.py#L423) | OpenAPI `in:` → path/query; `requestBody` properties → body; `header` se ispušta |
| Routing | [executor.py:148-172](../services/v2/executor.py#L148) | dijeli params po `location`; `{placeholder}` supstituira u URL + **refuse** ako ostane neispunjen |
| Query enkodiranje | api_gateway `_build_url` | query→query string (jednom enkodirano); body→`json=`; `Filter` zadrži `=` |
| **Dokazano** | — | 1570 body / 1666 query / 702 path; **0** bez lokacije; **0** placeholder-mismatch na 510 path-toolova (bijekcija) |

## C) UNOS PRAVILNO (prava vrijednost, nikad tiho-krivo)

Doktrina **"resolve-or-ask, never fabricate"** — svaki poziv je valjan ILI pošteno odgođen:

| Mehanizam | Gdje | Što radi |
|---|---|---|
| context-inject | [executor.py:121](../services/v2/executor.py#L121) | identity vrijednosti (tenant/person/vehicle/company/orgunit) → context params, auto |
| LLM-extract | [llm_router.py](../services/router/llm_router.py) | izvuče SAMO izrečene vrijednosti iz poruke |
| anti-fabrikacija | [tool_schema_builder.py:183](../services/router/tool_schema_builder.py#L183) | schema emitira `required: []` → LLM ne izmišlja obavezne |
| completeness guard | executor | required koji se ne može popuniti → `missing_required` refuse (ne prazan send) |
| coercion | (A) | tip/format normalizacija prije slanja |
| fail-safe | api_error_translator | ako ipak ode krivo → API 422 → hrvatski prijevod (nikad tiho) |

## D) TRAŽENJE PARAMETARA (kad bot pita korisnika)

| Korak | Gdje | Što radi |
|---|---|---|
| izračun što fali | `_compute_missing_required` [engine.py](../services/v2/engine.py) | required user_input koji nije prikupljen |
| start / refuse | `_maybe_start_param_collection` | required **array/object** → pošten refuse ("ne mogu strukturiran unos"); inače spremi `PendingParams` + pitaj prvi |
| pitanje | `param_ui.render_param_question` | HR pitanje; label preko `ParamLabeler` (LLM + cache) |
| obrada odgovora | `_resolve_pending_params` | `parse_param_value`; fail → `render_param_reask` (s primjerom); idući required; pa optional; pa finalize |
| opcijski | `_user_friendly_optionals` + `optional_extractor` | ponudi opcijske (skip array/object + paginacija First/Rows/Sort); free-text → dict |
| potvrda | `mutation_gate` | POST/PUT/PATCH/DELETE → Da/Ne prije izvršenja |
| prekid | `is_cancel`/`is_negative` | `odustani` / `ne` → prekini ili preskoči opcijske |

---

## Test: radi li za SVIH 950 toolova — `scripts/audit_all_tools.py`

Iterira `config/processed_tool_registry.json` (runtime registry) i za SVAKI tool provjeri invariante svih 4 područja. **Rezultat (2026-05-27): 950/950 ✓ na svakom, 0 problema.** Stvarno izvršeno (ne vakuozno): **3446** schema-propertyja izgrađeno, **3027** scalar + **111** datum params coerce-ano čisto, sve lokacije valjane.

```
LOCATION  : ✓ ALL OK     (lokacije valjane + placeholder bijekcija)
SCHEMA    : ✓ ALL OK     (schema gradi, required=[], Filter skriven)
COERCION  : ✓ ALL OK     (svaki tip poznat; sample coercion bez greške)
PARAM-ASK : ✓ ALL OK     (missing/optional/refuse/question rade)
```

### Honest caveat (KLJUČNO)
Ovo je **STATIČKI** test: dokazuje da bot za svaki tool MOŽE složiti schema / rutirati na pravo mjesto / coerce-ati / pitati po njegovoj spec. **NE dokazuje da live API PRIHVATI poziv** — to traži prave vrijednosti + M1 token + da routing uopće izabere taj tool. "Mašinerija radi za svih 950" ✓ dokazano; "svih 950 live prolazi" ✗ (smoke test, M1-gated). Routing (~20% long-tail / `no_tool_call`) je odvojen, veći strop.

Pokreni: `python scripts/audit_all_tools.py`
