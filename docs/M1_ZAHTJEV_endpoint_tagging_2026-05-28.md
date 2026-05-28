# Zahtjev MobilityOne backend timu — endpoint klasifikacija + test access

## Zašto

Uz tehničke dopune Swagger-a (enumi, body schema, opisi — zaseban dokument), trebam **2 stvari koje znatno utječu na accuracy WhatsApp bota**:

1. **Endpoint klasifikacija** — koji su za krajnje korisnike vs interni
2. **Test okruženje** — da možemo verificirati pozive prije produkcije

Bez njih bot funkcionira na našoj heuristici (nagađanju) i bez ikakvog smoke testa protiv live API-ja.

---

## 1. Endpoint klasifikacija (koji endpointi su za chat use case?)

### Trenutna situacija

Bot ima ~950 endpointa iz vašeg Swagger-a. **Otprilike 410 NIJE za chat use case** — tj. nijedan vozač/manager neće tipkati WhatsApp poruku koja bi rezultirala tim endpointom:

| Kategorija | Otprilike | Primjer | Zašto nije za chat |
|---|---|---|---|
| `_DistinctMakes`, `_DistinctModels`, ... | ~80 | `get_VehicleInputHelper_DistinctMakes` | Web UI dropdown autocomplete |
| `_GroupBy`, `_ProjectTo`, `_metadata` | ~100 | `get_Cases_GroupBy` | Backend introspection / UI grafovi |
| `_DeleteByCriteria`, `_multipatch` | ~60 | `delete_Cases_DeleteByCriteria` | Bulk operacije s JSON array body-jem |
| `Latest_*` | ~50 | `get_LatestVehicleCalendar` | UI dashboard snapshots |
| Admin/system tenant ops | ~120 | `post_Companies`, sync endpointi | Multi-tenant management |

Trenutno **ovo filtriramo regexom (po imenu) + ručno kuriranim popisom** (`tool_subset.json` od 594 dozvoljenih). To je **naša heuristika bez vašeg autoritativnog signala** — možemo griješiti u oba smjera (blokirati nešto što bi user trebao, ili pustiti nešto što nije za njega).

### Što tražimo

Bilo koja od sljedeće 3 opcije (po prioritetu, ne treba sve):

#### Opcija A (preferirana, najlakša za vas) — `x-user-facing` tag u Swagger-u

```yaml
paths:
  /Vehicles/{id}:
    get:
      x-user-facing: true        # ← vidi krajnji user
      summary: "Pojedinosti vozila"
  /VehicleInputHelper/DistinctMakes:
    get:
      x-user-facing: false       # ← interno, UI helper
      summary: "Distinct vrijednosti za dropdown"
```

Bot odmah preko `sync_tools` skripte koristi taj tag — eliminira naše nagađanje.

#### Opcija B (više detalja, ako lakše za vas) — `x-audience` tag

```yaml
paths:
  /Vehicles/{id}:
    get:
      x-audience: "driver,manager,admin"
  /Companies:
    post:
      x-audience: "admin"
  /VehicleInputHelper/DistinctMakes:
    get:
      x-audience: "internal"
```

Time pokrivamo i buduće persona-grupiranje (ako jednom dobijemo pouzdan izvor uloge korisnika).

#### Opcija C — per-endpoint OAuth scope info

Ako svaki endpoint već štiti specifičan OAuth scope u tokenu (npr. `fleet.read`, `admin.write`), molimo:
- Popis svih scope-ova koje koristite
- Mapiranje endpoint → required scope

Tada bot može sam mapirati scope iz user-ovog tokena → koje endpointe smije nuditi.

### Što ne treba (mit-busting)

- **NE** trebamo da "obrišete" ili sakrijete endpointe iz Swagger-a — samo tag/anotaciju.
- **NE** trebamo poseban "user endpoints" Swagger varijantu. Jedna verzija s tagovima je dovoljna.

### Što time dobivamo

- Eliminacija naše heuristike (`tool_subset.json` postaje auto-generiran iz tag-ova)
- Manji search pool za router → bolja accuracy (verified mjerenje pokazuje da je svaki +100 toolova u kandidat pool-u oko -1-2pp accuracy)
- Sigurnije ponašanje — bot ne nudi opasne bulk-delete endpointe (sad ih filtrira regex, što je krhko)

---

## 2. Test okruženje + token za smoke

### Trenutna situacija

Bot nikad nije poslao **stvarnu WhatsApp poruku end-to-end** u dev/staging M1 okruženju. Sve do sad testirano je na razini koda (mock gateway). Prvi pravi test = direktno u produkciji što je rizik.

Konkretno blokira:
- Verificirati da naš `requestBody` shape stvarno radi za PATCH/POST endpointe
- Verificirati da naš `Filter` parametar sintaksa radi
- Verificirati identity flow (`/Persons` query → `TenantId` → ostali tenant-scoped pozivi)
- Pokupiti edge case-eve koje mock ne pokazuje (header format, error JSON shape, latencija)

Sad imamo `dev-k1` koji povremeno timeout-a (auth server nije reachable). Bez živog dev/staging okruženja **ne možemo pokrenuti pravi smoke**.

### Što tražimo

Po prioritetu:

1. **Dev/staging M1 instanca URL** — koja se može sigurno read+write koristiti za testove (bez utjecaja na produkciju ili pravu klijentsku data).

2. **OAuth client credentials** (`client_id` + `client_secret`) za taj environment.

3. **1-2 test broja** (vozač + manager, idealno) — registrirana u `/Persons` tog environment-a — koja možemo koristiti za stvarni WhatsApp smoke test.

4. **Test podaci** — barem 2-3 vozila, par rezervacija, jedan tip troška u tom test-tenantu. Bez tih podataka query-ji vraćaju prazne liste i ne mogu se verificirati format-i odgovora.

### Što time dobivamo

- Stvarni P0 smoke test (10 reprezentativnih queries) prije produkcije
- Detekcija razlika između našeg pretpostavljenog API contracta i live API contracta (header, error format, status kodovi)
- Verifikacija OAuth flow-a (token refresh, 403 handling)
- Pošten "live" benchmark accuracy (umjesto sintetičkog evala)

---

## 3. TenantRoles — potvrda i mapiranje

Vaš Swagger deklarira polje `TenantRoles` na `/Persons` response-u, ali nismo verificirali:

1. **Puni li se to polje na živom API-ju?** (deklarirano u schemi ≠ stvarno vraćeno)
2. **Kojeg je tipa?** Array string-ova, array objekata, ili nešto drugo?
3. **Koje su moguće vrijednosti?** (npr. `"FleetManager"`, `"Admin"`, `"Driver"`, ili interno-id-evi?)

Razlog: ako pouzdano vraćate role korisnika, bot može automatski derivirati persona-tier (driver/manager/admin) bez per-tenant ručnih popisa. To nije bloker — radimo i bez tog — ali znatno bi pojednostavilo onboarding novih klijenata.

**Konkretno: 1 sample `/Persons?Filter=Phone(=){test-broj}` JSON response** s realnim `TenantRoles` vrijednosti je sve što trebamo. Ne mora biti dokumentirano — sample je dovoljan.

---

## Prioritet

**1) Test okruženje + token (§2)** — bez ovog, sve ostalo je akademsko. Najhitnije.
**2) Endpoint klasifikacija (§1)** — najveći lift accuracy bez nove infrastrukture na našoj strani.
**3) TenantRoles potvrda (§3)** — sample odgovor, malo posla, omogućuje buduće persona-grupiranje.

---

## Kako vratiti

- Endpoint klasifikacija: pošaljite osvježeni Swagger URL (ili JSON) kad budete dodali tagove.
- Test access: client credentials + URL preko sigurnog kanala (1Password, Bitwarden Send, ili kako preferirate).

Hvala!
