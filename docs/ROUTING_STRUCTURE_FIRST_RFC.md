# RFC: Structure-first routing (entitet + akcija) — 2026-05-27

> **Status:** RFC / hipoteza. NIJE implementirano. Gradi se tek ako shadow+eval mjerenje pokaže dobitak vs sadašnjih ~47% recall@3. (Filip + analiza, potaknuto usporedbom s MobilityOne Query Builderom.)

## Problem
Bot-ovo usko grlo je **routing: NL → 1 od 950 toolova** (~47% recall@3, p@1 32-37%). Sve ostalo (param-unos, gate, echo, izvršenje) je solidno. Routing je jedna ogromna klasifikacija koju mali model ne rješava pouzdano.

## Uvid (iz QB usporedbe)
Query Builder nema taj problem jer korisnik **u UI-ju izabere entitet**, pa AI samo popuni upit. Meta je zadana, ne pogađana. Možemo posuditi taj princip: **routing po STRUKTURI (entitet + akcija) umjesto po flat semantičkom matchu.**

**Naši FLOWS su živi dokaz:** `_guess_flow_name` već radi hardkodiran structure-first — "rezerviraj"→(Booking,write)→post_VehicleCalendar, "upiši km"→(Mileage,write)→post_AddMileage, "kvar"→(Case,write)→post_AddCase. Deterministički, ~100% za te 3. **Structure-first = generaliziraj taj obrazac na svih 950** (izvučen entitet+akcija umjesto 3 hardkodirana keyworda).

## Jezgra ideje
```
SADA:        950 → scope → [METODA] → cosine top-50 → LLM bira 1 od 50      (teško)
PRIJEDLOG:   950 → scope → [METODA] + [ENTITET] → ~3-8 → LLM bira 1 od ~5    (lako)
```
**Dvije lake klasifikacije > jedna nemoguća:** NL → 1-od-~50 entiteta × 1-od-4 akcije → (entitet×akcija) determinističk i suzi na sićušan set. Nije rewrite — **dodatni FILTER (entitet) uz postojeći action-filter.**

## Glavni entitet vs filter-entitet
Upit spomene više entiteta; jedan je glavni (bira tool), ostali su filteri.
- **Pravilo (80%):** glavni = **direktni objekt glagola akcije** (head-noun); filteri = iza prijedloga (za/od/u) ili specifične vrijednosti (DA053F, id).
- "pokaži **troškove** za vozilo DA053F" → glavni=Expense (→get_Expenses); filteri=Vehicle=DA053F, ExpenseType=Gorivo.
- Granice: slobodan hrv. red riječi, implicitan entitet ("koliko sam platio?"), ponekad filter-meta JEST glavni.

## Kako ekstrahirati
- **Preporuka: constrained LLM** — entitet ∈ enum(~50) + akcija ∈ {read,create,update,delete} + filters[] → JSON. Enum + validacija spr. halucinaciju. Rješava i "glavni vs filter".
- **Merge u postojeći `intent_type`** (L2a već klasificira 4-way) → vrati `{kind, entitet, akcija}` → **0 dodatnih LLM poziva.**
- **Keyword fast-path:** `anchor_vocab.py` (entity sinonimi iz ranije) kao izvor enuma + brza determinist. provjera.

## Filter / type_resolver se uklapaju (ista ekstrakcija)
Strukturirana ekstrakcija **istovremeno** daje routing I filter:
- glavni entitet → ROUTING (tool)
- `*TypeId` filteri ("gorivo"→3) → **type_resolver** (imamo, 7/10)
- generički filteri (LicencePlate=, Date-rasponi) → **filter-builder** (M1-gated: treba filter-schemu)

→ Ovo je isti `filter_extractor` koji je filter-redizajn ionako tražio. **Routing je M1-neovisan (može sad); bogati filter čeka M1.**

## Latencija / trošak / turnusi
- Naivno (novi LLM poziv) = +round-trip → loše. **Merge u intent_type = ~besplatno.**
- Manji router prompt (~5 schema vs 50) = jeftiniji/brži finalni pick.
- **Bonus (najveći UX):** ako pouzdano izvučemo akciju → **preskačemo action-picker turn** (i tool-picker) → "upiši 50000 km" → odmah "Vozilo DA053F, 50000 km. Da/Ne?". Manje turnusa = veći dobitak nego ušteda poziva.

## Sigurnost: fallback (nikad gore od danas)
- **Mora biti dodatak, ne tvrda brana.** Nesiguran/nema entiteta/prazna ćelija → **padni na trenutni anchor top-50** (status quo ~47%). Može samo pomoći ili biti neutralno.
- Opasnost = "siguran ali krivi entitet" (suzi na krivu ćeliju → promašaj). Zato:
  - **Soft re-rank (sigurni start):** entitet BOOST-a unutar anchor top-50, nikad ne izbacuje → nula rizika, skroman dobitak.
  - **Hard-filter** (50→5) tek za visoko-sigurne ćelije, nakon dokaza.

## Migracija bez rušenja (projektov postojeći obrazac)
1. Aditivno + flag (OFF/shadow default; stari put glavni).
2. **Shadow mode** — radi paralelno, LOGIRA što BI izabrao vs stari router, NE djeluje (0 rizika).
3. **Eval A/B** — oba na eval setu → recall@3/p@1 → odluka po broju (CLAUDE.md gate).
4. Postupno: soft re-rank → mjeri živo → hard-filter za high-confidence.
5. **Nikad ne briši anchor** — fallback.

## Mjerni protokol (gate prije gradnje)
- Implementiraj ekstrakciju + entity-tagging toolova (entitet iz path-a, ručna validacija složenih path-eva).
- Shadow na `tests/benchmarks/expanded_benchmark_v2.json`: usporedi structure-first kandidatni set vs anchor top-50 — sadrži li pravi tool, na kojem mjestu.
- **Odluka:** ide dalje samo ako recall@3 ↑ vs ~47% (i p@1) bez pada na drugim upitima.

## Iskreni caveati
- Eval set je **sintetički** (driver-light) → broj je proxy dok ne dođe pravi promet (smoke/telemetrija).
- Entity-tagging 950 toolova treba validaciju (`/Vehicles/{id}/documents` → Vehicle ili Document?).
- Merge u intent_type mijenja klasifikator koji RADI → A/B da 4-way ne regresira.
- **NEDOKAZANO** da diže 47% — dobra hipoteza (napada točno usko grlo), ali bez mjerenja je nagađanje.

## Reuse iz postojećeg koda
- [intent_type.py](../services/v2/intent_type.py) — L2a klasifikator (proširi na {entitet, akcija})
- [anchor_vocab.py](../services/router/anchor_vocab.py) — entity sinonimi
- [type_resolver.py](../services/v2/type_resolver.py) — lookup ime→id
- [catalog_scoper.py](../services/router/catalog_scoper.py) — gdje bi entity-filter sjeo uz method-filter
- [flow_engine.py](../services/v2/flow_engine.py) — postojeći hardkodiran structure-first dokaz
