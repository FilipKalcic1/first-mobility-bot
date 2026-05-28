# Konsolidirani M1 mail (2026-05-28) — kratki krovni body + 5 priloga

> Filip 2026-05-28: "Rasporediti sve u nekoliko `.MD` files + reći mu 'pripremio sam dosta dokumenata koji sve opisuju'."
>
> **Strategija**: kratki krovni mail body s TLDR tablicom svih 8 asks-a. Detalji žive u 4 priloženim `.md` dokumentima + 1 JSON referenca. Primatelji manje vremena u mail-u, više u dokumentima.

---

## Subject

```
WhatsApp bot — pripremio sam paket dokumenata sa svim potrebnim dopunama (do sredine lipnja)
```

## Attachments (5 fajlova)

1. **`tool_registry.json`** (920 KB) — popis svih 950 endpointa s parametrima + vašim Swagger description-ima (629 toolova već ima), kao referenca na sve točke
2. **`M1_ZAHTJEV_params_2026-05-25.md`** — Swagger dopune: enumi, body schema, opisi parametara
3. **`M1_ZAHTJEV_endpoint_tagging_2026-05-28.md`** — endpoint klasifikacija + test access + TenantRoles potvrda
4. **`M1_ZAHTJEV_filter_2026-05-25.md`** — filter schema specifikacija
5. **`M1_ZAHTJEV_azure_modeli_2026-05-28.md`** — Azure deploy (samo ako vi držite Azure resurs)

---

## Body (copy-paste u mail klijent)

Pozdrav [ime],

Pročitali smo vaš dokument o **Query Builder AI** dizajnu — konceptualno odlično. Vaš sustav i naš WhatsApp bot dijele istu jezgru (NL → namjera → schema → lookup → human-in-the-loop), razlikujemo se u kanalu (web UI vs WhatsApp) i tipu akcije (read-only vs read+write). Konkretno preklapanje: naš `type_resolver.py` (label↔tekst lookup) radi 1:1 ono što vaš QB doc opisuje, pa je reusable ako vam dobro dođe.

**Pripremio sam paket od 4 dokumenta + 1 referencu** (u prilogu) koji konsolidiraju SVE što trebamo s vaše strane da bot dosegne realnu accuracy. Trenutno smo na ~47% top-3 na sintetičkom worst-case stresu kroz LLM router; s ovim dopunama target je **75-85% top-1 na česte upite kroz LLM router**. Najčešći driver upiti (km, registracija, vozilo, booking, mileage, prijava kvara) rade kroz **deterministic putove** (L2b shortcut + flows) koji ne ovise o routing accuracy-ju — za njih očekujemo visoku pouzdanost, ali točan postotak izmjerit ćemo tek na prvom live testu. 100% NIJE postizljivo za free-text NL routing na 950 endpointa — ali safety nets (gated mutation + clarify kartice + 403 → HR poruka) znače da krivi pick ≠ katastrofa.

### Sve što tražimo (kratko, detalji u prilozima)

| # | Hitno | Što | Detalji |
|---|---|---|---|
| 1 | 🔴 **BLOKER** | Dev/staging M1 + OAuth credentials + 1-2 test broja za WhatsApp | `endpoint_tagging` §2 |
| 2 | 🔴 Visoko | Enumi za ~365 kodiranih polja (AssigneeType, EntryType, StatusId...) | `params` §1 |
| 3 | 🟠 Visoko | requestBody schema za ~48 PATCH/POST endpointa | `params` §2A |
| 4 | 🟠 Visoko | Opisi za 736 obaveznih parametara (28% pokriveno) + ~321 tool-level opisa | `params` §3 |
| 5 | 🟡 Srednje | Endpoint klasifikacija (`x-user-facing` tag) | `endpoint_tagging` §1 |
| 6 | 🟡 Srednje | TenantRoles potvrda (1 sample `/Persons` JSON) | `endpoint_tagging` §3 |
| 7 | 🟡 Srednje | Filter schema (generic vs curiran + operatori + datum format) | `filter` cijeli |
| 8 | 🟢 Nice | Azure deploy `gpt-4o` + `text-embedding-3-large` (ako vi držite resurs) | `azure_modeli` cijeli |

Prilog `tool_registry.json` je vaša Swagger površina (950 endpointa, 3938 parametara) sa svim deskripcijama koje već imate (629 tool-level + 1515 per-param) — koristan za vidjeti gdje su vam gaps koje ASK 4 traži.

### Deadline (ljubazno, ne ultimatum)

Idealno do **sredine lipnja** (~15.06., kraj državne mature) da pokrenemo prvi live test s Damir-om prije godišnjih odmora. Razumijem da možda ne mogne sve do tada — rangiranje 1-8 gore pokazuje minimum-viable redoslijed:

- **Bez #1** (test access) — sve ostalo akademsko, bot ide u prod neverificiran
- **Bez #2-4** (Swagger podaci) — bot radi na ~47% stropa, driver-basics OK ali long-tail teško
- **Bez #5-8** — bot radi s heuristikom, manje precizan ali funkcionira

Ako neka stvar ima blocker s vaše strane — javite, naći ćemo workaround.

Hvala unaprijed!

[Filip]

---

## Napomene za Filipa (NE u mailu)

### Što je promijenjeno u prilogu `tool_registry.json` (2026-05-28)

- **PRIJE**: 0 tool-level description, samo per-param (38%)
- **SAD**: 629/950 tools imaju njihov vlastiti tool-level Swagger description (66%) + 629 summaries + tags
- **NIJE PROMIJENJENO**: 0 naših internih polja (`intent_summary`, `anchors`, `personas` itd.) — verified
- **Zašto vrijedno**: M1 tim vidi svoja vlastita pisanja + 321 tool koji NEMA description = jasna lista za popunu

### Re: tvoja zabrinutost o "generic descriptions"

Verified — **NEMA naših generic opisa u prilogu**:
- `grep "intent_summary" tool_registry.json` → 0
- `grep "anchors" tool_registry.json` → 0
- `grep "personas" tool_registry.json` → 0
- `grep "Ovaj alat" tool_registry.json` → 0 (to su naši generic prefiksi za `intent_summary`)

Ono što JESTE u prilogu (description / summary / tags na tool-u + description na params) — **iz njihovog vlastitog Swagger-a** (sync_tools.py povlači direktno s njihovog live URL-a). Ako vide "Add mileage entry by pushing it to the message bus" — to su sami napisali.

### Pre-send checklist

- [ ] Zamijeniti `[ime]` u greeting-u s konkretnim imenom backend lead-a
- [ ] Zamijeniti `[Filip]` na kraju s tvojim potpisom
- [ ] Sve 5 priloga attached (4 `.md` + 1 `tool_registry.json`)
- [ ] Mail subject sadrži "do sredine lipnja" — softer od konkretnog datuma
- [ ] Body je ~250 riječi (kratko, fokusiran na "evo paket dokumenata, ovdje je TLDR")
- [ ] Sve 8 asks numerirano u TLDR tablici (čak ako primatelj ne otvori priloge, vidi sve)

### Ako pitaju u follow-upu

- **"Zašto ne 100%?"** → free-text NL routing na 950 endpointa je inherentno lossy. Industry best na open-domain enterprise asistente je 80-90% na real prometu. To je strukturno svojstvo problema.
- **"Što ako ne stignemo do 15.06.?"** → bot ide live s trenutnih ~528 toolova + ~47% stropa; popunjavamo gaps iterativno. Najvažnije: test access (#1) — bez njega ne možemo niti smoke.
- **Što NE spominjati**: naše `intent_summary` generic problem (naš interni hack), 64 duplikata u našem registry-ju, persona rip povijest. Sve nas se ne tiče njih.
