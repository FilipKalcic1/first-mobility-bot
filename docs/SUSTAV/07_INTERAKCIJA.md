# 07 — INTERAKCIJA (clarify + param collection + mutation gate)

**Svrha**: Kako bot pita korisnika za razrješenje (action picker + top-3 tool picker), prikuplja parametre koji nedostaju, rješava `*TypeId` FK parametre, i štiti mutacije Da/Ne potvrdom. Svih 9 datoteka je LIVE (importane u engine.py).

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/v2/clarify_ui.py` | 347 | LIVE | Action picker + top-3 tool kartice + parsing odgovora |
| `services/v2/pending_clarify.py` | 166 | LIVE | Redis state top-3 kartica + reoffer polja |
| `services/v2/param_ui.py` | 394 | LIVE | HR pitanja za parametre + tipska koercija + HR datum parser |
| `services/v2/param_labeler.py` | 200 | LIVE | LLM HR labele s 3-tier cacheom (preloaded JSON → Redis 24h → LLM) |
| `services/v2/pending_params.py` | 103 | LIVE | Redis state prikupljanja parametara |
| `services/v2/optional_extractor.py` | 174 | LIVE | LLM ekstrakta opcionalnih parametara iz slobodnog teksta (silent fallback) |
| `services/v2/type_resolver.py` | 108 | LIVE | `*TypeId` FK → id (fetch /…Types + match riječi) |
| `services/v2/mutation_gate.py` | 68 | LIVE | Odluka AUTO (GET) vs CONFIRM (mutacije) |
| `services/v2/pending_mutation.py` | 214 | LIVE | Redis state confirm dijaloga + anti-replay lock + parse_reply |

## 1) Clarify UI (clarify_ui.py)

| Simbol | Lokacija | Što radi |
|---|---|---|
| `ACTION_LABELS` | clarify_ui.py:53-59 | GET→"POGLEDATI", POST→"UNIJETI / KREIRATI", PUT/PATCH→"IZMIJENITI", DELETE→"IZBRISATI" |
| `build_action_picker_global` | clarify_ui.py:65-89 | Universal picker (Model A), uvijek 4 opcije |
| `methods_for_action_label` | clarify_ui.py:143-146 | reverse: "IZMIJENITI"→{PUT,PATCH} |
| `build_from_router_candidates` | clarify_ui.py:172-197 | Top-3 kartice iz router kandidata + TKB lookup |
| `render_text` | clarify_ui.py:244-260 | "1️⃣/2️⃣/3️⃣ + ❌ Nešto drugo" |
| `parse_clarify_reply` | clarify_ui.py:302-347 | "1"/"2️⃣"/"ne" → tool_id ili None |
| `ClarifyCard`/`ClarifyOptions` | clarify_ui.py:28-41 | frozen dataclasses |

## 2) Pending clarify state (pending_clarify.py)

`PendingClarify` dataclass (pending_clarify.py:42+): `phone, candidates, original_query, stage` + **reoffer polja**: `all_candidate_ids` (cosine top-50), `shown_tool_ids`, `last_executed_tool`, `can_reoffer` (2026-06-05) + `reoffer_origin_tool` (2026-06-10: krivi tool nošen do correction-picka za golden-set labelu). Stage konstante: `STAGE_ACTION` (legacy), `STAGE_TOOL` (default), `STAGE_ACTION_GLOBAL` (Model A).

- Redis ključ: `v2_pending_clarify:{phone}` (pending_clarify.py:27), **TTL 300s**.
- `save()` (pending_clarify.py:99-134) — prošireni potpis sa svim reoffer poljima.

## 3) Param collection

- **param_ui** (param_ui.py:161 `parse_param_value`): tipska koercija — integer (strip non-digit, ambiguous zarez → re-ask), number (HR: "1.500,75"→1500.75, "12,5"→12.5, lone "1.500"→re-ask), boolean (strict whitelist da/ne, ostalo→None), date (`parse_datetime_hr` :296 → ISO 8601, weekday HR, dijelovi dana ujutro→9/popodne→13/navečer→18, Europe/Zagreb). `render_param_question` (:64), `render_param_reask` (:122 s primjerom), `render_optional_offer` (:87).
- **param_labeler** (param_labeler.py:70 `label_for`): 3-tier — preloaded `config/param_labels_hr.json` → Redis `param_label:{tool_id}::{param}` (TTL 24h) → LLM. Vraća None na bilo koju grešku (engine fallback na `humanize_param_name`).
- **pending_params** (pending_params.py:42): `PendingParams(phone, tool_id, collected, required_remaining, optional_remaining, optional_offered, original_query, type_options)`. Redis `v2_pending_params:{phone}`, **TTL 300s**. `type_options` = `{param: [[id,name],...]}` (cache /…Types).
- **optional_extractor** (optional_extractor.py:92 `extract`): LLM s `tool_choice="auto"`, fill samo spomenuto. Vraća `{}` na bilo koju grešku (nikad ne baca).
- **type_resolver** (type_resolver.py): `build_typeid_map` (:29) gradi `{param_lower: get_tool_id}` (prioritet `/Lookup/{Param}` pa `/{Base}s`). `match` (:89) normaliziran exact pa substring → `(id, pairs)`; bez unique → `(None, pairs)` kao pick-list. `_norm` (:21) lowercase + skida HR dijakritike (NFKD).

## 4) Mutation gate (mutation_gate.py)

`decide_mutation` (mutation_gate.py:41) — **pure function, samo iz HTTP metode**:
- GET → `DECISION_AUTO`
- DELETE → `DECISION_CONFIRM` "⚠️ TRAJNO BRISANJE: sigurno želiš obrisati {entity}? … Odgovori DA/NE"
- POST/PUT/PATCH → `DECISION_CONFIRM` "Potvrđuješ {entity}? Da/Ne"

`MutationDecision` frozen dataclass (decision, confirm_message, log_reason). **Nema** per-tool data/range/critical-fields — Filipova "maksimalna uniformnost" direktiva; vrijednosna validacija je engine echo + backend 4xx → ApiErrorTranslator.

## 5) Pending mutation + anti-replay (pending_mutation.py)

- `PendingMutation(tool_id, params, stage, created_at)`, Redis `v2:pending_mut:{phone}` (pending_mutation.py:36), **TTL 300s**.
- **Anti-replay lock** (pending_mutation.py:118-153): `try_acquire_execution` atomic SET NX EX (`v2:pending_mut_exec:{phone}`, 30s); **fail-open** na Redis outage. `release_execution` DELETE.
- `parse_reply` (pending_mutation.py:177): vraća "execute"/"cancel"/"ambiguous". `_AFFIRMATIVE` strict exact ({da, yes, y, ok, okej, može, moze, potvrdjujem, potvrđujem} — 9 tokena); `_NEGATIVE` lenient ({ne, no, n, odustani, prekini, stani, cancel, otkaži, otkazi} — 9 tokena, + word-split "ne hvala"). "može biti" → ambiguous (NIJE "može"). Zahtijeva `stage==STAGE_SINGLE`.

## Što NE radi

- mutation_gate ne provjerava vrijednosti/permisije — samo metoda.
- type_resolver/optional_extractor LLM vraćaju None/{} na grešku (nikad ne ruše).
- param_ui parseri vraćaju None na ambiguous → re-ask (NE tiho defaultanje).

## Caveati

- **Asimetrija `parse_reply`** (MED-2, namjerno): EXECUTE traži exact match, CANCEL dozvoljava word-split — false-execute (nenamjeran write) gore od false-cancel (re-ask).
- `param_ui` `_WEEKDAYS_HR` + `_PART_OF_DAY_HR` ručno održavani paralelno s `flow_engine.py` — `_WEEKDAYS_HR` je ista u obje kopije, ali `_PART_OF_DAY_HR` se razlikuje: param_ui sprema jedan sat (popodne→13), flow_engine sprema raspone.
- `param_labeler._preloaded` prazan dict ako JSON missing (bez crasha).
- `try_acquire_execution` fail-open: radije izgubi replay-zaštitu nego blokira sve mutacije.
