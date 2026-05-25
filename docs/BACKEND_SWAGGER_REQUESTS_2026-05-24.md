# MobilityOne OpenAPI/Swagger — što nedostaje za WhatsApp bota (2026-05-24)

## Kontekst (zašto ovo tražimo)

WhatsApp bot čita vaš OpenAPI (Swagger) da bi znao kako pozvati svaki endpoint — koja polja prima, koje su dozvoljene vrijednosti i što znači koji parametar. Tri stvari trenutno **ne postoje u Swaggeru**, pa bot za dio endpointa ne može složiti ispravan poziv iz korisničke WhatsApp poruke (mora pitati korisnika nešto što ni korisnik ne zna, ili ne zna koja polja poslati).

Kad ih dodate, bot ih pokupi **automatski** (sljedećim sync-om registra) — **bez ijedne izmjene na našoj strani**. Ovo je jedini stvarni način da se otključa otprilike polovica endpointa za rad preko chata.

Analiza je rađena nad vašim live Swaggerom: **950 endpointa / 3938 parametara**.

---

## 1. Enum / kodirane "izborne" vrijednosti (NAJVEĆI prioritet)

Mnogi parametri su `integer` (kodirane vrijednosti 1/2/3…) ili izbor s fiksnog popisa, ali Swagger ne navodi **što koja vrijednost znači** ni **koje su dozvoljene**. Bot pita "koji tip/status?", ali nema popis — pa ni korisnik ne zna. Posljedica: ti se endpointi ne mogu pouzdano pozvati.

> **VAŽNO — popis niže je naša heuristička procjena (po imenu + tipu) i vjerojatno NIJE potpun.** Vi znate koja su polja stvarno "izbor s popisa". Molimo **prođite SVE kodirane/izborne parametre** i dodajte `enum` + `description`; ovaj popis je polazište, ne konačan.

Molimo na schema-property dodati `enum` (popis dozvoljenih) + `description` s mapiranjem značenja.

**Obavezni, kodirani (sigurno enum):**
`AssigneeType`, `EntryType` (VehicleCalendar) · `Status` (Equipment) · `Type`, `Visibility` (DashboardItems) · `Source` (MileageReports) · `ColumnType` (Metadata) · `Category` (PeriodicActivityTypes) · `AcquiringType` (VehicleContracts) · `StatusId`, `GeneralStatusId`, `ActivityStatusId`, `CaseStatusId`, `PeriodicActivityStatusId` (status-šifre) · `MaintenanceWarningUnits` (Pools).

**Opcijski, ali isto trebaju vrijednosti (vrlo vjerojatno enum):**
`VehicleStatus` (×14), `Severity`, `State`, `CaseStatus`, `CaseType`, `EquipmentStatus`, `EquipmentCategory`, `RaisedByAssigneeType`, `VehicleType`…

Primjer (OpenAPI 3):
```yaml
AssigneeType:
  type: integer
  enum: [1, 2, 3]
  description: "1=vozač, 2=tvrtka, 3=..."   # točne vrijednosti znate vi
```
**Vrijednosti znate samo vi** — mi ih ne smijemo pogađati (pogrešna vrijednost = pogrešan zapis). Zato ovo mora doći iz backenda.

### 1b. FK reference na "tip" tablice (`*TypeId`) — povezano, ali drugačije

Polja poput `VehicleTypeId`, `CaseTypeId`, `ExpenseTypeId`, `PeriodicActivityTypeId`, `PersonTypeId`, `EquipmentTypeId`, `TripTypeId`, `ActivityTypeId` **NISU enumi** — to su strani ključevi na vaše `/…Types` tablice (korisnik bira postojeći tip). Za njih NE treba `enum`. **Pitanje za vas:** postoji li stabilan šifrarnik/`code` po tipu, ili se popis dohvaća live preko GET `/…Types`? Trebamo to znati da bot može mapirati korisnikov tekst ("kvar") na ispravan `*TypeId` (dohvat + mapiranje je naš posao — samo recite s čime radimo).

---

## 2. requestBody schema — fokusirano (NE svih 196!)

Mjerenje pokazuje 196 POST/PUT/PATCH endpointa bez deklariranih named body-polja. **ALI većina je to namjerno** — pa je stvarni zahtjev puno manji. Razdvojili smo pošteno:

### 2A. Stvarno trebaju body-schemu — ~48 (PRIORITET)

**37 × `PATCH /{Entitet}/{id}` (partial update)** — bot ne zna koja polja smije mijenjati jer schema nije deklarirana. Molimo izložite request DTO (`requestBody` → `application/json` → `schema` s named `properties` + `required`):

```
patch_CaseTypes_id, patch_Cases_id, patch_Companies_id, patch_CostCenters_id,
patch_DocumentTypes_id, patch_EquipmentCalendar_id, patch_EquipmentTypes_id,
patch_Equipment_id, patch_ExpenseGroups_id, patch_ExpenseTypes_id, patch_Expenses_id,
patch_Metadata_id, patch_MileageReports_id, patch_OrgUnits_id, patch_Partners_id,
patch_PeriodicActivitiesSchedules_id, patch_PeriodicActivities_id,
patch_PeriodicActivityTypes_id, patch_PersonActivityTypes_id, patch_PersonOrgUnits_id,
patch_PersonPeriodicActivities_id, patch_PersonTypes_id, patch_Persons_id, patch_Pools_id,
patch_SchedulingModels_id, patch_Tags_id, patch_TeamMembers_id, patch_Teams_id,
patch_TenantPermissions_id, patch_Tenants_id, patch_TripTypes_id, patch_Trips_id,
patch_VehicleCalendar_id, patch_VehicleContracts_id, patch_VehicleTypes_id,
patch_VehiclesHistoricalEntries_id, patch_Vehicles_id
```

**11 × posebni POST** — dio PRIMA body (deklarirati schemu), dio je akcijski bez body-ja (potvrditi da je namjerno prazan):
| Endpoint | Vjerojatno |
|---|---|
| `post_Upsert_People` | treba body (popis osoba) → schema |
| `post_SyncExternalIdAndLicencePlate` | treba body → schema |
| `post_SyncExternalIdAndVIN` | treba body → schema |
| `post_Metadata_Order` | treba body (redoslijed) → schema |
| `post_MonthlyMileages_RecalculateAll` | akcija, bez body-ja → potvrditi |
| `post_MonthlyMileages_Recalculate_{vehicleId}` | akcija (id u putu) → potvrditi |
| `post_MonthlyMileages_RequestMileageEstimation` | akcija → potvrditi |
| `post_Persons_ResendInvitation_{personId}` | akcija (id u putu) → potvrditi |
| `post_Partners_linktenant_{partnerId}` | akcija → potvrditi |
| `post_Partners_unlinktenant_{partnerId}` | akcija → potvrditi |
| `post_TenantPermissions_SetRolesForUser_{personId}` | treba body (popis rola) → schema |

### 2B. Vjerojatno namjerno bez JSON body-ja — ~148 (molimo samo POTVRDITE)

Sustavni obrazac (svaki entitet ima isti set). Vjerojatno **nisu greška**, ali potvrdite da ih ispravno označimo (i da bot ne troši pokušaje na njih iz chata):
- **37 × `POST /{Entitet}/multipatch`** — body je **niz** patch-operacija (bulk). Ne radi se iz chat-poruke.
- **37 × `POST /{Entitet}/{id}/documents`** — **upload datoteke** (multipart, ne JSON).
- **74 × `PUT /{Entitet}/{id}/documents/{documentId}` + `…/SetAsDefault`** — operacije nad dokumentom (path-param je dovoljan).

(Obrazac je potpuno sustavan — isti za sve entitete; puni popis možemo dostaviti, ali je izvodiv iz gornjeg uzorka.)

---

## 3. Opisi parametara — 736 obaveznih bez opisa

Samo **282 od 1018** obaveznih parametara (**28 %**) ima `description`. Bez opisa bot (i AI sloj koji čita poruku) nagađa značenje polja → lošija točnost izvlačenja. Molimo dodajte kratak `description` po parametru, prioritetno obaveznima.

Najveće rupe (najviše obaveznih polja bez opisa):
`put/post_PeriodicActivityTypes`, `put/post_PersonActivityTypes`, `post/put_Expenses`, `post_VehicleAssignments`, `put_Metadata_id`.

---

## 4. Default vrijednosti (opcijski, sitno)
Ako parametar ima smislen default, deklarirajte `default:` u schemi → bot ga ne mora ni izvlačiti ni pitati.

## Kako (tehnički)

Sve je standardni **OpenAPI 3**. Vjerojatno se anotira na DTO razini — npr. C# `[Required]`, enum *tipovi* umjesto golog `int`, te XML-doc komentari koje Swashbuckle/NSwag pretvori u `description`/`schema`. Nakon izmjena pošaljite osvježeni Swagger URL; mi pokrenemo sync i bot odmah koristi nove podatke.

## Iskrene napomene (da ne bude nesporazuma)

- **Enum-popis je POLAZIŠTE, ne konačan** — prepoznat heuristički (po imenu + tipu), pa zasigurno podcjenjuje (npr. prvo smo našli 9, pa još ~6 obaveznih + ~15 opcijskih). **Molimo prođite sve izborne parametre, ne samo nabrojane.** Točne vrijednosti znate vi.
- `PricePerUnit` (Expenses) izgleda kao enum po automatskom skenu, ali je **decimalna cijena — NIJE enum**, ignorirati.
- `*TypeId` polja (sekcija 1b) NISU enumi nego FK na /Types — drugačiji mehanizam.
- **"196" zvuči puno, ali stvarni prioritet je ~48** (sekcija 2A). Ostalo (~148) je sustavno/namjerno (2B) i traži samo potvrdu.
- Redoslijed po dobitku: **1) enumi** → **2A) requestBody (~48)** → **3) opisi (736)** → 2B (potvrda) → 4) defaults.
- **Filter** (po kojim poljima se smije filtrirati) je namjerno **izvan ovog doca** — taj se dio redizajnira zasebno; tražit ćemo ga naknadno.
