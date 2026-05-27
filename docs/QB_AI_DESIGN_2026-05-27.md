# Query Builder AI — dizajn (FYI budući projekt, 2026-05-27)

> **Izvor:** Filipov spec ("Drugi dio koji želim napraviti, FYI") + usporedba s postojećim WhatsApp botom. ⚠️ Oblici `SearchInfo` i `widget JSON` niže su **ILUSTRATIVNI** (rekonstruirani) — točan format treba potvrditi s MobilityOne Query Builder timom.

## Cilj
AI kao **alternativni unos** u postojeći Query Builder / Dashboard: korisnik opiše widget prirodnim jezikom ("Mjesečni troškovi goriva za osobna vozila") → AI popuni **widget JSON** (koristeći postojeće `SearchInfo` definicije + lookup-e) → rezultat se **preview-a** u QB-u → korisnik ručno doradi prije spremanja. **AI NE zamjenjuje QB — puni ga / ubrzava.**

## Ključna odluka: read-only + preview
Dashboard + AI je **read-only + preview** use-case: nema side-effecta, nema promjene backend statea bez korisnika. Idealno za enterprise/sigurnost (vidi *Sigurnosni model*).

## Arhitektura (AI se prilagođava postojećem QB-u, ne obratno)
- **`SearchInfo` (kanonsko, VEĆ POSTOJI)** — po entitetu: polja, filteri, group-by, agregacije, lookup definicije. AI ga **ČITA** kao "jelovnik" — ne stvara ga.
- **Query Builder state** — widget JSON koji definira upit.
- **Preview/Test pipeline** — postojeći; izvrši upit (read-only) i prikaže.
- **AI = "virtualni korisnik QB-a":** čita SearchInfo → interpretira NL → programatski bira entitet + filtere + grupiranja + agregacije + vizualizaciju → puni FINALNI widget JSON.
- AI **NE:** piše SQL, izvršava direktne DB upite, sprema dashboard, skriva logiku, hardkodira lookup vrijednosti.

## SearchInfo — kako izgleda (ilustrativno)
Per-entitet metadata = "ugovor" što se smije tražiti:
```
Expenses SearchInfo:
  fields:        [Amount, Date, ExpenseType, VehicleType, ...]
  filterable:    [{ExpenseType, lookup}, {VehicleType, lookup}, {Date, range}]
  groupBy:       [Date(month), VehicleType, ...]
  aggregations:  [SUM(Amount), COUNT, AVG, ...]
  lookups:       {ExpenseType: {endpoint, idField, labelField}, VehicleType: {...}}
```
AI bira SAMO iz ovog jelovnika → ne može izmisliti polje/filter koji ne postoji.

## Tok — primjer "Mjesečni troškovi goriva za osobna vozila"
1. Korisnik upiše u QB chat.
2. AI razloži: metrika=SUM(troškovi), entitet=Expenses, filteri={gorivo, osobna}, granularnost=mjesečno, period=∅→default 12mj, viz=∅→default tablica.
3. AI pročita `SearchInfo` za Expenses (jelovnik gore).
4. **Lookup** (kritično, vidi dolje): "gorivo"→ExpenseType `3`, "osobna"→VehicleType `1` (preko lookup endpointa, ne pogađa).
5. AI popuni **widget JSON** (deklarativni upit):
   ```json
   {"entity":"Expenses",
    "filters":[{"field":"ExpenseType","op":"=","value":3},
               {"field":"VehicleType","op":"=","value":1},
               {"field":"Date","op":"between","value":["2025-06","2026-05"]}],
    "groupBy":["Date.month"], "aggregations":[{"fn":"SUM","field":"Amount"}],
    "visualization":"table", "sort":[{"field":"SUM(Amount)","dir":"desc"}]}
   ```
6. AI objavi pretpostavke: "Pretpostavio sam zadnjih 12 mjeseci, mjesečno, tablicu. Reci ako želiš drugačije."
7. QB UI se odmah ažurira tim JSON-om (sve vidljivo, ručni override).
8. Preview pipeline izvrši (read-only) → prikaže tablicu/graf.
9. Korisnik doradi (chat ili ručno).
10. **AI nema "Save" gumb** — korisnik sam sprema na dashboard.

## Lookup model (= NAŠ `services/v2/type_resolver.py`, isti obrazac)
Ako je filter lookup polje: AI vidi u SearchInfo da je lookup → pozove lookup endpoint → iz vraćenih `{id,label}` deterministički matcha label↔tekst → **1 match = koristi; 0 ili >1 = pita korisnika. NIKAD ne pogađa id.** Ovo je 1:1 reuse-kandidat iz bota.

## Defaulti (UX pravila)
period=zadnjih 12 mj · granularnost=mjesečno · viz=tablica · sort=desc po glavnoj metrici. Dopušteni, vidljivi u UI-ju, lako promjenjivi. AI uvijek objavi "Pretpostavio sam X".

## Chat + UI + delta-promjene
- Layout: `[ Query Builder UI ] | [ AI Chat ]`. Chat prima NL, generira/mijenja upit; QB se odmah ažurira; preview se rerendera.
- **Delta vs puni JSON:** nakon prvog widgeta korisnik dora­đuje kroz chat ("dodaj i teretna", "zadnjih 6 mjeseci", "prikaži kao graf"). AI šalje **delta promjenu** na postojeći state (ne gradi ispočetka) — npr. `VehicleType =1` → `in [1,2]`. UI uvijek prikazuje finalni rezultat + dopušta ručni override. AI nikad nema "final save".

## Sigurnosni model (zašto je read-only idealno za enterprise)
- **Samo čita** — najgori ishod je kriva/prazna tablica; nikad korupcija podataka.
- **Nema SQL** (nema injection) — AI puni ograničen JSON koji **postojeći, već osigurani QB engine** validira i izvrši. AI ne može ništa što čovjek-korisnik QB-a ne može.
- **Nema skrivene logike** — sve je vidljivo u UI-ju (JSON), korisnik inspektira/override-a.
- **Nema auto-save/execute bez korisnika.**
- **Postojeće permisije** — AI je "virtualni korisnik" s istim pristupom; nema eskalacije.

→ Minimalna nova površina napada, nula novih write-putova, puna auditabilnost. (Suprotno našem botu koji PIŠE → treba mutation gate + potvrdu + idempotency + tenant izolaciju.)

## Što se DIJELI s WhatsApp botom / što je RAZLIČITO
| | QB AI | WhatsApp bot |
|---|---|---|
| Nađi metu | izaberi **entitet** (malo, vođeno SearchInfo) | pogodi **1 od 950 endpointa** (teško) |
| Sastavi | **deklarativni query JSON** | params 1 toola → **poziv** |
| Lookup ime→id | **isti obrazac** (type_resolver) | type_resolver |
| Akcija | **read-only** → preview widgeta | read ILI **WRITE** (gated potvrdom) |
| Izlaz/kanal | **widget/graf** (UI) | **tekst** (WhatsApp) |
| Spremanje | korisnik (AI ne) | korisnik "Da" → mutacija |

**Srž:** dijele mozak NL→meta→popuni→lookup; QB je deklarativan + read-only (lakši, sigurniji problem — nema 1-od-950 routinga ni write-a).

## Otvorena pitanja (za MobilityOne QB tim, prije gradnje)
- Točan oblik `SearchInfo` (polja, filterable, lookup definicije).
- Točan `widget JSON` schema (kako QB engine očekuje upit).
- Popis entiteta + koji imaju SearchInfo.
- Lookup endpoint konvencija — je li isti kao `/Lookup/*` koje bot već koristi (→ direktan reuse `type_resolver`).
- MCP (kasnije, opcijski): `generate_dashboard_widget`, `explain_widget`.

## NIJE za sad
FYI / budući projekt. **Bez koda.** Jedini konkretan reuse iz bota = lookup-match logika ([type_resolver.py](../services/v2/type_resolver.py)).
