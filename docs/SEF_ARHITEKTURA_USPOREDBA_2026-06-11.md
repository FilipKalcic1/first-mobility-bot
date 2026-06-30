# Damirova "Fleet AI Copilot" arhitektura vs. naš sustav — usporedba i plan

> Interni briefing (Filip, 2026-06-11) za razgovor s Damirom o dokumentu
> "Fleet AI Assistant – Architecture Document (Final Clean Version)".
>
> Sve tvrdnje o trenutnom stanju provjerene su protiv koda, ne po sjećanju:
> executor gradi granularne path-ove direktno (`POST /AddMileage`); nema
> `/actions` sloja (`grep -rn "/actions/" services/` → prazno); nema MCP servera
> (`grep -rni "mcp" services/ *.py` → prazno); auth je jedan service principal
> (`client_credentials` u `token_manager.py:165`) + `x-tenant` header.

---

## TL;DR (ako pročitaš samo ovo)

Damirova arhitektura **nije kritika** našeg rada — to je **ista** arhitektura
koju smo već sagradili, plus **jedan sloj koji ne postoji**: `/actions` Business
API. I taj sloj koji fali je **točno** razlog zašto nam je routing accuracy
zaglavljen : danas LLM bira između **950 granularnih
Swagger operacija** sa siblinzima (`delete_X` vs `delete_X_id`, PUT vs PATCH),
a u Damirovom modelu bi birao između **~30 čistih, intent-oblikovanih akcija**
(`book_vehicle`, `add_mileage`, `report_incident`). Sibling kolizije — naš #1
izmjereni izvor greške (15/24 promašaja u live testu 2026-05-30) — **strukturno
nestaju** kad rutaš po business akcijama umjesto po granularnim CRUD-ovima.

**Glavna poruka za sastanak:** *"Slažem se sa smjerom; 80% je već u sustavu.
Najveća vrijednost dijagrama je `/actions` sloj — on rješava naš #1 accuracy
problem. Otvoreno pitanje za nas: tko gradi `/actions` i kojih je prvih ~15?"*

---

## 1. Najvažnija razlika: "AI Backend" vs "Business API"

Ovo se najčešće miješa jer su **oba "backend"** — ali rade dva potpuno
različita posla:

| | **AI Backend** (= naš bot, `V2Engine`) | **Business API** (`/actions`) |
|---|---|---|
| Posao | pretvara **JEZIK ↔ POZIV** | zna **PRAVILA voznog parka** |
| Ulaz | "rezerviraj mi auto sutra 9-15" | `book_vehicle{from, to, driver}` |
| Što radi | razumije, izvuče parametre, pita ako fali, potvrdi (Da/Ne), formatira JSON natrag u hrvatski | provjeri dostupnost, provjeri ovlasti vozača, zabrani dupli booking, upiši u VehicleCalendar s točnim poljima, vrati rezultat |
| **NE zna** | ništa o pravilima voznog parka | ništa o jeziku, WhatsAppu, razgovoru |
| Vlasnik | **mi (Filip)** | **MobilityOne** (Damirova strana) |

**Lakmus test koji odmah razdvoji slojeve:**
- *"Da MobilityOne promijeni poslovno pravilo (npr. vozači ne smiju rezervirati
  vikendom) — gdje ta promjena živi?"* → **Business API.** Bot ne bi ni trebao
  znati da pravilo postoji.
- *"Da dodamo novi jezik ili kanal — gdje to živi?"* → **AI Backend.**

**Restoran analogija:**
- **Channel** = vrata / telefonska linija kroz koju gost naručuje
- **AI Backend** = konobar koji razumije tvoj jezik i zapiše narudžbu — ne kuha
- **Business API** = kuhar koji zna recepte i pravila
- **Domain API** (950 Swagger ops) = špajza — sirovi sastojci bez ikakvog znanja

Danas **nemamo kuhara**. Konobar (bot) trči u špajzu, čita 950 etiketa i pokušava
sam skuhati. Zato griješi. `/actions` sloj = zaposliti kuhara.

**Worked example — "rezerviraj mi auto za sutra 9-15":**

```
DANAS (bez /actions, bot nosi sve):
  WhatsApp → bot razumije "booking, sutra 9-15"
           → bot SAM bira: koji od 950? get_AvailableVehicles? post_VehicleCalendar?
           → bot SAM gradi payload (AssigneeType=?, EntryType=?, VehicleId iz konteksta…)
           → bot SAM orkestrira: prvo lookup slobodnih, pa ASK_CHOICE, pa upis
           → API vrati JSON → bot formatira hrvatski
  ⤷ 5 mjesta gdje može pogriješiti, sva u botu.

CILJ (s /actions, kuhar nosi pravila):
  WhatsApp → bot razumije "booking, sutra 9-15" → POZOVE POST /actions/book-vehicle{from,to,driver}
           → Business API: provjeri dostupnost + ovlasti + ne-dupli-booking + upiši + notify
           → vrati {ok, vehicle, slot} → bot formatira hrvatski
  ⤷ bot zna SAMO jezik; pravila su u jednom endpointu koji i web UI zove.
```

**Zašto ih uopće razdvojiti?** Jer i **web Query Builder** treba "rezerviraj auto
s istim pravilima". Ako pravila žive u botu — web UI ih ne može iskoristiti
(duplikacija). Ako žive u Business API-ju — i bot i web zovu isti
`POST /actions/book-vehicle`. To je Damirov princip *"UI i AI koriste isti API"*.

---

## ⭐ "Ali to zvuči identično našem sustavu" — da, jer si VEĆ sagradio kutiju 3

Prirodna reakcija na Damirov dijagram je: *"pa to je ono što već imamo."* I to je
**90% točno** — ne zabuna, nego pravi uvid. Razlog: za naše **3 glavne radnje
(booking / mileage / case)** orkestraciju koju Damir stavlja u Business API
**već smo implementirali i radi** — u `services/v2/flow_engine.py`.

Pogledaj `BOOKING_FLOW` — to je **doslovno** Damirov `/actions/book_vehicle`:

```
ASK_PERIOD → EXEC_LOOKUP get_AvailableVehicles   ← "je li auto slobodan?" (Damirova provjera!)
           → ASK_CHOICE → ASK_CONFIRM
           → final_tool = POST /VehicleCalendar
```

A "glupe parametre" koje je Damir naveo (`EntryType`, `AssigneeType`) bot **već zna
sam upisati** (`_booking_params`, `flow_engine.py:688`):

```python
"AssigneeType": 1,
"EntryType": 0,
"FromTime": ..., "ToTime": ..., "VehicleId": ...
```

I imamo ih **točno 3**: `FLOWS = {booking, mileage, case}` (`flow_engine.py:813`).

**Dakle:** logika koju Damir stavlja u Business API — provjeri dostupnost, znaj
koji granularni endpoint i s kojim poljima — **već postoji u botu, za 3 akcije.**
Zato dijagram zvuči kao naš sustav: za naše 3 glavne radnje **i jest**.

### Tih 10% razlike = cijeli projekt

Ne mijenja se arhitektura (slažemo se). Mijenja se **gdje logika živi, tko je
vlasnik, i koliko je akcija:**

| | Mi danas (`flow_engine.py`) | Damirov cilj (`/actions`) |
|---|---|---|
| **Gdje živi** | unutar bota | zaseban shared servis |
| **Tko reuse** | samo bot | bot **+ web QB + Copilot** (isti `/actions`) |
| **Koliko akcija** | 3 hardkodirane | ~30 |
| **Ostalih ~947 ops** | bot ruta direktno → **TU je 35% accuracy** | dobiju čist coarse front |

Dva prava dobitka: (1) **reuse** — booking logika je danas zaključana u botu, web
QB je ne može pozvati (krši "UI i AI dijele isti API"); (2) **pokrivenost** —
imamo "kuhara" za 3 radnje, za ostalih ~947 bot pogađa direktno; `/actions`
proširuje kuhara s 3 na ~30.

### Iskrena nijansa (da te ne uhvati nespremnog)

Dio kutije 3 **već radiš** (slijed poziva: dostupnost → odabir → upis). Dio **ne
radiš**: autorizacijska pravila ("ima li vozač dozvolu?") — danas to ne provjeravaš
unaprijed; backend odbije s 403/400 i `api_error_translator` prevede grešku na
hrvatski. Pa Business API zapravo **konsolidira + pred-validira + da ti JEDAN
čist endpoint** umjesto da ti slažeš N granularnih poziva.

### Talking point za sastanak

> *"Slažem se — i zapravo sam ovaj pattern već dokazao: `flow_engine.py` radi
> točno tvoj `/actions/book_vehicle` za naše 3 glavne radnje, uključujući provjeru
> dostupnosti i upis s ispravnim poljima. Projekt je: podignuti tu logiku iz mog
> bota u shared `/actions` servis i proširiti je s 3 na ~30 akcija. Pitanje je
> tko ga gradi i vlasništvo."*

Time pokazuješ da nisi samo razumio dijagram — **već si izgradio i validirao
njegov najteži dio**, i znaš točno što fali da se dovrši.

---

## 2. Mapiranje: Damirovih 7 slojeva → naša stvarnost danas

| # | Damirov sloj | Što stvarno imamo | Status |
|---|---|---|---|
| 1 | **Channels** (WA, Viber, Web, M365) | Samo WhatsApp (Infobip → `webhook_simple.py` → `worker.py`) | ⚠️ 1 od 4 — backend jest channel-agnostičan, ali ožičen samo WA |
| 2 | **AI Backend** (THIN: identity, history, zove OpenAI, exec preko MCP; **NE radi business logiku**) | `V2Engine` — ali **NIJE thin**: radi identity+history **I** tešku orkestraciju (param collection, type/Graph discovery, context injection, mutation gate) | 🔴 **Divergira** — backend je debeo jer nema Business API kojem bi delegirao |
| 3 | **OpenAI** (decision + formatting) | Azure `gpt-4o-mini`: L3 routing + L8 formatting + L2a intent | 🟢 Match u duhu — ali namjerno NE damo LLM-u auto-execute (picker + Da/Ne); **konzervativnije** od dijagrama |
| 4 | **Tool Config** (`ai` dio + `execution` dio) | `tool_data.json` (ai: intent_summary, anchors, use_when) + `processed_tool_registry.json` (execution: method/path/params) | 🟢 **Jak match** — već imamo točno taj ai/execution split, deriviran iz Swaggera |
| 5 | **MCP** (execution + auth abstrakcija) | `executor.py` + `api_gateway.py` + `token_manager.py` (mapiranje, OAuth, tenant header, idempotency) | 🟡 Match u **funkciji**, ne u **protokolu** — direktni HTTP gateway, ne MCP server |
| 6 | **Business API** (`/actions/book-vehicle`, validacija, orkestracija) | **NE POSTOJI.** Bot zove `POST /AddMileage`, `GET /Trips`… direktno | 🔴 **VELIKI GAP** — sloj koji fali = naš accuracy strop |
| 7 | **Domain / Granular API** (CRUD) | 950 MobilityOne Swagger ops | 🟢 Match — to je ono po čemu danas rutamo |

**Kako čitati tablicu:** 4 sloja su 🟢/🟡 (već imamo, u duhu ili funkciji). Sva
tri 🔴 problema imaju **isti korijen** — nedostatak `/actions` sloja: zato je
backend debeo (#2), zato accuracy pati (#6).

---

## 3. Gdje Damir (priznato) pojednostavljuje — 4 točke za sastanak

Damir sam kaže *"možda pojednostavljujem scope"*. Točno na ova 4 mjesta:

1. **`/actions` je nacrtan kao jedna kutija, a zapravo je cijeli program.**
   Dizajn ~30 business akcija s validacijom + orkestracijom granularnih poziva
   je **najveći pojedinačni komad posla u sustavu**. Njegov princip "bez
   duplikacije logike" je TOČAN — baš zato ta logika mora živjeti na **jednom**
   mjestu. Danas je improvizirana u botu jer Business API ne postoji.

2. **Auth model radi za Web/M365, ali NE čisto za WhatsApp.** Njegov
   `user: {email, phone, token}` + "MCP resolve-a per-user auth" pretpostavlja
   per-user token / SSO. **WhatsApp nema OAuth redirect** — jedini identitet je
   broj telefona. Zato danas (ispravno) radimo: jedan service principal
   (`client_credentials`) + `x-tenant` + identity-scoping (phone→Person→TenantId).
   Per-user delegirani token na WA kanalu **fizički ne ide** — tu smo *ispred*
   idealizacije. Za Web/M365 njegov model radi. Realan model za WA: service
   principal + autorizacija (ovlasti) **unutar Business API-ja** na temelju
   razlučenog identiteta.

3. **"OpenAI odlučuje" + njegov flow nema confirm gate.** Sekvenca mu je
   OpenAI → tool call → MCP → execute, bez human-in-the-loop. Naša skupo naučena
   lekcija (izmjereno: LLM fabricira parametre, 35%): PRIJE mutacije MORA ići
   Da/Ne potvrda + clarify za dvosmislene read-ove. **Dobra vijest:** u QB mailu
   je Damir SAM inzistirao na human-in-the-loop — slažemo se; samo eksplicitno
   reći da "AI-first" ne znači maknuti safety gateove.

4. **MCP — protokol ili samo "execution sloj"?** Ako misli doslovni MCP server,
   to je konkretna implementacijska odluka; funkcionalni ekvivalent već imamo.
   Literal MCP ima smisla **jer M365 Copilot konzumira MCP servere** (vidi §4) —
   ali ne mijenja fundamentale i gradi se tek kad postoji `/actions`.

---

## 4. MCP — gdje točno ulazi (dvije slike)

MCP (Model Context Protocol) je **standardni način da nekome ponudiš "evo mojih
akcija" da ih AI može pozvati.** Ključ je tko je "mozak":

**Slika A — WhatsApp / Viber (NAŠ bot je mozak):**
```
User → WhatsApp/Viber → [NAŠ AI Backend = mozak: razumije, odluči, pita, potvrdi]
        → pozove akciju → [Business API /actions] → [Domain API] → odgovor
```
MCP **ne treba** — naš bot direktno zove akcije.

**Slika B — M365 Copilot (MICROSOFTOV mozak, naše akcije preko MCP):**
```
User u Teamsu → [Microsoft Copilot = mozak] → MCP → [naš MCP server → /actions] → [Domain API] → odgovor
```
Ovdje **Copilot misli** (Microsoftov LLM bira i poziva), a naš bot je **zaobiđen** —
Copilot samo treba standardni način da dosegne naše akcije, a to je MCP.

**Zaključak:** MCP je most za *"tuđi AI (Copilot) hoće pozvati naše funkcije"*.
Nije obavezan za WA/Viber. Gradi se TEK kad postoji `/actions` (samo ga omata),
i tada otključava Copilot kanal "besplatno" — isti tool-layer, drugi mozak.

---

## 5. Channels / multichannel — Viber je AKTIVNI workstream (i jeftin je)

Damir lista 4 kanala; mi imamo 1. Viber stiže — i to je **malo posla** jer mozak
(`V2Engine`) se **ne mijenja** (već je channel-agnostičan). Provjereno u kodu:
danas **nema pojma o kanalu** — `stream_data` nosi samo
`sender/text/message_id/tenant_id` (`webhook_simple.py:514` i `:546`), a izlaz je
jedan hardkodiran `_send_whatsapp` (`worker.py:1296`).

**Konkretno što se mijenja za multichannel:**
1. **Inbound:** dodati `"channel"` u `stream_data` na oba mjesta u
   `webhook_simple.py` (text + NON_TEXT grana).
2. **Outbound:** granati `_enqueue_outbound` / `_send_whatsapp` u `worker.py`
   po `channel` tagu (`viber` → Viber sender, `whatsapp` → postojeći).
3. **Viber ide kroz ISTI Infobip** koji već koristimo (Infobip je multichannel:
   WhatsApp, Viber, SMS, RCS kroz isti API) → novi inbound parser + novi send;
   **bot brain netaknut**, formatiranje odgovora isto.

Web i M365 su veći (drukčiji auth + UX) → kasnije faze. **Viber specifično je
adapter na rubu, ne novi bot.**

---

## 6. Staged convergence — kako ostati "upotrebljiv" (Damirova riječ)

| Faza | Što | Dobitak |
|---|---|---|
| **Sada** | WhatsApp bot na ~20 high-frequency capabilitija (driver rutina, već ~90%) | upotrebljivo **danas**; nastavi shippati |
| **Faza 1** | Prvih ~15 `/actions` (tko ih gradi = pitanje za sastanak); bot ih dobije kao "tools" umjesto 950 granularnih | **accuracy skok**, safety gateovi ostaju |
| **Faza 2** | Viber kanal (jeftino, §5) | drugi kanal, isti mozak |
| **Faza 3** | literal MCP server oko `/actions` | otključava **M365 Copilot** kanal |
| **Faza 4** | Web kanal + per-user auth gdje kanal dopušta | puni multichannel |

Bitno: svaka faza je samostalno korisna i ne ruši prethodnu. Ne čeka se "veliki
prepis" — bot već radi, a `/actions` se uvodi inkrementalno (svaka migrirana
akcija = accuracy dobitak).

---

## 7. Otvorena pitanja za Damira (donijeti na sastanak)

1. **Tko gradi `/actions`?** Njegov backend tim (arhitektonski ispravno — poštuje
   "bez duplikacije"), ili ja adapter/BFF na svojoj strani (brže, neovisno o
   njima, ali orkestracija ostaje u botu)? — *Ovo je odluka #1; ne presuđujem
   unaprijed.*
2. **MCP = protokol ili pojam?** Ako protokol — je li driver M365 Copilot
   integracija (Slika B)?
3. **Auth na WhatsApp** — prihvaća li service-principal + identity-scoping model
   (jer per-user token na WA ne ide), s autorizacijom u Business API-ju?
4. **Prvih ~15 akcija** — koje capabilitije smatra MVP-jem? (Mapira se na naših
   ~20 high-frequency — imam prijedlog spreman.)
5. **M1 Swagger zahtjevi** — ostaju li svi relevantni, ili `/actions` sloj dio
   njih čini suvišnima? (Ako Business API interno orkestrira granularno, treba li
   nam i dalje sibling-distinkcija na 950, ili samo na ~30 akcija?)
6. **Read-only vs write** — u QB mailu spominje read-only za dashboard; vrijedi
   li to i za bota, ili bot ostaje read+write (s confirm gateom)?

---

## Dodatak — kako provjeriti tvrdnje iz ovog dokumenta

```bash
grep -rn "/actions/" services/                     # prazno → /actions sloj ne postoji
grep -rni "mcp" services/ *.py                      # prazno → nema MCP servera
grep -n "client_credentials" services/token_manager.py   # single-principal auth
grep -n "channel" webhook_simple.py worker.py       # prazno → danas sve implicitno WhatsApp
```
