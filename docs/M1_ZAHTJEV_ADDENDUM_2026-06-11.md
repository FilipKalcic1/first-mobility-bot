# Addendum zahtjevima M1 timu — 8 stavki koje nisu pokrivene (2026-06-11)

> Nadopuna na 4 poslana dokumenta (params / filter / endpoint-tagging / azure-modeli).
> Ništa od ovog ne mijenja poslane zahtjeve — ovo su rupe koje su ostale, poredane po šteti koju prave.

---

## 1. Ugovor o envelope-u odgovora (gdje su redovi + gdje je ukupan broj)

Bot danas **pogađa** envelope ključ liste na 4 mjesta u kodu, redom probavajući:
`Data, data, Result, Results, Items, items, value`. To radi, ali je pogađanje — i ne znamo
**gdje je ukupan broj zapisa** kad je lista veća od `Rows`.

**Molimo:** (a) potvrdu je li envelope uniforman po svim list-endpointima i koji je ključ,
(b) polje/„header" s ukupnim brojem (`Total`? `X-Total-Count`?). Bez toga korisniku ne možemo
reći „imaš 320 putovanja, prikazujem 100" — ne znamo da ih je 320.

## 2. Paginacija — ugovor, ne samo parametri

Šaljemo `Rows=100` kao default. Pitanja: maksimalni dopušteni `Rows`? Kombinacija
`First`+`Rows` je offset-paginacija — je li stabilna uz umetanje novih zapisa (ili postoji
cursor varijanta)? Default sort kad `Sort` nije poslan?

## 3. Idempotency-Key — honorira li ga backend?

Bot na **svaku mutaciju** šalje `Idempotency-Key` header (UUID, stabilan kroz retry).
Pitanje na koje nikad nismo dobili odgovor: **deduplicira li M1 po tom headeru?**
Ako ne — mrežni timeout nakon uspješnog POST-a + retry = dupla rezervacija/dupli unos km,
i moramo to riješiti drugačije (read-before-write provjere). Jedna rečenica odgovora
("da, honoriramo X sekundi" / "ne") mijenja naš dizajn zaštite.
(Imamo spreman probe: `scripts/probe_idempotency.py` — možemo i sami izmjeriti čim dobijemo dev access.)

## 4. Vremenske zone za datetime vrijednosti

Bot šalje ISO bez offseta (`2026-06-12T09:00:00`), interpretirano kao Europe/Zagreb.
**Tretira li M1 naive datetime kao lokalno ili UTC?** Pogrešna pretpostavka = rezervacije
pomaknute 1-2 sata (ljetno/zimsko). Filter dokument je pitao samo format za `Filter` —
ovo je za sve body/query datetime parametre.

## 5. Strukturiran error-shape za SVE 4xx (ne samo filter)

Filter dokument traži strukturiran 400 za filter greške. Generaliziramo: stabilan JSON oblik
za sve 4xx (`{"error_code": "...", "field": "...", "message": "..."}`). Bot prevodi greške
korisniku na hrvatski LLM-om — sa stabilnim `field`/`error_code` prijevod postaje
deterministički i točniji ("Nedostaje ti X", ne "Tehnički problem").

## 6. Rate limit / kvote API-ja

Koji su limiti po clientu (RPS/RPM)? Koji status + headeri dolaze kod prekoračenja
(`429` + `Retry-After`?)? Bot ima retry s backoffom, ali kalibriran naslijepo.

## 7. Kanonski format telefona u /Persons

Živo smo izmjerili da isti tenant drži tri formata: `385…`, `+385…`, `0…`. Bot zato radi
NSN fallback (`Phone(contains)` + post-verifikacija). Molimo ili (a) normalizaciju na
backend strani / pri unosu, ili (b) potvrdu da je `contains`-pristup ispravan i da nema
endpointa gdje bi vratio krivu osobu. Identifikacija korisnika je nulti korak svega.

## 8. Golden sample responses (top ~20 endpointa)

Po jedan **stvarni JSON response** (anonimiziran) za najčešće endpointe
(MasterData, Persons, Trips, VehicleCalendar, Expenses, MileageReports, AvailableVehicles…).
Time pinamo formatter i ekstrakciju na istinu umjesto na pretpostavke — i služi kao
regression fixture kad mijenjate API.

---

## Prioritet

**3 (idempotency)** i **4 (timezone)** su tihe korupcije podataka — prvo njih.
Zatim **1+2 (envelope/paginacija)** jer ograničavaju točnost odgovora korisniku.
**5-8** su kvaliteta života, idu uz ostalo.
