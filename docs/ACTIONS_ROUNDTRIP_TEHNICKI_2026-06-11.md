# Tehnički round-trip: kako se 950 funkcija pretvara u 1 akciju

> Tehnički companion uz [SEF_ARHITEKTURA_USPOREDBA_2026-06-11.md](SEF_ARHITEKTURA_USPOREDBA_2026-06-11.md).
> Prati Damirov primjer `report_incident` ("Pukla mi je guma na ZG-1234-AB")
> kroz 4 sloja — ali **korigirano protiv stvarnog koda**, ne protiv pseudokoda.
>
> Provjereno Grepom (ne po sjećanju): `services/v2/executor.py` je **async**
> (`async def execute` na :90, `await self._gateway.call` na :187), **ne** sync
> `requests.post`; auth je `TokenManager.get_token()` + `client_credentials`;
> confirm gate je `mutation_gate.decide_mutation` u `engine.py`.

---

## ⚠️ Prvo: jedna rečenica koja mijenja TKO radi posao (World A vs World B)

U opisu se lako pomiješaju **dvije suprotne slike**. Razlika nije kozmetička —
određuje tko nosi teški dio i tko je blokiran na čemu:

| | **World B** (orkestracija u botu) | **World A** (orkestracija na Business API-ju) |
|---|---|---|
| Odakle dolazi | rečenica *"MI moramo znati params/order/filtere"* | Damirov kod: `# OVAJ KOD ŽIVI NA MOBILITYONE SERVERU` |
| Tko zna `GET vehicle → POST incident CaseType:3 → PUT calendar` | **bot** (kao `flow_engine.py` danas) | **Damirov tim** |
| Što bot zna | sve — redoslijed, polja, filtere | **samo** `report_incident{registration_plate, description}` |
| Tko nosi "filteri, nije sve definirano" | **ti** (i blokiran si na M1 Swagger metapodacima) | **Damir** (njegov tim posjeduje API) |
| Poštuje Damirov princip "bez duplikacije, UI+AI dijele API"? | ❌ ne (web QB ne može pozvati botovu logiku) | ✅ da |

**Damirov dijagram crta World A.** Tvoja intuicija vuče na World B jer si **već
sagradio World B za 3 akcije** (`flow_engine.py` — vidi usporedni doc). To je
**#1 pitanje za ponedjeljak**: *"Je li `/actions` na vašem serveru (vi gradite
orkestraciju), ili je to adapter na mojoj strani?"* Odgovor flipa tko radi 80%
posla. Ostatak ovog dokumenta pretpostavlja **World A** (Damirov dijagram).

> **Skrivena dobra vijest za tebe:** u World A tvoja briga *"moramo točno znati
> koje parametre, koji redoslijed, filteri nisu uvijek definirani"* **nestaje** —
> postaje Damirov problem, jer njegov tim zna svoj API. Bot je "zaštićen od kaosa".

---

## Round-trip `report_incident` kroz 4 sloja (mapirano na stvarni kod)

### Korak 1 — Edge (`webhook_simple.py` + `worker.py`) ✅ walkthrough TOČAN

Ulaz i izlaz po kanalu. Ovaj dio Damirovog primjera odgovara stvarnosti — i to
je **pravi multichannel posao** koji ionako moramo napraviti:

- **Inbound** (`webhook_simple.py`, `stream_data` na ~:514 i :546): dodati
  `"channel": "whatsapp"|"viber"`. Danas tog polja nema (potvrđeno).
- **Outbound** (`worker.py`, `_enqueue_outbound`/`_send_whatsapp` ~:1296):
  granati po `channel` tagu (`viber` → Viber send, inače postojeći WA send).
- Viber ide kroz **isti Infobip** → mozak (`V2Engine`) se ne dira.

### Korak 2 — Tool Config (`config/tool_data.json`)

OpenAI više ne vidi stotine Swagger ruta, nego čistu akciju. Forma se **poklapa
s onim što već imaš** (`ai` dio + `execution` dio); samo se mijenja sadržaj:
umjesto 950 granularnih → ~30 akcija. Definicija `report_incident`:
`ai.parameters = {registration_plate, description}`, `execution.action =
"POST /actions/report-incident"`.

### Korak 3 — Executor (`services/v2/executor.py`) ⚠️ ISPRAVAK pseudokoda

Damirov primjer ovdje ima 3 stvari koje **NE odgovaraju tvom sustavu** (bitno da
ne ponoviš krivo pred njim):

| Pseudokod u primjeru | Tvoja stvarnost |
|---|---|
| `services/executor.py` | **`services/v2/executor.py`** |
| `import requests` + `requests.post(...)` (sync) | **async**: `await asyncio.wait_for(self._gateway.call(...))` (`executor.py:187`) — sync `requests` bi blokirao event-loop |
| `get_client_credentials_token()` | `TokenManager.get_token()` (`token_manager.py`, grant_type `client_credentials`) |
| hardkodiran `https://api.mobilityone.com/...` | URL se gradi iz configa + `MOBILITY_API_URL`, uz SSRF guard, `x-tenant`, **`Idempotency-Key`** |

**I — najvažniji ispravak — bot NIJE glupi pass-through.** Primjer prikazuje
`requests.post(url, json=tool_input)` (proslijedi ravno). Ali `report_incident`
je **WRITE** akcija, pa PRIJE poziva mora proći tvoj postojeći sloj:

```
... param-ask (ako fali registration_plate → pitaj korisnika)
... mutation_gate.decide_mutation(method=POST)  → CONFIRM
... "Prijavit ću kvar na ZG-1234-AB: 'pukla guma'. Potvrđuješ? (Da/Ne)"
... tek na "Da" → executor.execute → /actions/report-incident
```

Akcija-sloj makne **routing-preko-950** i **orkestraciju**, ali **NE makne tvoj
param-ask + Da/Ne confirm**. Ako ih makneš, vraćaš se na "LLM auto-execute" koji
si namjerno izbjegao (izmjereno: LLM fabricira parametre). Damirov dijagram
(OpenAI → execute, bez confirma) tu pojednostavljuje — confirm gate ostaje tvoj.

### Korak 4 — Business API `/actions` (na MobilityOne serveru — Damirov posao)

Ovdje je "teški programerski posao" i u World A je **Damirov**. Akcija
`/actions/report-incident` interno orkestrira granularne Domain API pozive:

```
1. GET   /Vehicles            → nađi vehicle_id po registraciji
2. POST  /Incidents/Create    → {VehicleId, CaseType: 3, Description, Status}
3. PUT   /VehicleCalendar/... → blokiraj kalendar tom vozilu
→ vrati botu čisti sažetak {status, incident_id, vehicle_status}
```

Domain API-ji su tu **funkcije unutar Business API-ja** — građevni materijal pod
haubom. Tvoj bot ne zna za `CaseType: 3` ni redoslijed; vidi samo rezultat.

---

## Tvoje pitanje: "zašto vlasnik ne napiše bolji Swagger?"

Odličan instinkt — ali bolji Swagger i akcija-sloj rješavaju **različite probleme**:

| | Bolji Swagger | Akcija-sloj (`/actions`) |
|---|---|---|
| Što rješava | bot lakše **NAĐE** 1 od 950 | bot više ne traži među 950 — bira 1 od ~30 |
| Što ne može | ne spaja više poziva u radnju | — |
| Ključno | dokumentira **pojedinačne** endpointe | **SPAJA** endpointe s pravilima + redoslijedom |

Swagger fizički **ne može** izraziti "da prijaviš kvar treba ova 3 poziva tim
redom + blokada kalendara" — to je poslovna logika, ne opis endpointa. Zato je
akcija-sloj **bolje rješenje od boljeg Swaggera**: premješta kompleksnost 950 na
ljude koji ih **posjeduju i razumiju** (MobilityOne dev), umjesto da bot
(autsajder) to reverse-enginira iz dokumentacije.

**Posljedica za tvoje M1 Swagger zahtjeve:** u World A dio njih postaje **manje
bitan** — ako bot ne ruta po 950 nego po ~30 akcija, sibling-distinkcija
(`delete_X` vs `delete_X_id`), enumi na granularnim poljima i body-scheme za
granularne PATCH-eve više nisu na kritičnom putu bota (Damirov Business API to
zna interno). Ostaje bitno: **definicija ~30 akcija** + auth/test access. Ovo
otvori na sastanku (pitanje #5 u usporednom docu) da ne tražiš stvari koje
`/actions` čini suvišnima.

---

## Rezime (što ponijeti na sastanak)

1. **Potvrdi World A vs World B** — tko gradi orkestraciju? Damirov kod kaže on;
   potvrdi da je tako, jer to flipa tko radi 80% posla.
2. **Bot zadržava param-ask + Da/Ne confirm** i u novom sustavu (write akcije).
3. **Već imaš World-B dokaz** za 3 akcije (`flow_engine.py`) — predložak za
   prvih nekoliko `/actions`, ne bačen rad.
4. **Pre-čisti M1 zahtjeve** — u World A neki postaju suvišni; fokus na
   definiciju ~30 akcija + dev/test access.
