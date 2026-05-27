# Filter redizajn — što pitati M1 + što pokrećemo zajedno (2026-05-25)

Filter (po čemu se smije filtrirati liste) je **resetiran na nulu** (commit `8f9ba4d`). Da ga vratimo ispravno (bez nagađanja, bez 500-grešaka), trebamo od MobilityOne **schema-znanje** (po kojim poljima se filtrira, kojim operatorima, koje su dozvoljene vrijednosti). Mi ne smijemo to pogađati.

Dokument ima 2 dijela:
- **Dio A** — copy-paste pošalji M1 timu.
- **Dio B** — koraci koje radimo zajedno (ti + ja) nakon što M1 odgovori.

---

# DIO A — Što pitati MobilityOne tim

> *Kontekst za njih:* WhatsApp bot zove vaše list-endpointe i šalje `Filter` query param (`Polje(operator)Vrijednost`, više spojeno s `" and "`). Bot zna sintaksu i koji endpointi primaju `Filter`, ali **ne zna po kojim se poljima smije filtrirati, kojim operatorima, ni koje su dozvoljene vrijednosti kodiranih polja**. Swagger to trenutno ne deklarira. Molimo sljedeće:

## 0. Prvo pitanje (određuje koliko ostalog treba)
**Je li `Filter` generički ili curiran?**
- **Generički** = prima BILO KOJE stvarno polje entiteta (ono što endpoint vraća u responseu). Tada nam **ne treba popis polja** — koristit ćemo polja iz responsea; trebamo samo točke **2, 3, 4** dolje.
- **Curiran** = samo određena polja su filtrabilna. Tada nam treba **1** (popis po endpointu).

## 1. Filtrabilna polja po endpointu (samo ako je `Filter` curiran)
Po list-endpointu: koja su polja filtrabilna, koji tip, koji operatori.
**Najlakše za vas (preferirano): jedan discovery endpoint** umjesto anotiranja 266 endpointa:
```
GET /api/v2/filter-schema?endpoint=get_Vehicles
→
{
  "endpoint": "get_Vehicles",
  "filterable_fields": [
    {"name": "LicencePlate",    "type": "string", "operators": ["(=)", "(contains)"]},
    {"name": "Manufacturer",    "type": "string", "operators": ["(contains)"]},
    {"name": "GeneralStatusId", "type": "enum",   "operators": ["(=)"], "enum_ref": "VehicleStatus"},
    {"name": "CreatedAt",       "type": "date",   "operators": ["(>=)", "(<=)"]}
  ]
}
```
*(Alternativa ako vam je draže u Swaggeru: `x-filterable` vendor-extension na endpointu s istim sadržajem.)*

## 2. Enum dictionary (za kodirana polja — status/tip itd.)
Za svako kodirano polje, mapiranje koda na ljudski naziv. Bez ovog "status = aktivno" je neprevodivo.
```json
"VehicleStatus": [
  {"id": 1, "hr": "Aktivno",  "en": "Active"},
  {"id": 2, "hr": "Servis",   "en": "In Service"},
  {"id": 3, "hr": "Prodano",  "en": "Sold"}
]
```

## 3. Operatori
Puni popis podržanih operatora (`(=)`, `(!=)`, `(contains)`, `(>)`, `(<)`, `(>=)`, `(<=)`, …) i **podržava li `Filter` `OR`** (ne samo `and`).

## 4. Datum format
Koji format `Filter` očekuje za datume (npr. ISO `2026-05-25` ili `25.05.2026`)?

## 5. (Nice-to-have) Strukturiran 400 na filter-error
Da bot razlikuje "pogrešno polje" od "API down":
```json
{"error_code": "FILTER_INVALID", "field": "Manufacturer", "reason": "operator (=) not supported, use (contains)"}
```

**Prioritet:** točka 0 + (1 ako curiran) + 2 + 3 su blokeri za filter. 4 je sitno. 5 je polish.
**Napomena:** ne tražimo da pišete naš interni registry — samo gornje (discovery endpoint ILI Swagger anotacije); mi to sami uvučemo.

---

# DIO B — Što pokrećemo zajedno (nakon M1 odgovora)

## Korak 1 — Probe (ti pokreneš, kad M1 token popusti)
```
python scripts/probe_auto_mappings.py --phone 0915087196
```
Sekcija **FILTER-TEST** (V1/V2/V3) pokuša filtrirati `get_Vehicles` po nekoliko `output_keys` polja + operatorima.
- Ako V1 polja prođu → **filter je generički** → ne trebamo da M1 nabraja polja (output_keys = popis); M1 ask se svodi na enume + operatore (Dio A: 2, 3, 4).
- Ako padnu (500) → **curiran** → treba M1 popis (Dio A: 1).
- Operatori koji prođu = naš stvarni set.

→ Pošalji mi output; time znamo koliko velik je M1 ask zapravo.

## Korak 2 — Ja gradim pipeline (nakon probe + M1 podataka)
Data-driven, no-guess. Gradi se tek kad imamo schemu (ne unaprijed):
1. **parser-ingest** — `swagger_parser` uvuče discovery/x-filterable → per-tool `filterable_fields` + enum dictionary. (Enum vrijednosti parser već uvlači.)
2. **filter_extractor** (novo) — LLM **ograničen na ta polja + enum-vrijednosti** izvuče `{polje, operator, vrijednost}` iz poruke (ili ništa). Ograničen schemom ⇒ ne halucinira.
3. **filter_builder** — recover iz gita (`git show 8f9ba4d^:services/v2/filter_builder.py`) — bio ispravan: sanitize + `Polje(op)Vrijednost` + spoji `" and "`.
4. **validate-before-send** — svaki `(polje, operator)` mora biti u schemi → inače dropni → **nikad ne šaljemo nevažeći filter** (0 izazvanih 500).
5. **confirm scope** — re-add `STAGE_FILTER` (sad multi-field): "Razumio: za aktivna Škoda vozila. Točno? (Da/Ne)".
6. **fallback menu** — velika lista + ništa izvučeno → ponudi top filtrabilna polja; za enum-polje pokaži labele.
7. **testovi** + mjerenje (extraction točnost na filter-upitima).

## Korak 3 — Mjerenje + postupno paljenje
Filter se pali **per-tool** kako schema stiže (prazna schema = nema filtera = današnje stanje). Mjerimo prije širenja.

---

## Ovisnosti i rizici (pošteno)
- **Ovisi o M1** — bez Dio-A podataka (ili potvrde "generički") filter ostaje nula. To je strop koji naš kod ne prelazi.
- **+1 LLM poziv** po filter-upitu (extractor) — mala cijena/latencija.
- **validate-before-send** je ključan — sprječava da nevažeće polje izazove 500 ("bot pukao").
- **Gradimo tek nakon podataka** — ne radimo inert stroj unaprijed (izbjegava dizajn protiv neprovjerene scheme).

## Što je već spremno
- Probe (Korak 1) — postoji u `scripts/probe_auto_mappings.py` (sekcija FILTER-TEST).
- `filter_builder` — recoverable iz gita (`8f9ba4d^`), bio ispravan.
- `STAGE_FILTER` confirm obrazac — bio ispravan, re-add iz gita.
- Flow referenca — `docs/BOT_FLOW_2026-05-25.md`.
