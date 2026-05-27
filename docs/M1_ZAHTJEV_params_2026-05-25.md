# Zahtjev MobilityOne backend timu — dopune OpenAPI/Swagger (parametri)

## Zašto

WhatsApp bot poziva vaše endpointe i čita vaš **OpenAPI (Swagger)** da zna kako složiti svaki poziv — koja polja endpoint prima, koje su dozvoljene vrijednosti, i što znači koji parametar. Tri stvari trenutno **ne postoje u Swaggeru**, pa bot za dio endpointa ne može složiti ispravan poziv (mora pitati korisnika nešto što ni korisnik ne zna, ili ne zna koja polja poslati).

Kad ih dodate, bot ih pokupi **automatski** — bez ikakvih izmjena na našoj strani.

**U prilogu: `tool_registry.json`** — popis svih ~950 endpointa i njihovih parametara, kao referenca na koje se točno polje/endpoint odnosi svaka točka dolje.

---

## 1. Enum vrijednosti (najveći prioritet)

Mnogi parametri su `integer` (kodirane vrijednosti 1/2/3…) ili izbor s fiksnog popisa, ali Swagger ne navodi **što koja vrijednost znači** ni **koje su dozvoljene**. Bot tada pita korisnika "koji tip/status?" bez popisa — pa ni korisnik ne zna odgovor, i poziv ne uspije.

Molimo na schema-property dodajte `enum` (popis dozvoljenih) + `description` s mapiranjem značenja:

```yaml
AssigneeType:
  type: integer
  enum: [1, 2, 3]
  description: "1=vozač, 2=tvrtka, 3=..."   # točne vrijednosti znate vi
```

**Parametri za koje je gotovo sigurno potreban enum (obavezni, kodirani):**
`AssigneeType`, `EntryType` (VehicleCalendar) · `Status` (Equipment) · `Type`, `Visibility` (DashboardItems) · `Source` (MileageReports) · `ColumnType` (Metadata) · `Category` (PeriodicActivityTypes) · `AcquiringType` (VehicleContracts) · `StatusId`, `GeneralStatusId`, `ActivityStatusId`, `CaseStatusId`, `PeriodicActivityStatusId` (status-šifre) · `MaintenanceWarningUnits` (Pools).

**Vjerojatno trebaju enum (opcijski, ali se koriste kao izbor):**
`VehicleStatus`, `Severity`, `State`, `CaseStatus`, `CaseType`, `EquipmentStatus`, `EquipmentCategory`, `RaisedByAssigneeType`, `VehicleType`…

> Gornji popis je naša procjena po imenu+tipu i **vjerojatno nije potpun** — vi znate koja su polja stvarno "izbor s popisa". Molimo prođite **sve** kodirane/izborne parametre.
> Napomena: `PricePerUnit` (Expenses) izgleda kao kandidat po automatskom skenu, ali je decimalna cijena — **nije enum**, zanemarite.

### 1b. `*TypeId` polja (strani ključevi na /Types — nisu enum)

Polja `VehicleTypeId`, `CaseTypeId`, `ExpenseTypeId`, `PeriodicActivityTypeId`, `PersonTypeId`, `EquipmentTypeId`, `TripTypeId`, `ActivityTypeId` **nisu enum** — to su FK na vaše `/…Types` tablice. **Pitanje:** postoji li stabilan šifrarnik/`code` po tipu, ili se popis dohvaća live preko `GET /…Types`? (Dohvat + mapiranje korisnikovog teksta na ispravan id je naš posao — samo nam recite s čime radimo.)

---

## 2. requestBody schema

Mjerenje pokazuje **196 POST/PUT/PATCH** endpointa bez deklariranih named body-polja u Swaggeru, pa bot ne zna koja polja poslati. **Većina je to namjerno** — stvarni zahtjev je puno manji:

### 2A. Stvarno trebaju body-schemu — ~48 (prioritet)

Molimo izložite request DTO (`requestBody` → `application/json` → `schema` s named `properties` + `required`):

**37 × `PATCH /{Entitet}/{id}` (partial update)** — bot ne zna koja polja smije mijenjati:
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

**11 × posebni POST** — dio prima body (treba schema), dio je akcijski bez body-ja (molimo potvrdite):
| Endpoint | Naša pretpostavka |
|---|---|
| `post_Upsert_People` | treba body (popis osoba) → schema |
| `post_SyncExternalIdAndLicencePlate` | treba body → schema |
| `post_SyncExternalIdAndVIN` | treba body → schema |
| `post_Metadata_Order` | treba body (redoslijed) → schema |
| `post_TenantPermissions_SetRolesForUser_{personId}` | treba body (popis rola) → schema |
| `post_MonthlyMileages_RecalculateAll` | akcija, bez body-ja → potvrditi |
| `post_MonthlyMileages_Recalculate_{vehicleId}` | akcija (id u putu) → potvrditi |
| `post_MonthlyMileages_RequestMileageEstimation` | akcija → potvrditi |
| `post_Persons_ResendInvitation_{personId}` | akcija → potvrditi |
| `post_Partners_linktenant_{partnerId}` | akcija → potvrditi |
| `post_Partners_unlinktenant_{partnerId}` | akcija → potvrditi |

### 2B. Vjerojatno namjerno bez JSON body-ja — ~148 (molimo samo potvrdite)

Sustavni obrazac (svaki entitet ima isti set). Vjerojatno nisu greška — potvrdite da ih ispravno označimo:
- **37 × `POST /{Entitet}/multipatch`** — body je niz patch-operacija (bulk).
- **37 × `POST /{Entitet}/{id}/documents`** — upload datoteke (multipart, ne JSON).
- **74 × `PUT /{Entitet}/{id}/documents/{documentId}` + `…/SetAsDefault`** — operacije nad dokumentom (path-param je dovoljan).

---

## 3. Opisi parametara

Samo **282 od 1018** obaveznih parametara (28 %) ima `description`. Bez opisa bot teže pogađa značenje polja. Molimo dodajte kratak `description` po parametru, prioritetno obaveznima.

Najveće rupe: `PeriodicActivityTypes`, `PersonActivityTypes`, `Expenses`, `VehicleAssignments`, `Metadata`.

---

## 4. Default vrijednosti (opcijski, sitno)

Ako parametar ima smislen default, deklarirajte `default:` u schemi → bot ga ne mora ni izvlačiti ni pitati.

---

## Kako vratiti

Sve je standardni **OpenAPI 3** (vjerojatno anotacije na DTO razini — `[Required]`, enum tipovi, XML-doc komentari koje Swashbuckle/NSwag pretvori u schema/description). Nakon dopuna **pošaljite osvježeni Swagger URL** (ili izvezeni JSON) — mi pokrenemo sync i bot odmah koristi nove podatke.

## Prioritet (po dobitku)
**1) enumi (§1)** → **2A) requestBody ~48 (§2A)** → **3) opisi (§3)** → potvrda §2B → 4) defaults (§4).
