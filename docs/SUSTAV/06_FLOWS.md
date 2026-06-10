# 06 — FLOWS (booking / mileage / case automati)

**Svrha**: Višekoračni stateful workflowi koji korak-po-korak prikupljaju parametre, pa nakon eksplicitne Da/Ne potvrde izvršavaju TOČNO JEDAN mutacijski API poziv. Determinističke prečice koje zaobilaze LLM routing.

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/v2/flow_engine.py` | 852 | LIVE | Jezgra: state machine, validatori, period parser, 3 flowa, Redis serijalizacija |
| `services/v2/engine.py` (L4 dio) | 2733 | LIVE | Orkestracija: pokreće/nastavlja flow, EXEC_LOOKUP round-trip, anti-replay lock, pre-fill iz inicijalne poruke |

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| `FlowEngine.start` | flow_engine.py:170 | Čista funkcija: prima identity_context, kreira početni FlowState, vraća FlowOutcome s prvim pitanjem (ili CANCELLED ako flow ne postoji). **Poziva ju** `V2Engine._start_flow` (engine.py:865) |
| `FlowEngine.handle` | flow_engine.py:195 | Nastavlja flow s korisničkim unosom ili lookup_result → FlowOutcome. **Pozivaju ju** `V2Engine._continue_flow` / `_drive_flow_lookups` |
| `FlowStateStore.load/save/clear` | flow_engine.py:818/840/848 | Redis persistence (`v2:flow:{phone}`, TTL 600s) |

## Ključne komponente

| Simbol | Lokacija | Što radi |
|---|---|---|
| `FLOWS` dict | flow_engine.py:795 | Registry: 'booking', 'mileage', 'case' |
| `Flow` dataclass | flow_engine.py:107 | name, steps, final_tool_id, final_params_builder |
| `Step` dataclass | flow_engine.py:87 | kind, slot_name, prompt, choices_slot, lookup_tool_id/lookup_params_template |
| `FlowState` dataclass | flow_engine.py:116 | flow_name, step_index, collected_params, started_at; to/from_json |
| `FlowOutcome` dataclass | flow_engine.py:142 | kind, response, tool_id+params, new_state |
| STEP_* konstante | flow_engine.py:65-70 | ASK_TEXT(65), ASK_NUMBER(66), ASK_PERIOD(67), ASK_CHOICE(68), ASK_CONFIRM(69), EXEC_LOOKUP(70) |
| OUTCOME_* konstante | flow_engine.py:74-78 | PROMPT, EXECUTE, CANCELLED, INVALID, DONE |
| `_advance_or_prompt` | flow_engine.py:267 | State machine kernel: EXEC_LOOKUP → vrati tool_id; ASK_* → gradi prompt; auto-skip ako slot već popunjen (HIGH-1 fix:276) |
| `_validate` | flow_engine.py:385 | Po step.kind: TEXT (≥2 znaka), NUMBER (strip km, 0-9999999), PERIOD (_parse_period), CHOICE (1-based pick) |
| `_parse_period` | flow_engine.py:564 | HR periodi: "sutra 9-15", "16.12.2025 9:00-17:00", "u petak ujutro", "od 9 do 15" → {period_text, from_time, to_time} (ISO) ili None |
| `_handle_lookup` | flow_engine.py:331 | Pohrani EXEC_LOOKUP rezultat u collected, napreduj |
| `_handle_confirm` | flow_engine.py:348 | ASK_CONFIRM: parse_reply (STAGE_SINGLE) → EXECUTE za "da", CANCELLED za "ne" |

## 3 flowa (verificirano)

| Flow | Koraci | Tool | Builder |
|---|---|---|---|
| **BOOKING** (flow_engine.py:706) | ASK_PERIOD → EXEC_LOOKUP `get_AvailableVehicles` → ASK_CHOICE → ASK_CONFIRM | `post_VehicleCalendar` | `_booking_params` (:670): AssignedToId, AssigneeType=1, EntryType=0, FromTime, ToTime, VehicleId |
| **MILEAGE** (flow_engine.py:750) | ASK_NUMBER → ASK_CONFIRM | `post_AddMileage` | `_mileage_params` (:684): VehicleId, Value, Comment |
| **CASE** (flow_engine.py:772) | ASK_TEXT → ASK_CONFIRM | `post_AddCase` | `_case_params` (:692): User, Subject, Message |

## Period parsing helperi (flow_engine.py:432-604)

`_PERIOD_HOUR_RE` (9-15, 9:30-17:00), `_PERIOD_HOUR_OD_DO_RE` ("od 9 do 15", DIO 2 fix), `_PERIOD_DATE_RE` (16.12.2025), `_WEEKDAYS_HR` (ponedjeljak→0 ISO), `_PART_OF_DAY` (ujutro→9-12), `_resolve_date/_resolve_hours/_resolve_weekday`. Sve u Europe/Zagreb (`zoneinfo.ZoneInfo`).

## Redis ključ

- `v2:flow:{phone}` — FlowState JSON, SETEX, **TTL 600s (10 min)**.

## Što NE radi

- Nije HTTP endpoint (čista logika, V2Engine orkestrira).
- Nije DB ORM (Redis je samo serijalizacija).
- Nije LLM intent klasifikacija (`KIND_FLOW_REQUEST` je detekcija, FlowEngine je dispatcher).
- Pokriva SAMO write operacije s confirm gate-om.
- Ne retry-a transient lookup failure (V2Engine logira + vrati error poruku).

## Caveati

- `AssigneeType=1` / `EntryType=0` su **hardkodirani** za booking (flow_engine.py:675-676) — značenje nije dokumentirano u kodu (pretpostavka: tip osobe/rezervacije; ovo je upravo što MAIL ASK 2 traži M1 da deklarira).
- `FLOW_TTL_SECONDS=600` hardkodiran; napušteni flow auto-expire nakon 10 min.
- BOOKING uvijek očekuje ASK_PERIOD → EXEC_LOOKUP → ASK_CHOICE. Ako lookup vrati [] (nema vozila) → "Nema dostupnih opcija" + clear.
- **Dva odvojena cancel-mehanizma**: (1) na ASK_CONFIRM koraku shared `parse_reply` (STAGE_SINGLE) — "ne"/"ne hvala" → CANCELLED (lenient), "može biti" → INVALID (re-ask), samo strict "da" → EXECUTE; (2) mid-flow na ASK_* koracima `_is_cancel` (flow_engine.py:609) prekida flow na riječi iz skupa `("odustani", "prekini", "cancel", "stop")` — **"ne" NIJE u tom skupu** (mid-flow "ne" se tretira kao odgovor, ne kao cancel). NAPOMENA: `handle()` docstring (flow_engine.py:208-210) je zastario — navodi "ne" kao cancel okidač iako ga stvarni `_is_cancel` ne sadrži ("stop" je točan).
- Mileage number parser (flow_engine.py:397) stripa sve non-digits ("145000 km" → 145000); negativni predznak se gubi (edge case).
