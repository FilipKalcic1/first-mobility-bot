# Structure-first routing — Phase 1a mjerenje (offline ceiling) — 2026-05-28

> **Cilj:** prije gradnje (RFC), izmjeri diže li (entitet × akcija) filtar routing iznad sadašnjih ~47% recall@3. Phase 1a = deterministička "ceiling" analiza: IF entitet+akcija savršeno poznati, kolika je kandidatska ćelija + sadrži li odgovor. Skripta: `scripts/eval_structure_first.py`. Eval: `expanded_benchmark_v2.json` (138 upita).

## Brojke
- **Grid:** 950 toolova → 396 entiteta × 516 (entitet, akcija) ćelija. 350/516 (68%) ćelija = veličine 1.
- **Ceiling (savršen entitet+akcija) — gdje pada answer (138 upita):**
  - u ćeliji ≤1 tool: **8%**
  - u ćeliji ≤3 toola: **46%**  ← usporedi: anchor recall@3 ~**47%** → **PARITET**
  - u ćeliji ≤5 toolova: **86%**
  - medijan veličine ćelije: **4**

## Ključni nalaz: TAXONOMY DILEMA (zašto ovo NIJE čisti dobitak)
Uzorci derivacije pokazuju strukturni problem:
```
get_Persons_Agg     → entitet "PersonsAgg"      cell=1   ⚠️ junk — korisnik kaže "osobe", ne "PersonsAgg"
get_TripTypes_Agg   → entitet "TripTypesAgg"    cell=1   ⚠️ isto
get_Trips_id        → entitet "Trip"            cell=6
get_ExpenseGroups   → entitet "ExpenseGroup"    cell=6
```
- **68% size-1 ćelija je NAPUHANO** — `_Agg`/compound toolovi dobiju super-specifičan jedinstven "entitet" koji se **NIKAD ne može izvući iz prirodnog jezika** (korisnik kaže "agregirani troškovi", ne "ExpensesAgg").
- **Dilema bez izlaza:**
  - **Fine entitete** (kao sad) → uske ćelije ALI ne matchaju NL → systematski **promaše long-tail** (_Agg/compound = velik dio repa).
  - **Grube entitete** (mapiraj get_Expenses_Agg → "Expense") → matchaju NL ALI ćelije narastu (Expense+read = puno toolova) → opet treba semantička disambiguacija = **isto što anchor već radi**.
- Čak i sa **SAVRŠENOM** ekstrakcijom, na ≤3 smo na **paritetu** s anchorom (46% vs 47%). Bolji ceiling (≤5=86%) ovisi o (a) savršenoj ekstrakciji [nemjereno, bit će <100%] i (b) napuhanoj fine-taksonomiji koja promaši long-tail.

## Verdikt: NE gradimo structure-first (measure-first nas spasio)
- Ceiling **nije čisti dobitak** — paritet na ≤3, a ≤5 prednost je napuhana junk-entitetima + ovisi o savršenoj ekstrakciji koju long-tail mismatch ionako ruši.
- Strukturna dilema (fine-ne-matcha-NL vs grubo-ne-suzava) znači da **structure-first ne rješava upravo teške slučajeve** (long-tail) — iste koje anchor promaši. Ne mijenja sliku za "opći sustav nad 950".
- **Phase 1b (LLM ekstrakcija accuracy) ne bi preokrenuo verdikt:** taxonomy mismatch je problem NEOVISAN o kvaliteti ekstrakcije (savršena ekstrakcija "Expense" svejedno promaši get_Expenses_Agg). Pa ne trošimo na to.

## Što OSTAJE pravi lever za pouzdaniji opći sustav (umjesto structure-first)
1. **Deterministički put za često** — flows (booking/mileage/case ~100%) + driver-basics; tu je pouzdanost već visoka.
2. **M1 data kvaliteta** — distinktni opisi/enumi (gasi `wrong_pick` među lookalike toolovima) = root-cause za long-tail.
3. **Jači model** (gpt-4o / text-embedding-3-large) — mjereni lever na pick među lookalike.
4. **Safe-fail** — kad nesiguran: top-3 picker + potvrda → nikad tiho krivo (≠ magičnih 100%).

## Iskreni caveati o samom mjerenju
- Entity-derivacija je heuristička (op_id → strip verb/_id → singular); junk na _Agg/compound. ALI to ide U KORIST structure-firsta (napuhuje narrowing), pa je verdikt konzervativan.
- Eval je sintetički (driver-light) → proxy, ne pravi promet. Ali za "narrowing ceiling" je dovoljno reprezentativan.
- "Anchor ~47% recall@3" je iz ranijeg `bench_router_e2e` runa (Run B, isti eval).

## Zaključak (1 rečenica)
Structure-first zvuči dobro u teoriji, ali mjerenje pokazuje **paritet na ≤3 + strukturnu taxonomy-dilemu** koja ne rješava long-tail — pa ga **NE gradimo**; pravi leveri ostaju M1-podaci + model + deterministika za često + safe-fail.
