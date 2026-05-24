# Pošten extraction review — 2026-05-24

**Pitanje (Filip):** bot mora iz JEDNE poruke izvući SVE — param na pravo mjesto, prava vrijednost, pravi format, filter — gotovo 100% točno, inače poziv nije adekvatan. Koliko je točno?

**Opseg:** mjeri se **one-shot ekstrakcija** (što bot izvuče iz sirove poruke). Param-ask (višeturni fill required-koji-fale) + context-injection (executor: VehicleId/personId) su determinist. follow-up, NE mjere se ovdje. Bez promjena koda — samo mjerni alat (`scripts/bench_extraction.py`) + eval (`tests/benchmarks/extraction_eval.json`, 22 ručno pisana upita s vrijednostima EKSPLICITNO u poruci).

---

## Rezultat: na čistoj površini — ~100% točno

| Dio | Što | Rezultat |
|---|---|---|
| **A — LLM tool-call** (real router + Azure) | path `{id}` + body numerik | **12/12 tool-correct, 12/12 potpun poziv (100% param)** |
| **B — registracija** (deterministički, entity_detector) | tablica → Filter | **7/7 pozitiva + 7/7 ispravan Filter**; **3/3 negativa** (nema lažne tablice) |

Konkretno (sve točno):
- `obriši rezervaciju 45` → `{id: 45}` ✓
- `dodaj 30500 km` → `{Value: 30500}` ✓ (ispravno mapirao "km" na generički param `Value`)
- `evidentiraj 42.500 km` → `{Value: 42500}` ✓ (HR tisućica separator riješen)
- `km za škodu DA053F` → tablica `DA053F` → `LicencePlate(=)DA053F` ✓
- `dodaj 30500 km` / `za 30500 km` → **NE** detektira lažnu tablicu ✓

**Zaključak dijela:** za jasno izrečene jednostavne vrijednosti (id-evi, brojevi, HR-formatirani brojevi, registracije) ekstrakcija je pouzdana. Bot NE krivo čita / NE stavlja na krivo mjesto / NE kvari format izrečene vrijednosti. Tvoj glavni strah (kriva vrijednost / krivo mjesto / krivi format / zaboravljen filter) — na ovoj površini NIJE problem.

---

## ALI — pošteni caveati (gdje "adekvatan poziv" i dalje može pasti)
Ovaj 100% je na ČISTOJ, MJERLJIVOJ površini. Ne precjenjujmo:

1. **Uska površina.** Mjereno: path-id, jednostavni numerik, registracija. NIJE mjereno: datumi/vrijeme (flow-driven), **enumi**, context-params (VehicleId — injecta se, ne izvlači), array-Filter s više polja, 159 toolova bez body-scheme (nema se ŠTO izvući).
2. **Routing je gate.** Ovdje 12/12 jer su to česti toolovi s jasnim upitom. Na repu (routing ~20%, prošli izvještaj) ekstrakcija je nebitna jer je tool kriv.
3. **🔴 LLM IZMIŠLJA required parametre koje ne može znati.** Primjer e06: `izmijeni rezervaciju 12` → `{id:12, AssigneeType:1}`. `id` točan, ali **`AssigneeType:1` je NAGAĐANJE** (required enum koji user nije rekao). Pošto je "prisutan", param-ask ga NE pita → kriva vrijednost tiho ide u body → moguć krivi update / 422. **Ovo je pravi rizik za toolove s awkward required poljima** (ne za jednostavne id/broj).
4. **Mali set** (22 upita) — nedvosmislen ground-truth, ali nije iscrpan. Datumi/enumi/multi-filter trebaju vlastiti krug.

---

## Honest presuda
**Mehanika ekstrakcije je solidna** — izrečene jednostavne vrijednosti se izvlače točno (~100% na ovom setu), filter (registracija) radi 100% + uvijek uz potvrdu. **Pravi rizici za "neadekvatan poziv" NISU u čitanju izrečenih vrijednosti**, nego uzvodno/oko ruba:
- **Routing** (~20% na repu) — krivi tool poništava sve.
- **159 toolova bez body-scheme** — bot ne zna koja polja postoje.
- **LLM nagađa required enume/polja** koje user nije rekao (e06) — tiho kriva vrijednost.
- Datumi/enumi/multi-field-filter — nemjereno, vjerojatno slabije.

## Poluge za sljedeći krug (Filip odlučuje — bez namještanja)
1. Proširi eval na **datume/vrijeme + enume** (izmjeri prije fixa).
2. **Guard protiv izmišljanja**: ako LLM popuni required param koji nije u poruci (osobito enum/uuid bez hinta) → radije PITATI nego nagađati.
3. **159 body-scheme** (FAZA 8) — backfill ili označi nedostupnima.
4. Datum-raspon + operatori u Filteru (`>`,`<`,`contains`).

**Promjene koda ovaj krug:** SAMO mjerni alat (`scripts/bench_extraction.py`) + eval JSON. Nijedan routing/param/executor file nije mijenjan. `pytest` ostaje zeleno.

---

## Dodatak (2026-05-24): Faza 1 anti-fabrikacija — implementirano + rezultat
Promjene: `tool_schema_builder` emitira **`required: []`** LLM-u (required-enforcement OSTAJE preko registryja → param-ask); `param_ui` bool: "ne"/"false"→False, ambiguous→None (re-ask, ne tiho-False).

- **A/B (isti 12-q extraction set), tool-selection NEUTRALNO**: **5/12 i SA i BEZ `required`** → moja promjena NE dira routing. Fabrikacija uklonjena (među točno-routanim upitima param-level ostaje 100%, bez izmišljenog `AssigneeType`).
- **🔴 Otkriveno (ne prikrivam): router je NESTABILAN run-to-run.** Isti jasni upiti: prvi run 12/12, dva recentna runa 5/12. By-id upiti ("obriši rezervaciju 45", "izmijeni rezervaciju 12") gpt-4o-mini sve češće **ODBIJA** (`no_tool_call`) ili pickne sibling; POST ("dodaj km") stabilno 5/5. To je **ISTI** routing problem iz `ACCURACY_HONEST` (sibling-ambiguity + decline na ~99-toolnom bucketu), **NE regresija i NE od moje promjene** (A/B dokazao). Prvi 12/12 bio optimističan outlier.
- **Zaključak**: ekstrakcija (KAD tool routa) radi pouzdano; **ROUTING (decline/instabilnost na by-id, sibling-ambiguity) je usko grlo** — potvrđuje accuracy review. 1615 testova zeleno.

Promjene koda: `tool_schema_builder.py` (`required:[]`), `param_ui.py` (bool), + `test_tool_schema_builder.py` update. Nije commitano.
