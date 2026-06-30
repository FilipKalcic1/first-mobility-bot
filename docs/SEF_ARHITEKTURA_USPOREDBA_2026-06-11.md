# Arhitektonska Usporedba i Plan Konvergencije: Fleet AI Copilot vs. Trenutni Sustav

**Dokument pripremio:** Filip

**Datum:** 11. lipnja 2026.

**Status:** Interni briefing i tehnička analiza za sastanak s Damirom

---

## TL;DR (Sažetak za brzi pregled)

Damirov prijedlog nove arhitekture **nije kritika našeg dosadašnjeg rada** — to je konceptualno **ista arhitektura** koju smo već postavili, uz dodatak jednog kritičnog sloja koji nam trenutno nedostaje: **`/actions` Business API**.

Ovaj sloj koji nedostaje izravan je razlog zašto nam je točnost usmjeravanja (routing accuracy) trenutno zaglavljena na **~35% na cijelom katalogu**. Danas naš LLM mora birati između **950 granularnih Swagger operacija** s izraženim "sibling" kolizijama (`delete_X` vs `delete_X_id`, `PUT` vs `PATCH`). U Damirovom modelu, LLM bi birao između **~30 čistih, poslovno oblikovanih akcija** (`book_vehicle`, `add_mileage`, `report_incident`). Sibling kolizije — koji su naš izmjereni izvor greške broj jedan (15/24 promašaja u live testu) — **strukturno nestaju** kada se usmjeravanje prebaci na poslovne akcije umjesto na granularne CRUD-ove.

**Glavna poruka za sastanak:** Slažem se sa smjerom; 80% ovoga je već ugrađeno u sustav. Najveća vrijednost predloženog dijagrama je `/actions` sloj jer rješava naš glavni problem s točnošću. Otvoreno pitanje za sastanak je: **tko tehnički gradi `/actions` sloj i kojih je prvih ~15 akcija?** 

---

## 1. Temeljna razlika: "AI Backend" vs "Business API"

Česta je zabuna jer su oba sloja tehnički "backend"  — međutim, oni obavljaju dva potpuno različita zadatka u sustavu:

| Značajka | **AI Backend** (naš bot, `V2Engine`) | **Business API** (`/actions` sloj) |
| --- | --- | --- |
| **Glavni posao** | Pretvara **JEZIK $\leftrightarrow$ POZIV** 

 | Poznaje **POSLOVNA PRAVILA voznog parka** 

 |
| **Ulazni podatak** | "Rezerviraj mi auto sutra 9-15" 

 | <br>`book_vehicle{from, to, driver}` 

 |
| **Što točno radi** | Razumije prirodni jezik, izvlači parametre, pojašnjava nejasnoće s korisnikom, provodi potvrdu (Da/Ne) i formatira JSON natrag u jezik.

 | Provjerava dostupnost vozila, provjerava ovlasti vozača, zabranjuje duple rezervacije i upisuje podatke u kalendar s točnim poljima.

 |
| **Što NE zna** | Ne zna ništa o poslovnim pravilima voznog parka.

 | Ne zna ništa o jeziku, WhatsAppu ili tijeku razgovora.

 |
| **Vlasnik sloja** | <br>**Mi (Filip / AI tim)** 

 | <br>**MobilityOne (Damirova strana / Backend tim)** 

 |

> **Lakmus test za razdvajanje odgovornosti:**
> * Ako MobilityOne promijeni poslovno pravilo (npr. "vozači ne smiju rezervirati vozila vikendom") — ta promjena mora živjeti isključivo u **Business API-ju**. Bot o tom pravilu ne bi trebao znati ništa.
> * Ako dodajemo novi jezik ili komunikacijski kanal (npr. Viber) — ta izmjena živi isključivo u **AI Backendu**.
> 
> 

### Restoran analogija slojeva:

* 
**Channel:** Vrata restorana ili telefonska linija kroz koju gost naručuje.


* 
**AI Backend (Konobar):** Razumije jezik gosta, zapisuje narudžbu, pita ako nešto fali, ali sam ne kuha.


* 
**Business API (Kuhar):** Zna točne recepte, sastojke i poslovna pravila pripreme.


* 
**Domain API (Špajza / 950 Swagger operacija):** Sirovi sastojci bez ikakvog autonomnog znanja o kuhanju.



Danas nemamo sloj kuhara. Naš konobar (bot) trči direktno u špajzu, čita 950 sitnih etiketa i pokušava sam skuhati jelo. Zato dolazi do grešaka. Uvođenje `/actions` sloja zapravo znači zapošljavanje kuhara koji orkestrira sastojke.

---

## 2. Dokaz koncepta: Arhitektura je već potvrđena u kodu

Logika koju Damir postavlja u Business API — provjera dostupnosti, poznavanje granularnih endpointa i točno mapiranje polja — **već je djelomično implementirana i radi u našem botu**, specifično za naše **3 glavne radnje** (booking / mileage / case) unutar datoteke `services/v2/flow_engine.py`.

Naš trenutni `BOOKING_FLOW` izravno replicira Damirov zamišljeni `/actions/book_vehicle` proces:

```
ASK_PERIOD → EXEC_LOOKUP get_AvailableVehicles (Provjera dostupnosti auta)
           → ASK_CHOICE → ASK_CONFIRM (Safety gate)
           → final_tool = POST /VehicleCalendar

```

Sve "glupe" tehničke parametre (`EntryType`, `AssigneeType`) bot već autonomno upisuje kroz strukturu `_booking_params` (`flow_engine.py:688`):

```python
"AssigneeType": 1,
"EntryType": 0,
"FromTime": ..., "ToTime": ..., "VehicleId": ...

```

Trenutno imamo točno 3 takva hardkodirana tijeka: `FLOWS = {booking, mileage, case}` (`flow_engine.py:813`). Za te tri radnje, bot već uspješno simulira Business API.

### Razlika između trenutnog stanja i cilja:

Arhitektonski smjer je pogođen, no razlika je u **lokaciji logike, vlasništvu i pokrivenosti kataloga**:

* 
**Gdje logika živi:** Danas je zaključana unutar bota, a cilj je da postane zaseban, shared servis.


* 
**Ponovna upotrebivost (Reuse):** Trenutno je može koristiti samo bot. Premještanjem u `/actions`, istu logiku istovremeno mogu pozivati i bot, i web Query Builder, i Copilot (ispunjavanje principa *"UI i AI koriste isti API"*).


* 
**Pokrivenost kataloga:** Imamo rješenje za 3 hardkodirane radnje, dok za ostalih ~947 operacija bot mora gađati bazu direktno (što uzrokuje pad točnosti na 35%). Cilj je proširiti ovaj sloj na ~30 čistih krovnih akcija.



---

## 3. Detaljno mapiranje: Damirovih 7 slojeva vs. Naša stvarnost

| # | Damirov predloženi sloj | Što stvarno imamo u kodu | Status i uvid iz koda |
| --- | --- | --- | --- |
| **1** | <br>**Channels** (WA, Viber, Web, M365) 

 | Trenutno je ožičen isključivo WhatsApp (Infobip $\rightarrow$ `webhook_simple.py` $\rightarrow$ `worker.py`).

 | ⚠️ **Djelomično realizirano** (1 od 4 kanala). Backend je postavljen agnostično, ali integracije na rubovima nedostaju.

 |
| **2** | <br>**AI Backend** (Thin Integration Layer bez biznis logike) 

 | <br>`V2Engine` — trenutno **nije** thin sloj. Osim identiteta i povijesti, obavlja tešku orkestraciju jer nema vanjskog API-ja kojem bi delegirao posao.

 | 🔴 **Divergira.** Backend je "debeo" upravo zato što Business API sloj ne postoji.

 |
| **3** | <br>**OpenAI** (Donošenje odluka + formatiranje) 

 | Azure `gpt-4o-mini`: L3 routing + L8 formatting + L2a intent.

 | 🟢 **Match u duhu.** Razlika je što mi LLM-u namjerno ne dopuštamo auto-execute; imamo confirm gate (Da/Ne), što je konzervativnije i sigurnije.

 |
| **4** | <br>**Tool Config** (AI + Execution specifikacija) 

 | <br>`tool_data.json` (AI metapodaci) + `processed_tool_registry.json` (Tehnički parametri rutanja derivirani iz Swaggera).

 | 🟢 **Potpuni Match.** Već imamo implementiran točno taj razdvojeni ai/execution split.

 |
| **5** | <br>**MCP** (Execution + Auth apstrakcija) 

 | <br>`executor.py` + `api_gateway.py` + `token_manager.py` (Zaduženi za OAuth, mapiranje, tenant headere i idempotenciju).

 | 1. **Slika A — WhatsApp/Viber (Naš bot je mozak):** MCP kao protokol nam tehnički ne treba jer naš bot izravno okida akcije prema Business API-ju.

 |

2. 
**Slika B — M365 Copilot (Microsoftov mozak):** Microsoftov LLM donosi odluke i treba standardizirani MCP server kako bi dohvatio naše funkcije. Naš bot je tu zaobiđen.



**Zaključak:** MCP je potreban isključivo kao most kada tuđi AI (M365 Copilot) želi konzumirati naše funkcije. Gradi se tek nakon što uspostavimo funkcionalni `/actions` sloj.

---

## 5. Multichannel i Viber Workstream

Uvođenje Viber kanala je **visoko isplativ i tehnički lagan korak** jer naš core mozak (`V2Engine`) nema nikakvu svijest o kanalu komunikacije. Pregledom koda potvrđeno je da `stream_data` prenosi samo `sender/text/message_id/tenant_id` , dok je izlaz trenutačno vezan na `_send_whatsapp` unutar `worker.py`.

Budući da koristimo Infobip (koji nativno podržava multichannel: WA, Viber, SMS kroz isti API), integracija zahtijeva minimalne izmjene:

1. 
**Inbound:** Dodati ključ `"channel"` u `stream_data` unutar `webhook_simple.py` (unutar tekstualne i netekstualne grane).


2. 
**Outbound:** U `worker.py` granati funkciju `_enqueue_outbound` na temelju `channel` oznake (ako je `viber` $\rightarrow$ okida se Viber klijent, inače postojeći WhatsApp).



---

## 6. Plan postupne konvergencije (Staged Convergence)

Kako bi sustav ostao operativan i upotrebljiv u svakom trenutku, migraciju izvodimo kroz faze, bez radikalnih prekida trenutnog rada:

* 
**Faza 0 (Trenutno stanje):** WhatsApp bot stabilno pokriva ~20 high-frequency mogućnosti kroz `flow_engine.py` (točnost unutar tih tijekova je već na ~90%).


* 
**Faza 1 (Glavni skok):** Definiranje prvih ~15 akcija na `/actions` sloju. Bot umjesto 950 granularnih Swagger operacija dobiva tih 15 čistih akcija kao alate. **Točnost rutanja odmah skače na ciljanu razinu**, a safety gateovi ostaju unutar bota.


* 
**Faza 2:** Dodavanje Viber kanala kroz minimalne modifikacije na rubovima sustava (Infobip adapter).


* 
**Faza 3:** Omotavanje gotovog `/actions` sloja u literalni MCP server kako bi se otvorio kanal prema M365 Copilotu.


* 
**Faza 4:** Implementacija Web kanala i per-user autentifikacije gdje kanali to tehnološki dopuštaju.



---

## 7. Otvorena pitanja za sastanak

Za uspješnu realizaciju projekta, na sastanku moramo definirati odgovore na sljedećih 6 točaka:

1. 
**Vlasništvo nad izgradnjom `/actions` sloja:** Hoće li ga graditi MobilityOne backend tim (što je arhitektonski najčišće jer sprječava dupliranje logike) ili ja trebam podići privremeni adapter/BFF (Backend-for-Frontend) na svojoj strani kako bismo osigurali brzinu razvoja? 


2. 
**Uloga MCP-a:** Smatramo li MCP samo konceptom izvršavanja ili planiramo razvoj doslovnog MCP protokola radi integracije s M365 Copilotom (Slika B)? 


3. 
**Autentifikacijski model na WhatsAppu:** Budući da WA nema OAuth redirect, slaže li se backend tim s našim trenutnim modelom (service principal `client_credentials` + `x-tenant` + identity-scoping preko broja telefona), gdje se autorizacija prava provodi unutar samog Business API-ja nakon što mu proslijedimo identitet? 


4. 
**Opseg prvih 15 akcija:** Predlažem da MVP opseg definiramo prema naših trenutnih 20 najkorištenijih funkcionalnosti kako bismo odmah osjetili dobitak na stabilnosti.


5. 
**Sudbina granularnog Swaggera:** Hoće li nakon uvođenja `/actions` sloja bot i dalje morati imati pristup nekim granularnim rutama za specifične rubne slučajeve ili se vidljivost bota u potpunosti ograničava na ~30 čistih akcija? 


6. 
**Confirm Gate (Read/Write distinkcija):** Budući da se u Query Builderu spominje read-only pristup za dashboard, moramo potvrditi da za bota ostaje aktivno pravilo obaveznog confirm gate-a (ljudska potvrda prije bilo kakve mutacije podataka u bazi).
