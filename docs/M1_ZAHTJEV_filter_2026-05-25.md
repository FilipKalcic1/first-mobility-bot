# Zahtjev MobilityOne backend timu — filter schema

## Zašto

WhatsApp bot poziva vaše **list-endpointe** i šalje `Filter` query parametar u obliku `Polje(operator)Vrijednost`, više uvjeta spojeno s `" and "` (npr. `LicencePlate(=)DA533 and GeneralStatusId(=)1`).

Bot zna sintaksu i koji endpointi primaju `Filter`, ali **ne zna po kojim se poljima smije filtrirati, kojim operatorima, ni koje su dozvoljene vrijednosti kodiranih polja**. Swagger to trenutno ne deklarira, pa bot ne može pouzdano graditi filtere (riskira grešku "Unknown filter field").

**U prilogu: `tool_registry.json`** — popis svih endpointa i polja koja vraćaju, kao referenca.

---

## 0. Prvo pitanje (određuje koliko ostalog treba)

**Je li `Filter` generički ili curiran?**
- **Generički** = prima bilo koje stvarno polje entiteta (ono što endpoint vraća u responseu). Tada nam **ne treba popis polja** — koristit ćemo polja iz responsea; trebamo samo točke **2, 3, 4**.
- **Curiran** = samo određena polja su filtrabilna. Tada nam treba **1** (popis po endpointu).

---

## 1. Filtrabilna polja po endpointu (samo ako je `Filter` curiran)

Za svaki list-endpoint: koja su polja filtrabilna, koji tip, koji operatori.

**Najlakše za vas (preferirano): jedan discovery endpoint** umjesto anotiranja stotina endpointa:
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

---

## 2. Enum dictionary (za kodirana polja — status/tip itd.)

Za svako kodirano filtrabilno polje, mapiranje koda na ljudski naziv. Bez ovog "status = aktivno" je neprevodivo u broj.
```json
"VehicleStatus": [
  {"id": 1, "hr": "Aktivno",  "en": "Active"},
  {"id": 2, "hr": "Servis",   "en": "In Service"},
  {"id": 3, "hr": "Prodano",  "en": "Sold"}
]
```
*(Ovo se preklapa s enum-zahtjevom iz dokumenta za parametre — isti dictionary pokriva oboje.)*

---

## 3. Operatori

Puni popis podržanih operatora (`(=)`, `(!=)`, `(contains)`, `(>)`, `(<)`, `(>=)`, `(<=)`, …) i **podržava li `Filter` `OR`** (ne samo `and`).

---

## 4. Datum format

Koji format `Filter` očekuje za datume (npr. ISO `2026-05-25` ili `25.05.2026`)?

---

## 5. (Nice-to-have) Strukturiran 400 na filter-error

Da bot može razlikovati "pogrešno polje" od "API nedostupan":
```json
{"error_code": "FILTER_INVALID", "field": "Manufacturer", "reason": "operator (=) not supported, use (contains)"}
```

---

## Kako vratiti
Discovery endpoint (preferirano) ili `x-filterable` u Swaggeru + enum dictionary. Pošaljite URL/JSON — mi uvučemo automatski.

## Prioritet
**0) generic vs curiran** → (1 popis polja ako curiran) → **2) enumi** → **3) operatori** su blokeri. 4) datum je sitno. 5) strukturiran 400 je polish.
