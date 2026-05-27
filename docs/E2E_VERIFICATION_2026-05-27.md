# Deep E2E verifikacija: radi li STVARNO + je li POVEZANO (2026-05-27)

Filip: "testovi nisu dovoljan dokaz — moramo znati da je SVE povezano i ima smisla prije M1 maila." Ovaj doc dokazuje **POVEZANOST** lanca (params / *TypeId / datumi / lokacija / filter) kroz **PRAVI engine**, ne izolirane unit testove.

## Ljestvica dokaza
| Razina | Dokazuje | Status |
|---|---|---|
| L1 unit (1639) | dijelovi rade izolirano | ✅ |
| L2 statički-950 (`audit_all_tools.py`) | mašinerija barata svaki tool po spec | ✅ |
| **L3 E2E kroz pravi engine** | **dijelovi POVEZANI — pravi podatak stigne na wire** | ✅ **ovaj doc** |
| L4 live (pravi MobilityOne) | API stvarno PRIHVAĆA poziv | ⛔ treba M1 token (zaključan) |

## L3 — 4 E2E dokaza (pogonjeno `process_message()` kroz PRAVI V2Engine + FakeGateway koji hvata wire)
Svaki test asertira **stvarni `gateway.call(...)` argument** (body/path) — dakle podatak koji bi otišao na API.

| Scenarij | Lanac | ASSERT na wire-u | Test |
|---|---|---|---|
| *TypeId iz riječi | "dodaj trošak za **gorivo** 50€" → router(post_Expenses,{Amount:50}) → resolver fetch `/Lookup/ExpenseTypeId` → match "gorivo"→3 → mutation-confirm → execute | POST `/Expenses` **body = {Amount:50, ExpenseTypeId:3}** | `test_e2e_typeid_word_in_query_reaches_executor_body` |
| *TypeId ask-with-list | "dodaj trošak" (bez tipa) → ask "Dostupno: Gorivo, Servis" → odgovor "gorivo"→3 → execute | POST body **ExpenseTypeId:3** | `test_e2e_typeid_ask_with_list_then_answer_reaches_body` |
| Datum coercion | router(post_VehicleCalendar,{FromTime:"17.05.2026 09:00"}) → `_coerce_llm_params` → ISO → execute | POST body **FromTime="2026-05-17T09:00:00"** | `test_e2e_date_string_reaches_body_as_iso` |
| Param-ask + path | DELETE `/VehicleCalendar/{id}`, router bez id → ask → "45" → execute | DELETE path **`/VehicleCalendar/45`** (nema `{id}`) | `test_e2e_param_ask_path_id_substituted_in_url` |

**Zašto je ovo jače od "unit prolazi":** pogoni cijeli ožičeni put (identity → intent → clarify → param-collection → resolver → coercion → mutation-gate → executor → gateway) s realnim odgovorima i provjerava **što stvarno izađe na wire**. Ovakav test bi uhvatio prošlogodišnji `spec_for/method_of` showstopper (routing radi, execute puca) koji su unit testovi promašili.

## Koherencija (read + grep)
- **Filter = nula (user-filtering) potvrđeno:** grep `build_filter|combine_filters|"Filter":=` u `services/` → nema žive `.py` gradnje (samo stale `__pycache__/*.pyc` od obrisanog `filter_builder.py` — kozmetika). Jedina dva ŽIVA `Filter` korištenja: (a) [api_gateway.py:470](../services/api_gateway.py#L470) — encoding-guard koji čuva `=` AKO Filter postoji (inertan jer ga nitko ne kreira na user-putu), (b) [identity.py:465](../services/v2/identity.py#L465) — **interni** `Filter=Phone(contains)NSN` za identifikaciju korisnika (nužno, NIJE user-filtriranje). LLM ne može ubaciti Filter (sakriven iz scheme — `test_build_suppresses_filter_params`).
- **Resolver-id → wire:** dokazano E2E (ExpenseTypeId=3 u body-ju), ne samo da resolver vrati 3.
- **Coercion → wire:** dokazano E2E (ISO datum u body-ju).
- **Lokacija → wire:** dokazano E2E (`{id}`→45 supstituiran; body params u body).

## Iskreno — što L3 NE dokazuje (treba L4)
- Da **pravi MobilityOne API prihvaća** te pozive (status 200, ne 400/422). FakeGateway vraća što mu kažem — ne pravi backend.
- Da je tip-vrijednost u `/Lookup` ona koju mislimo (3=Gorivo) — to je pravi podatak iz backenda.
- **L4 je gated na M1 token** (sad "Auth server unreachable"). Kad token proradi: 2-3 read-probe (`GET /Lookup/ExpenseTypeId`, 1 GET liste, 1 kontrolirani write+revert) → potvrda da API prihvaća.

## Reprodukcija
```
python -m pytest tests/v2/test_engine_wireup.py -k e2e   # 4 E2E wire-proofa
python scripts/audit_all_tools.py                         # L2 statički 950
```

## Sitno (kozmetika, ne blokira)
Stale `services/**/__pycache__/filter_builder*.pyc` (od obrisanog modula) — može se obrisati; bezopasno (ništa ga ne importa).
