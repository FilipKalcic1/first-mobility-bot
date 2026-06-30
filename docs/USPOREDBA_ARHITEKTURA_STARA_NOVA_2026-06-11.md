# Usporedba stare i nove arhitekture

*Napisao: Filip — 11.06.2026.*

Sjeo sam i usporedio kako moj sustav radi **danas** (stara arhitektura) s onim
što idemo napraviti (**nova**, bazirana na `/actions` Business API sloju). Ovo je
samo usporedba — bez plana migracije, samo da jasno vidim razliku između to dvoje.

---

## Ukratko (jedna rečenica po svakoj)

- **Stara:** moj bot razgovara **direktno s ~950 granularnih API funkcija** i sam
  slaže svaki poziv — koju funkciju, kojim redom, s kojim parametrima.
- **Nova:** moj bot razgovara sa **~30 čistih poslovnih akcija** (`book_vehicle`,
  `report_incident`…), a teški posao (koji granularni pozivi, kojim redom, s kojim
  poljima) seli u **Business API** na MobilityOne strani.

---

## Side-by-side

| Što | STARA (danas) | NOVA (cilj) |
|---|---|---|
| **Što bot bira** | 1 od **~950** granularnih Swagger operacija | 1 od **~30** poslovnih akcija |
| **Tko slaže pozive prema bazi** | **moj bot** (sam pogađa funkciju + parametre) | **Business API** (MobilityOne) |
| **Tko zna "glupe" parametre** (`EntryType`, `AssigneeType`, `CaseType:3`) | moj bot ih hardkodira (kod mene to radi `flow_engine.py`) | Business API ih zna interno; bot ih ni ne vidi |
| **Najveći problem** | sibling kolizije (`delete_X` vs `delete_X_id`, PUT vs PATCH) → točnost pada | tih kolizija nema jer biram iz ~30 čistih akcija |
| **Točnost routinga** | ~35% na cijelom katalogu, ~90% na svakodnevnoj rutini | strukturno puno više (manji, čišći izbor) |
| **Koliko bot zna o backendu** | mora poznavati 950 endpointa i njihove parametre | zna samo ~30 akcija i koja polja korisnik daje |
| **Gdje živi poslovna logika** | u botu (improvizirano) | u Business API-ju (jedno mjesto) |
| **Tko može koristiti tu logiku** | samo moj bot | bot **+ web Query Builder + Copilot** (isti `/actions`) |
| **Težina configa** | `tool_data.json` 3.8 MB (950 alata) + cijeli Swagger registar | ~30 akcija |
| **Slojevi u botu** | routing nad 950 + orkestracija + safety + prijevod | tanji: safety + prijevod jezika + poziv akcije |
| **Kanali** | samo WhatsApp | WhatsApp + Viber (+ kasnije Web, M365 Copilot) |
| **Auth** | jedan service principal (`client_credentials`) + `x-tenant` | isto za WhatsApp; per-user gdje kanal to dopušta (Web/M365) |
| **Što bot radi najbolje** | sve pomalo (i routing, i orkestraciju, i razgovor) | samo ono u čemu je dobar: razumije ljude i prevodi u jednu akciju |

---

## Isti primjer kroz obje arhitekture

**Korisnik:** *"Prijavljujem kvar na ZG-1234-AB, pukla je guma."*

### Stara (danas)
Moj bot mora sam:
1. pogoditi koju od 950 funkcija (tražim incident/case endpoint),
2. naći vehicle_id iz registracije,
3. složiti payload s točnim poljima (`CaseType`, `Status`…),
4. eventualno još zaključati kalendar.

Sve to **moj bot radi sam** → 4-5 mjesta gdje mogu pogriješiti, sva u meni.

### Nova (cilj)
Moj bot:
1. shvati da je ovo `report_incident`,
2. izvuče `registracija = ZG-1234-AB`, `opis = pukla guma`,
3. pošalje **jedan čisti poziv**: `POST /actions/report-incident`.

Što se događa dalje (nađi vozilo → kreiraj incident `CaseType:3` → blokiraj
kalendar) **radi Business API**. Moj bot to ni ne zna — vidi samo rezultat.

---

## Što se NE mijenja (ostaje isto u obje)

Da ne ispadne da nova arhitektura mijenja sve — ovo ostaje identično:

- **Ulaz/izlaz (kanali):** webhook → Redis stream → worker → slanje natrag.
- **Sigurnosni slojevi:** PII scrubbing, prompt-injection zaštita, rate-limit.
- **Da/Ne potvrda prije svake izmjene** (mutation gate) — i dalje pitam korisnika.
- **Identitet:** telefon → osoba → tenant (multi-tenant izolacija).
- **Prijevod odgovora na hrvatski** (formatter).
- **GDPR i crisis handling.**

Mijenja se **samo srednji dio** — kako bot komunicira s backendom nakon što
shvati što korisnik želi. Vrh (razumijevanje jezika) i sigurnost ostaju moji.

---

## Moje kratko mišljenje

Stara arhitektura nije "kriva" — radi, i za naših ~20 najčešćih radnji je dobra.
Ali strop joj je u tome što bot mora sam baratati s 950 sitnih funkcija, i tu se
gubi točnost. Nova arhitektura ne mijenja **kako korisnik priča** — mijenja samo
**kako moj bot priča s ostatkom aplikacije**: umjesto da kopam po 950 ladica,
stisnem jedan od ~30 jasnih gumba, a teški posao preuzme Business API.

Za mene je to ista arhitektura kao i sad, samo s jednim slojem više (`/actions`)
koji preuzme posao koji danas radim improvizirano u botu.
