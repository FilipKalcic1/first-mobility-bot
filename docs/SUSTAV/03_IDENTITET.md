# 03 — IDENTITET (telefon → osoba / tenant / vozilo)

**Svrha**: Rezolucija telefonskog broja u `person_id`/`tenant_id` i preuzimanje master podataka vozila, s Redis cacheom od 30s. Temelj cijelog sustava — bez `person_id`+`tenant_id` korisnik zapne na enrollment gate.

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/v2/identity.py` | 585 | LIVE | `IdentityContext` — rezolucija broja; `IdentitySnapshot` dataclass |
| `services/tenant_resolver.py` | 365 | LIVE | Singleton resolver: E.164 normalizacija + Postgres `user_mappings` lookup + Redis cache + lazy onboarding |

## Ulazne točke

| Simbol | Lokacija | Što radi |
|---|---|---|
| `IdentityContext.resolve` | identity.py:209 | Glavna async metoda — vraća `IdentitySnapshot` za telefon, cache 30s |
| `resolve_tenant_for_phone` | tenant_resolver.py:362 | Wrapper: E.164 → tenant_id (Redis cache 5 min) |
| `make_v2_engine_for_production` | engine.py:2422 | Factory wireup IdentityContext + TenantResolver |

## Ključne komponente

| Simbol | Lokacija | Što radi |
|---|---|---|
| `IdentitySnapshot` | identity.py:59 | Dataclass: phone, person_id, full_name/first_name, tenant_id, company_id/org_unit_id/company_name/org_unit_name, vehicle_id/vehicle_name/licence_plate/vin/last_mileage/leasing_company/co2_emission/registration_expiry, vehicle (dict bez InitialAmount/MonthlyAmount/RemainingAmount/AcquiryComment), is_first_contact, is_known. JSON serijalizacija za Redis |
| `IdentityContext` | identity.py:146 | phone → `/tenantmgt/Persons` filter + `/automation/MasterData`. Cache miss → lazy onboarding u `user_mappings`. Cache-poisoning obrana (expected_tenant_id). **Never raises** — vraća degradiran snapshot + log |
| `_normalize_phone` | identity.py:169 | Strip whitespace/+/00 → barebone broj (385955087196). Razlika od tenant_resolver verzije (koja vraća E.164 s +) |
| `_national_significant_number` | identity.py:189 | NSN za format-robustan match: digits → drop 00/385 → drop trunk 0. Hvata 385.../+385.../0915... → isti NSN |
| `_find_person_by_phone` | identity.py:414 | Exact-first `Phone(=)`, fallback `Phone(contains){nsn}` + NSN post-verify za unique. Ambiguitet → refuse |
| `_populate_from_persons` | identity.py:448 | `/Persons` → person_id, full_name, tenant_id (MUST exist, else refuse), company_id/org_unit_id (28-param gap fix) |
| `_populate_from_masterdata` | identity.py:501 | `/automation/MasterData` → vehicle_id (od VehicleId/Id), vehicle dict, vehicle_name, licence_plate, vin, last_mileage (int), leasing_company, co2_emission, registration_expiry |
| `TenantResolver` | tenant_resolver.py:96 | Stateless phone→tenant_id: Redis read-through (5 min), Postgres SELECT, write-through. `upsert_user_mapping` raw SQL ON CONFLICT s uuid |
| `_normalize_phone` (modul) | tenant_resolver.py:58 | E.164: digits + 00→+, 0{rest}→+385{rest} (HR default), leading + |
| `upsert_user_mapping` | tenant_resolver.py:208 | Lazy onboarding: INSERT/UPDATE `user_mappings` (phone E.164, tenant_id, person_id, display_name, **explicit uuid.uuid4() id**). Best-effort |
| `purge_user_mapping` | tenant_resolver.py:270 | GDPR hard-DELETE + Redis drop + audit |

## Redis ključevi

- `v2:identity:{normalized_phone}` — IdentitySnapshot JSON, **TTL 30s**
- `tenant_phone:{e164_phone}` — tenant_id string, **TTL 300s (5 min)**

## Postgres

`user_mappings` row: `(id UUID, phone_number E.164 UNIQUE, api_identity person_id, display_name, tenant_id, is_active, created_at, updated_at)` + GDPR consent kolone (vidi [11_PODACI_STANJE](11_PODACI_STANJE.md)).

## Što NE radi

- Ne poziva `/tenantmgt/Roles` — org context samo iz `/Persons`.
- Ne queira `/Persons` multi-tenant — hardkodiran `settings.MOBILITY_TENANT_ID` za sve (bottleneck za 1000+ tenanta).
- Ne retry-a transient 503/timeout — vraća None/partial snapshot.
- Ne cachira failure zauvijek — 30s cache (i za known-unknown).

## Caveati

- **CRIT-1 fix (2026-05-28)**: `upsert_user_mapping` MORA imati explicit `uuid.uuid4()` jer alembic 001 nema `server_default` za `id` → PostgreSQL NOT NULL puca (raw SQL bypassa Python-side `default=uuid.uuid4`).
- **MED-6 race (benign)**: dva concurrent cache-miss mogu oba hit `/Persons`. Idempotentan upsert + worker per-sender lock → samo 1 extra API call. Ne fixa se.
- **TTL nesklad u docstringu**: tenant_resolver komentari (linije 15, 21) tvrde "1h", ali `DEFAULT_CACHE_TTL_SECONDS = 300` (5 min). Točno = 5 min.
- `expected_tenant_id` obrana: ako webhook resolvira tenant A ali identity vrati B → refuse + log (sprječava cross-tenant cache bleed u shared Redis).
- Prazan telefon → snapshot s `is_known=False`, `is_first_contact=True`, BEZ API poziva.
- Korisnici s null Phone u M1 ostaju neprepoznati — data-entry gap u MobilityOne, ne bug koda.
