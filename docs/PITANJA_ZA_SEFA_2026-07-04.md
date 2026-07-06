# Pitanja za šefa (Damir) — sastanak o /actions arhitekturi

*Filip, 2026-07-04. Redoslijed = redoslijed pitanja na sastanku. Svako pitanje:
doslovna formulacija → što zapravo pitam → što ovisi o odgovoru.*

*Izvori: MASTER_BUILD_PROMPT §17/§22/§24 + M1_ZAHTJEV_ADDENDUM. Križno:
svaki gate iz showstopper registra koji ovisi o M1 strani ima ovdje pitanje.*

---

## BLOK A — GLAVNA ODLUKA (pitati PRVO; gate svega ostalog)

### A1. Tko gradi `/actions`? (World A / World B)
> **"Hoće li tvoj tim graditi Business API `/actions` sloj na MobilityOne
> strani? Ako da — kada kreće i kada mogu očekivati prvih ~5 akcija na dev
> okruženju?"**

**Što zapravo pitam:** Gdje živi orkestracija. Za "prijavi kvar" netko mora:
naći vozilo po registraciji → kreirati incident s pravim CaseType kodom →
blokirati kalendar. Pitam živi li to na NJEGOVOM serveru (World A: njegov tim
gradi `/actions/report-incident`) ili u MOM botu (World B: ja orkestriram
granularne pozive).

**Što ovisi:** tko radi 80% posla. World A poštuje njegov princip "UI i AI
dijele isti API" (web QB + Copilot koriste iste akcije). World B je brz start,
ali debeli bot i logika zaključana kod mene.

**Moja pozicija ako kaže "ne znam / kasnije":** hibrid — *"Krećem s 2-3 akcije
na svojoj strani (flow_engine mi je predložak) da odmah dokažemo vrijednost;
migriramo na tvoj `/actions` kako ga isporučuješ."* Siguran odgovor za obje
varijante.

### A2. Prvih 5 akcija + tko piše definicije
> **"Možemo li fiksirati prvih 5: `book_vehicle`, `add_mileage`,
> `report_incident`, `list_trips`, `vehicle_status`? Predajem ti gotovu
> specifikaciju po akciji — schema i contract fixturei za 3 već postoje."**

**Što zapravo pitam:** konkretan start umjesto apstraktne rasprave. Ja dolazim
s gotovim materijalom (schema §11 build prompta, fixturei u `tests/contract/`).

---

## BLOK B — BEZ OVOGA NIŠTA NE RADI (2 minute, fatalno ako visi)

### B1. OAuth scope — pisana potvrda + imena
> **"Trebam pisanu potvrdu da naš client_id dobiva scope za SVE `/actions/*`
> rute PRIJE prvog deploya — i koja su točna IMENA tih scopeova?"**

**Što zapravo pitam:** Token koji moj bot dobije od IdentityServera nosi popis
ovlasti. Ako `/actions` traži scope koji mi NIJE dodijeljen → svaki poziv 403 →
mrtav bot. **Dogodilo nam se živo 30.5.** ("bot nema ovlasti"). Moj kod to sad
hvata na startu (auth_preflight), ali grant daje samo njihova strana. Imena
scopeova idu u `MOBILITY_REQUIRED_SCOPES` pa provjera postaje automatska.

### B2. Host za `/actions`
> **"Hoće li `/actions` biti na istom hostu kao postojeći API ili odvojeno?
> Trebam samo URL."**

**Što zapravo pitam:** kod mene 1 env varijabla (`BUSINESS_API_URL`) — ali
vrijednost moram znati, inače svaki poziv ide na krivi host.

---

## BLOK C — TIHE KORUPCIJE PODATAKA (najjeftinija pitanja, najskuplja šutnja)

### C1. Idempotency-Key
> **"Na svaku mutaciju šaljemo `Idempotency-Key` header, stabilan kroz retry.
> Deduplicira li backend po njemu — i koliko dugo pamti ključ?"**

**Što zapravo pitam:** Bot pošalje rezervaciju; mreža pukne NAKON što je backend
upisao, PRIJE nego je odgovor stigao; bot ponovi poziv. Bez backend dedupa →
**dupla rezervacija / dupli unos km**, i nitko ne primijeti. Jedna rečenica
odgovora ("da, X min" / "ne") mijenja moj dizajn zaštite.
*Dodaj: "Imamo spreman probe (`scripts/probe_idempotency.py`) — možemo i sami
izmjeriti čim dobijem dev access."*

### C2. Vremenske zone
> **"Kad pošaljem `2026-07-05T09:00:00` bez offseta — tretirate li to kao
> Europe/Zagreb ili UTC? Ili da uvijek šaljem s offsetom (`+02:00`)?"**

**Što zapravo pitam:** kriva pretpostavka = "sutra u 9" postane 10 ili 11
(ljetno/zimsko). Najgora vrsta greške: **sve radi, ali krivo** — vidi se tek
kad korisnik dođe po auto u krivi sat.

---

## BLOK D — UGOVOR PO AKCIJI (nije pitanje nego PREDAJA — isprintaj/pošalji)

> **"Za svaku akciju koju gradite, ovo je checklist od 8 stvari koje mi
> trebaju prije nego počnete:"**

1. ruta + input DTO (čista poslovna polja);
2. **strukturiran error `{error_code, field?, message}` za SVE 4xx** — bot
   greške prevodi korisniku na hrvatski; sa stabilnim kodom prijevod je
   precizan ("Nedostaje ti datum"), bez njega generičan ("Tehnički problem");
3. **backend mapira šifrarnike** — ja šaljem semantiku ("kvar"), vi mapirate u
   SVOJ tenant-kod (CaseType 3 kod tenanta A, 2 kod tenanta B) — vi posjedujete
   šifrarnik;
4. Idempotency-Key honoriranje (= C1);
5. **liste vraćaju `{items, total}`** + max page size — bez `total` ne mogu
   reći "imaš 27 putovanja, prikazujem 10";
6. objavljeni rate-limiti (429 + `Retry-After`) — moj backoff je danas
   kalibriran naslijepo;
7. scope grant (= B1);
8. timezone semantika (= C2).

---

## BLOK E — POSTOJEĆI (granularni) API — može i mailom

- **Envelope:** "Je li envelope list-odgovora uniforman (koji ključ drži
  redove?) i gdje je ukupan broj zapisa?" — danas pogađamo između 7 varijanti.
- **Format telefona u /Persons:** "Isti tenant drži tri formata (385…, +385…,
  0…) — normalizacija na vašoj strani, ili potvrda da je naš contains-fallback
  ispravan?" — identifikacija korisnika je nulti korak svega.
- **Golden sample responses:** po jedan stvarni (anonimiziran) JSON za top ~20
  endpointa — pinamo formatter na istinu umjesto pretpostavki.
- **Bulk `GET /Tenants` scope:** smije li naš client listati sve tenante?
  (nije blokator — lazy otkrivanje radi bez toga; samo ubrzanje).

## BLOK F — BUDUĆNOST (30 sekundi)

- **Copilot identitet:** "Je li `Email` polje u /Persons filterabilno?" —
  M365 Copilot kanal identificira usera emailom umjesto telefonom.
- **Info njemu (ne pitanje):** Viber kod je gotov i testiran; čeka se samo
  registracija Viber sendera kod Infobipa (naša ops stavka).

---

## Kako voditi sastanak

1. **A1 prvo** — sve ovisi o njemu. Ne izlazi bez odgovora ILI roka za odgovor.
2. **B1+B2 odmah iza** — njemu trivijalno, nama fatalno ako visi.
3. **C1+C2** — naglasi "tihe korupcije": najjeftinije pitanje, najskuplja šutnja.
4. **D predaj kao dokument**, ne čitaj naglas.
5. **E/F mailom** ako ponestane vremena.
6. **Za svako "ne znam": dogovori TKO i DO KADA odgovara pisano** — nepraćeno
   pitanje = showstopper bez vlasnika.
