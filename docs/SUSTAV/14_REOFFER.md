# 14 — REOFFER ("nije točno" feedback petlja)

**Svrha**: Kad korisnik nakon izvršenog alata pošalje "nije točno" (ili sličnu frazu), bot ponudi sljedeća 3 kandidata iz keširanog cosine top-50 (ili re-rutira L2b shortcut kroz L3) umjesto svježeg action pickera. Izgrađeno 2026-06-05.

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/v2/engine.py` | 2733 | LIVE | `_handle_reoffer`, `_REOFFER_PHRASES`/`_L2B_SENTINEL`, "nije točno" handler u dispatchu, 3 mjesta spremanja reoffer-stanja |
| `services/v2/pending_clarify.py` | 166 | LIVE | 5 reoffer polja (4 + `reoffer_origin_tool` measure-first 2026-06-10) + prošireni `save()` potpis |

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| "Nije točno" handler blok | engine.py:328-344 | Nakon normalizacije (lower+strip+rstrip '.!?,;'), ako je upit u `_REOFFER_PHRASES` i store postoji i `can_reoffer=True` → log telemetrija + `_handle_reoffer`. **Smješten NAKON pending_mutation, PRIJE pending_clarify** |
| `_handle_reoffer` | engine.py:1142-1226 | Glavna logika: L2B re-route vs L3 next-3 vs exhausted |

## Ključne komponente

| Simbol | Lokacija | Što radi |
|---|---|---|
| `_REOFFER_PHRASES` | engine.py:1136-1139 | frozenset 10 fraza: "nije točno", "nije tocno", "nije to", "nije ovo", "krivo", "ne to", "ne ovo", "nije pravi", "pogrešno", "pogresno". Exact-match (izbjegava "nije ovo dosta novca") |
| `_L2B_SENTINEL` | engine.py:1140 | `'__L2B_DRIVER_BASICS__'` — last_executed_tool kad je korisnik dobio L2b odgovor |
| L2B re-route grana | engine.py:1158-1176 | Ako last_executed_tool==sentinel: action picker (ACTION_GLOBAL), header "U redu — evo opcija da preciziraš što tražiš:". Sljedeći picker = puni L3 |
| L3 next-3 grana | engine.py:1177-1224 | remaining = all_candidate_ids − shown; next_three=[:3]; build picker; spremi shown += next_three (can_reoffer=False, last_executed_tool=None); header "U redu, evo drugih opcija:" |
| Exhausted grana | engine.py:1182-1187 | Nema preostalih → clear + "Nemam više relevantnih opcija… ili pošalji 'pomoć'" |
| Save: L2b path | engine.py:577-593 | Nakon driver_basics match: candidates=[], shown=['__L2B_DRIVER_BASICS__'], last_executed=sentinel, can_reoffer=True (try/except) |
| Save: STAGE_TOOL clarify | engine.py:1520-1533, 1595-1597, 1637-1644 | (a) kešira all_candidate_ids=_anchor_ids[:50] + shown=3 kartice, can_reoffer=False; (b) prije clear sačuva _reoffer_top50/_reoffer_shown; (c) proslijedi u _run_gate_and_execute |
| Save: _run_gate_and_execute (GET) | engine.py:2130-2185 | Prošireni potpis (reoffer_top50/shown). Tek nakon uspješnog execute + ako proslijeđeni: shown += tool_id, can_reoffer=True. **Za mutacije se NAMJERNO ne sprema** (komentar :2142-2144) |
| PendingClarify reoffer polja | pending_clarify.py:68-72 | all_candidate_ids, shown_tool_ids, last_executed_tool, can_reoffer, **reoffer_origin_tool** (2026-06-10: krivi tool koji se nosi do correction-pick-a za golden-set labelu) — backward-compat setdefault |
| save() prošireni potpis | pending_clarify.py | +all_candidate_ids/shown_tool_ids/last_executed_tool/can_reoffer/reoffer_origin_tool; SETEX TTL 300s |

## Progresija (pozicije 4-6 → 7-9 → iscrpljeno)

1. STAGE_TOOL clarify prikaže kandidate 1-3 (shown = te 3).
2. Nakon execute, izvršeni tool se doda u shown.
3. Prvi "nije točno": isključi shown → remaining[:3] = poz. 4-6 → doda u shown.
4. Drugi "nije točno": isključi 1-6 → ponudi 7-9.
5. Iscrpljeno: "Nemam više relevantnih opcija".

> Reoffer lanac zahtijeva **pick+execute između svake "nije točno"** poruke: nakon L3 next-3 ponude `can_reoffer=False`, ponovo `True` tek kad korisnik odabere karticu i izvrši alat.

## Redis ključ

- `v2_pending_clarify:{phone}` (pending_clarify.py:27), SETEX TTL 300s.

## Što NE radi

- Ne okida na fuzzy/substring — fraza mora TOČNO odgovarati jednoj od 10 (nakon normalizacije).
- **Ne sprema reoffer-stanje nakon mutacije** (POST/PUT/DELETE confirm gate) — samo nakon uspješnog GET auto-execute ili L2b matcha.
- Ne re-rutira L3 svježe — koristi VEĆ keširan cosine top-50; jedino L2B sentinel pokreće novi L3.
- Ne radi ako pending_clarify_store nije ožičen ili je Redis stanje isteklo (TTL 300s) → svjež action picker.
- Ne pamti više od originalnog top-50.

## Caveati

- score_pairs u L3 grani koriste dummy 0.0 (engine.py:1190) — kartice se ne re-skoraju, zadrži cosine redoslijed.
- Sve save-ove štiti try/except (nikad ne prekida odgovor).
- Testovi: `tests/v2/test_reoffer.py` (8 unit, uklj. 3 correction-label) + 3 end-to-end u `tests/v2/test_engine_wireup.py`.
