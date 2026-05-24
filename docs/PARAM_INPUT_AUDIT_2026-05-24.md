# Param-input audit — cijela tool baza (950 tools / 3938 params), 2026-05-24

Filip: "pregledaj cijelu tool bazu da baš ništa ne propustimo od param unošenja." Iscrpna klasifikacija svakog parametra: odakle mu vrijednost dolazi i gdje su rupe.

## ✅ Pokriveno (radi)
- **Context-injection (180 params, sve string `*Id` ref)** — 5 ključeva iz identity (vehicle/person/tenant/company/orgunit), svi injectabilni. (Nakon company/orgunit fixa + misclassification fixa.)
- **Completeness guard** (executor): required context koji se ne može popuniti → `missing_required` refuse, ne silent 422.
- **user_input** — LLM extract (samo spomenuto) → ask(required) → offer(optional friendly) → filter(registracija) → pagination defaults. Coercion: int / HR-decimal / bool / date→ISO.

## 🔧 Popravljeno ovaj krug (T1 + T2): context MISCLASSIFICATION
**55 params** bila krivo tagirana `dependency_source=context` (po toolu, u oba registry filea) → executor bi injectao identity-UUID u krivo polje → 422/smeće. Demotani na `user_input` (sad se izvuku/pitaju). Pravilo (durable u `swagger_parser._context_value_appropriate` + jednokratno `scripts/fix_context_misclassification.py`): **context je legitiman SAMO ako je string + ime je entity-ref (`*Id` / personIdOrEmail / actor `CreatedBy`)**. Sve ostalo (non-string tip, value-polja) → user_input.
- **27 non-string** (int/array/bool — UUID fizički ne stane): `EntryType`, `StatusId`, `SeverityId`, `Source`, `Visibility`, `MaintenanceWarningDays/Units`, `Attempts`, `PeriodicActivityStatusId`, `VehicleStatusId`, `Filter`(array), `importScenario`(bool). **14 bili required → garantirano loš poziv; sad fixano.**
- **~28 string value-polja**: `VehicleName`, `AssigneeName`, `AssigneeCode`, `RaisedByAssigneeName`, `Code`, `Comment`, `Date`, `LastChange`, `LastMileage`, `NotificationTo`, `ChangeVehicleStatusTo`.
- **Zadržano (legitiman context)**: svi `*Id`, `personIdOrEmail`, `CreatedBy→person_id`. `get_Persons.Filter` demotan (filter_template je mrtav kod — identity gradi Phone filter direktno).
- Guard: 1621 testova zeleno (+2 regression).

## 🔴 T3 — strukturne granice (NISU extraction-fix; treba flow ili backend)
Pošteno: ovo se NE može riješiti pametnijom ekstrakcijom — body je strukturno nepopunjiv iz chat-poruke.
1. **37 `*_multipatch` toolova** (required `array` body): primaju ARRAY patch-operacija (npr. `post_Persons_multipatch`). User ne tipka JSON-array u WhatsApp. → out-of-scope za chat-param.
2. **196 mutacija bez ijednog popunjivog named body parama** — uglavnom `patch_X_id` (partial-update po id-u; body = polja za promjenu, ali su array/context/prazno) + nekoliko `post_X` s array/primitive body (`SyncExternalId*`, `Metadata_Order`, `ResendInvitation`). Bot ne može sklopiti body generički. → treba flow ili backend named-field schema.
3. **0 enuma / 26% description** (svih 950) — backend Swagger ne deklarira; LLM nema dozvoljene vrijednosti / hint. Backend strop.

## Honest verdikt
- **Injectability + misclassification = riješeno** (180 čistih context ref; 55 krivih demotano; guard protiv regresije). Najveći "tiho-krivi-poziv" rizik (14 garantirano-loših) zatvoren.
- **T3 (37 multipatch + 196 no-body + 0 enuma) = strukturni/backend strop** — dokumentirano, ne lažirano. Ovi toolovi trebaju flow ili obogaćen Swagger; chat-param ekstrakcija ih ne može pokriti.
- **"Ništa ne propuštamo"** = sad postoji POTPUN popis svih 3938 params po izvoru + svaka rupa imenovana. Popravljeno sve što je extraction-popravljivo; ostalo iskreno označeno kao granica.

Promjene koda: `swagger_parser.py` (guard), `scripts/fix_context_misclassification.py` (NEW), `config/tool_data.json` + `config/processed_tool_registry.json` (55 params demotano svaki), 3 testa ažurirana + 2 nova. **Nije commitano.**
