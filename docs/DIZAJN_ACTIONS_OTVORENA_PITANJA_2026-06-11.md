# Dizajn `/actions` sloja — otvorena pitanja i odluke

## 1. Payload nije prazan — tko puni koji parametar

**Izazov:** akcija `book_vehicle` i dalje treba `DateFrom`, `DateTo`; `report_incident`
treba `CaseType`, `Description`. Netko to mora popuniti.

**Podjela (ključna odluka po akciji):**
| Vrsta parametra | Tko ga puni | Primjer |
|---|---|---|
| **Poslovni, korisnik ga izgovori** | **AI** (ekstrakcija iz teksta) | `DateFrom`, `Description`, `registracija` |
| **Interni/tehnički, mapiranje** | **Business API** | `CaseType:3`, `EntryType:0`, `AssigneeType:1` |
| **Iz konteksta (identitet)** | **bot ili Business API** | `VehicleId`, `PersonId`, `TenantId` |

**Preporuka:** definicija svake akcije (`ai.parameters`) sadrži SAMO prvu skupinu —
čiste poslovne parametre koje korisnik stvarno kaže. Sve interno (kodovi, defaulti,
computed) ostaje u Business API-ju. To je "čist payload" koji AI puni pouzdano.

---

## 2. Dvije razine opisa parametra (tehnički + poslovni) + gdje se sprema

**Izazov (Filipovo zapažanje):** opis parametra ima **dva dijela** — *tehnički*
(format: `date`, `date-time`, regex) i *poslovni* (što znači u kontekstu). Morao si
posebno opisati "registraciju" da AI shvati — iznenađenje jer si očekivao da zna po defaultu.

**Zašto te iznenadilo (i zašto je normalno):** LLM ne mapira pouzdano golo polje
`registration_plate` (bez opisa) na "moja rega" / "ZG-1234-AB". Treba mu i
**semantički opis + primjeri**. To je točno razlog zašto je u M1 Swagger zahtjevu
§3 (`description` po parametru) — nije kozmetika, nego uvjet da AI uopće pogodi polje.

**Struktura koju već imaš** (`tool_data.json` param već ima oba dijela):
```json
"registration_plate": {
  "param_type": "string",              // tehnički
  "format": "",                        // tehnički (date/date-time/…)
  "description": "Registracijska oznaka vozila, npr. ZG-1234-AB",  // poslovni
  "examples": ["ZG-1234-AB", "moja rega"]   // (dodati — jako pomaže)
}
```

**Gdje spremiti za fleksibilnost (Filipovo pitanje):** upravo `tool_data.json` je
taj sloj — `ai` dio (namjera + opis parametara) odvojen od `execution` dijela
(ruta). Mijenja se **bez ijedne izmjene koda** (hot-editable JSON). Za ~30 akcija
postaje malen i ručno održiv. **To je tvoj fleksibilni config — već postoji, samo
se sadržaj mijenja s 950 na 30.**

---

## 3. Šifrarnici / enumi (`CaseType`, različit po tenantu) — NAJTEŽI dio

**Izazov:** kodirane vrijednosti (1/2/3) koje su (a) šifrarnik, (b) **različite po
tenantu**, (c) žive u backendu — a AI ih treba razumjeti ("pukla guma" → točan CaseType).

**Što VEĆ imaš:** `services/v2/type_resolver.py` **već rješava točno ovo** za
`*TypeId` parametre — dohvati tenantov `/…Types` šifrarnik u runtime-u i uparuje
korisnikov tekst → id. To je CaseType problem, već riješen za današnje granularne alate.

**Tri opcije:**
| Opcija | Kako | Mana |
|---|---|---|
| A. Statični enum u schemi | upiši `enum:[1,2,3]` u akciju | **puca** — različito po tenantu |
| B. Dinamički resolve u botu | `type_resolver` dohvati šifrarnik po tenantu, upari | bot mora znati koji endpoint = šifrarnik |
| C. **Business API resolve** | AI šalje **stabilnu semantičku vrijednost** ("flat_tire" ili slobodan tekst), Business API mapira u tenantov `CaseType` | traži da backend primi semantiku |

**Preporuka:** za nove `/actions` preferiraj **C** — backend posjeduje tenant
mapiranje, AI nikad ne dira brojčane kodove. Gdje AI mora ponuditi izbor korisniku,
koristi **B** (`type_resolver` pattern koji već radi). **Nikad A.**

---

## 4. Resolve identifikatora (registracija → VehicleId)

bot može iz **registracije** dohvatiti
**VehicleId** i poslati UUID akciji, umjesto registracije koju je AI prepoznao.

**Načelo:** "AI nikad ne tipka UUID; bot/backend ga razriješi." (To je doslovno
princip iz `FIRST.MD`.) Danas `identity.py` razrješuje **vozačev VLASTITI**
vehicle_id; za proizvoljnu registraciju treba lookup (vozilo po tablici).

**Opcije:** bot pre-resolve (tablica → VehicleId, pošalje UUID — daje ti validaciju
prije slanja) **ili** Business API resolve (primi tablicu, razriješi interno — kao
što je `report_incident` primjer radio). **Preporuka:** u World A backend-resolve
je čišći (jedno mjesto); bot-resolve ako želiš rano validirati "postoji li vozilo".

---

## 5. Kontrolna petlja bota: akcija / dodatno pitanje / odgovor

**Izazov:** bot mora reagirati na AI zaključak — zvati akciju, postaviti dodatno
pitanje, ili samo odgovoriti.

**Što VEĆ imaš:** to je postojeći dispatch ugovor. AI output → jedno od:
- **tool_call** → izvrši (kroz mutation gate za write),
- **nedostaje parametar** → `pending_params` pita i **zapamti** kroz turnove,
- **direktan odgovor** → formatter.

U `/actions` svijetu je **isti** 3-way, samo nad ~30 akcija umjesto 950. Ne treba novo.

---

## 6. Validacija AI outputa prije izvršenja

**Izazov:** bot može validirati je li AI dao adekvatnu uputu (validacija input JSON, validacija akcije).

**Što VEĆ imaš (djelomično):** OpenAI tool-calling validira protiv sheme;
`_coerce_llm_params` normalizira tipove (HR zarez, datumi); missing-required check.

**Preporuka (dodati eksplicitni gate prije executora):**
1. je li `action_name` u popisu ~30 akcija? (anti-halucinacija)
2. odgovaraju li parametri shemi? (tip, `format`, `required`)
3. tek onda → executor → `/actions/*`.
Jeftino osiguranje protiv malformiranog poziva; savršeno se uklapa prije L7.

---

## 7. Kontekst razgovora — što bot RADI danas (odgovor na tvoje pitanje)

**Ne šalje cijeli history, ni samo zadnju poruku — vodi ograničen kontekst:**
- sprema **zadnjih 5 turnova** po broju (`conversation_history.py:23`, `DEFAULT_MAX_TURNS=5`),
- **30 min sliding TTL** (`:22`), PII-scrubbano prije spremanja (`engine.py:224`),
- routeru se šalje **zadnja 3 turna** (`llm_router.py`, `conversation_history[-3:]`).

Dakle rolling-window kontekst, podesiv (`max_turns`/`ttl`). Ostaje isto u `/actions` svijetu.

---

## 8. Knowledge / RAG odgovori (faza 2)

**Izazov:** AI odgovara iz **priložene dokumentacije** (car policy, upute za putne
naloge, pravila korištenja vozila) — read-only Q&A, ne akcija.

**Što VEĆ imaš:** `services/rag_scheduler.py` (+ test) = scaffolding za RAG.

**Dizajn:** ovo je **zasebna klasa sposobnosti** uz akcije. Router dobije još jednu
"akciju" tipa `answer_from_policy` → umjesto `/actions/*` ide RAG pretraga nad
tenantovim dokumentima → formatter. Čisto se uklapa: ista petlja (§5), samo je
"izvršni sloj" RAG umjesto Business API. **Faza 2 — točno kako si predvidio.**

---

## Sažetak: što VEĆ imaš vs što je nova odluka

| Filipov komentar | Status |
|---|---|
| Payload i dalje ima parametre | ✅ ispravka mog pojednostavljenja — AI puni poslovne, backend interne |
| Dvije razine opisa (tehnički+poslovni) | ✅ `tool_data.json` param već ima oba; dodati `examples` |
| Gdje spremiti config fleksibilno | ✅ `tool_data.json` (ai/execution split) — već to |
| Šifrarnici per-tenant (CaseType) | 🟡 `type_resolver.py` već radi (opcija B); preporuka C (backend) |
| Registracija → VehicleId | 🟡 `identity.py` radi za vlastito vozilo; za tuđe treba lookup |
| Akcija/pitanje/odgovor petlja | ✅ postojeći dispatch (router/pending_params/formatter) |
| Validacija AI outputa | 🟡 djelomično (coerce+schema); dodati eksplicitni pre-execute gate |
| Kontekst razgovora | ✅ 5 turnova / 30 min / zadnja 3 routeru |
| Knowledge/RAG (faza 2) | 🟡 `rag_scheduler.py` scaffolding; zasebna sposobnost |

**Poanta:** većina tvojih briga je **već (djelomično) riješena u tvom kodu** —
`/actions` migracija ne baca to, nego dio **re-homes** (šifrarnici idealno → backend)
a dio **zadržava** (kontekst, validacija, param collection). Za Damira: najveća
prava odluka je **§3 (tko drži šifrarnike)** i **§1 (granica AI-puni vs backend-puni)**.
