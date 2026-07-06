# M1 API PLAYBOOK — konkretni pozivi + prve 2 akcije (booking + prijava štete)

*Dopuna `MASTER_BUILD_PROMPT.md`: on daje arhitekturu, OVO daje TOČNE pozive
(auth, Persons, MasterData, AvailableVehicles, VehicleCalendar) i prve 2 akcije
do razine payloada. `.env` u projektu ima prave dev kredencijale → SMIJEŠ i
TREBAŠ live-testirati svaki poziv.*

> ⚠ SIGURNOST: pravi `client_secret`/tokeni idu SAMO u `.env` (§4.5). Ovdje su
> placeholderi. Vrijednosti koje su ikad zalijepljene u chat rotirati prije prod.

**Host (dev):** `https://dev-k1.mobilityone.io`
**Faza 1 (za srijedu):** SAMO WhatsApp + **book_vehicle** (demo prioritet) i
**report_incident** (prijava štete). Ništa drugo se sad ne gradi.

---

## §1 AUTH — dohvat tokena (client_credentials)
```bash
curl -X POST "https://dev-k1.mobilityone.io/sso/connect/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "client_id=${MOBILITY_CLIENT_ID}" \
  --data-urlencode "client_secret=${MOBILITY_CLIENT_SECRET}" \
  --data-urlencode "audience=none" \
  --data-urlencode "grant_type=client_credentials"
# → { "access_token": "...", "expires_in": ..., "token_type": "Bearer" }
```
- `client_id` = `m1AI` (novi, za AI); `client_secret` iz `.env`.
- **`audience=none` i form-encoded su OBAVEZNI** (ne JSON). Ovo je već točno u
  postojećem `token_manager.py` — reuse.
- Token se kešira do isteka (`expires_in`); na 401 → refresh (§12 build prompta).
- Auth postoji ISKLJUČIVO za ove CRUD pozive (M1 potvrdio Filipov uvid).

## §2 IDENTITET — osoba po broju telefona
```bash
curl -X GET \
 "https://dev-k1.mobilityone.io/tenantmgt/Persons?Filter=Phone%28=%29${PHONE}" \
 -H "accept: text/plain" \
 -H "x-tenant: ${DEV_TENANT_ID}" \
 -H "Authorization: Bearer ${TOKEN}"
# Filter=Phone(=)385955087196  (zagrade URL-enc: %28 %29; broj bez +)
```
- Vraća osobu: `Id` (personId), `FirstName`, `LastName`, **`TenantId`** (pravi
  tenant korisnika — §16), + org polja.
- `x-tenant` za OVAJ lookup = dev default tenant iz `.env` (pilot = 1 tenant).
- Radi za više brojeva (Filipov i njegov testni). Format broja: `385…` (M1 zna i
  `+385`/`0…` → postojeći NSN contains-fallback, M-ana identity.py).

## §3 DODIJELJENO VOZILO — MasterData po osobi (za pozdrav)
```bash
curl -X GET \
 "https://dev-k1.mobilityone.io/automation/MasterData?personId=${PERSON_ID}" \
 -H "accept: text/plain" \
 -H "x-tenant: ${DEV_TENANT_ID}" \
 -H "Authorization: Bearer ${TOKEN}"
# → podaci o vozilu dodijeljenom osobi (svaka osoba ima jedno vozilo)
```

## §4 POZDRAV (na početku razgovora — lijep UX)
Kad korisnik INICIRA razgovor (prvi turn u novoj sesiji):
```
identity.resolve(phone) → Persons (FirstName, LastName, TenantId)
                        → MasterData(personId) (dodijeljeno vozilo)
POZDRAV:
  ako ima dodijeljeno vozilo:
    "Bok {FirstName}! Vidim da ti je dodijeljeno vozilo {vozilo}.
     Kako ti mogu pomoći?"
  ako NEMA vozilo:
    "Bok {FirstName}! Kako ti mogu pomoći? (npr. mogu ti rezervirati vozilo)"
```

---

## §5 AKCIJA 1 — `book_vehicle` (rezervacija vozila) ⟵ DEMO PRIORITET (srijeda)

**Priča:** korisnik traži vozilo → skupimo parametre → nađemo slobodno → potvrda
→ rezerviramo.

**Parametri (AI ih izvlači / pita ako fale):**
| param | required | ide u backend (Faza 1) |
|---|---|---|
| `date_from` (datum/vrijeme polaska) | ✅ | `from` + `FromTime` |
| `date_to` (datum/vrijeme povratka) | ✅ | `to` + `ToTime` |
| `destination` (odredište) | ✗ | → `Description` (opc.) |
| `purpose` (svrha puta) | ✗ | → `Description` (opc.) |
| `passengers` (broj putnika) | ✗ | → `Description` (opc.) |

> Faza 1: backend prima SAMO `from`/`to` za pretragu; odredište/svrha/putnici
> idu u `Description` (spoji ih; smiješ preskočiti za MVP). Kasnije backend dobiva
> i te parametre.

**Korak 1 — nađi slobodna vozila:**
```bash
curl -X GET \
 "https://dev-k1.mobilityone.io/vehiclemgt/AvailableVehicles?from=${FROM}&to=${TO}" \
 -H "accept: text/plain" -H "x-tenant: ${DEV_TENANT_ID}" -H "Authorization: Bearer ${TOKEN}"
# from/to format: "YYYY-MM-DD HH:MM"  (razmak; URL-enc %20 i %3A)
#   npr. from=2025-12-16 09:45  to=2025-12-17 09:45
# → popis vozila (svako ima VehicleId, naziv…), ILI prazno = nema slobodnih
```

**Korak 2 — UX izbor (za demo: JEDNOSTAVNIJA varijanta):**
```
JEDNOSTAVNIJA (preporuka za srijedu): prikaži PRVO vozilo s popisa →
   "Slobodno je {vozilo} za {period}. Da rezerviram? (Da/Ne)"
KOMPLICIRANIJA (kasnije): prikaži popis (1️⃣2️⃣3️⃣) → korisnik bira broj
Prazan popis → "Nažalost nema slobodnih vozila u tom periodu. Drugi termin?"
```

**Korak 3 — potvrda (Da/Ne mutation gate) → stvarni booking POST:**
```bash
curl -X POST "https://dev-k1.mobilityone.io/vehiclemgt/VehicleCalendar/" \
 -H "Content-Type: application/json" -H "x-tenant: ${DEV_TENANT_ID}" \
 -H "Authorization: Bearer ${TOKEN}" -H "Idempotency-Key: ${UUID}" \
 -d '{
   "AssignedToId": "'"${PERSON_ID}"'",   // Id osobe s čijeg broja stiže poruka (iz §2)
   "Description": null,                    // ili "odredište; svrha; N putnika"
   "AssigneeType": 1,                      // KONSTANTA — backend/inject, AI NE dira
   "EntryType": 0,                         // KONSTANTA — backend/inject, AI NE dira
   "FromTime": "2025-12-16T09:45:42",      // ISO (T, sekunde) — NE isti format kao query!
   "ToTime": "2025-12-17T09:45:42",
   "VehicleId": "'"${SELECTED_VEHICLE_ID}"'"  // iz Koraka 1
 }'
# uspjeh → potvrdi; greška → HR objašnjenje (api_error_translator)
```

**KRITIČNI DETALJI (mine!):**
- **Dva formata datuma:** query (`AvailableVehicles`) = `"YYYY-MM-DD HH:MM"`
  (razmak); body (`VehicleCalendar`) = ISO `"YYYY-MM-DDTHH:MM:SS"` (T). Ne miješaj.
- **`AssigneeType:1`, `EntryType:0`** = interne konstante → `policy.inject` /
  backend, **AI ih NIKAD ne generira** (§11 3. skupina parametara).
- **`AssignedToId` = personId iz §2** (identitet), NE iz teksta korisnika (INV-3/11).
- **Timezone (M8):** datumi su naive (bez offseta) — pretpostavi Europe/Zagreb i
  ZAPIŠI pretpostavku; potvrdi s Damirom (isti format u query i body?).
- **Idempotency-Key** na POST (M9) — da retry nakon timeouta ne napravi dvostruku
  rezervaciju.

**actions.json entry (book_vehicle) je već u `MASTER_BUILD_PROMPT.md §11.**
Ovdje je `execution` konkretno: pretraga = `GET /vehiclemgt/AvailableVehicles`,
upis = `POST /vehiclemgt/VehicleCalendar/` (dvokorak — orkestracija u botu za
Fazu 1, dok backend ne izloži jednu `/actions/book-vehicle` akciju).

---

## §6 AKCIJA 2 — `report_incident` (prijava štete)
- Schema (registration_plate, description, incident_type) je u build promptu §11.
- ⚠ Konkretan endpoint + payload za štetu **Damir još daje** (rekao "imam
  endpoint, mogu raspisati"). Do tada: implementiraj mozak-dio (prepoznavanje +
  parametri + confirm), a `execution` ostavi kao TODO s jasnim mjestom gdje
  ubaciti rutu/payload čim stigne (isti obrazac kao booking §5).
- Za srijedu je fokus BOOKING; incident kreni čim Damir da endpoint.

---

## §7 PROAKTIVNE PORUKE (kasnije — ne za srijedu)
Damir: umjesto mailova za kilometražu → WhatsApp poruka korisniku, pa iz njegovog
odgovora ravno `add_mileage`. To je OUTBOUND-inicirano (mi započinjemo razgovor).
Napomena: WhatsApp 24h prozor / template pravila (showstopper) — graditi TEK kad
se odobri template. Zabilježeno kao Faza 2.

---

## §8 ŠTO OVO ZNAČI ZA BUILD (sažetak za graditelja)
1. Auth + Persons + MasterData su TEMELJ — implementiraj i **live-testiraj** prve.
2. Pozdrav po imenu + dodijeljeno vozilo = prvi vidljivi rezultat (§4).
3. `book_vehicle` end-to-end (§5) = **demo za srijedu**. Jednostavnija UX varijanta.
4. `report_incident` mozak-dio spreman; `execution` čeka Damirov endpoint.
5. Sve ostale akcije (~30) su BUDUĆNOST — ports&adapters + actions.json ih puštaju
   bez dirania jezgre (§27). Faza 1 = TE 2 akcije, ni više ni manje.
