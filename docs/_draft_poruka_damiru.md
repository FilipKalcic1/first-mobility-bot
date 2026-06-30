# Nacrt poruke Damiru (prilagodi po svom)

> Kratko, neformalno — da pošalješ prije ponedjeljka i otvoriš razgovor.
> Ispod su dvije verzije: kraća (chat/Teams) i malo dulja (mail).

---

## Verzija A — kraća (chat / Teams)

Bok Damire,

Pregledao sam arhitekturu — sviđa mi se smjer i, iskreno, dobar dio toga već
imamo u sustavu (tool config s `ai`/`execution` splitom, execution+auth sloj,
OpenAI za decision i formatiranje). Ono što mi najviše skače u oči je
**`/actions` (Business API) sloj** — mislim da je baš to ono što bi nam najviše
diglo točnost: trenutno bot bira između ~950 granularnih Swagger endpointa
(i tu nastaju greške na "blizancima" tipa `delete_X` vs `delete_X_id`), a kad bi
birao između ~30 čistih akcija (`book_vehicle`, `add_mileage`…) te kolizije
strukturno nestaju.

Iskreno o trenutnom stanju: taj `/actions` sloj danas ne postoji — bot zove
granularne API-je direktno i sam nosi orkestraciju koja bi po tvom dijagramu
trebala biti u Business API-ju. Zato mi "AI Backend" nije thin kao na slici.

Glavno pitanje koje bih volio da zajedno riješimo: **tko gradi `/actions` i
kojih je prvih ~15 akcija?** Kad to dogovorimo, repoint bota na te akcije je brz
i dobitak na točnosti dolazi odmah — bez čekanja cijelog kataloga. (Usput, vidim
da računamo i s Viberom — to je dobra vijest, ide kroz isti Infobip pa je mali
posao, mozak bota se ne dira.)

Slobodan sam kad tebi paše prije ponedjeljka da prođemo zajedno. 👍

---

## Verzija B — malo dulja (mail)

Bok Damire,

Pregledao sam "Fleet AI Assistant" arhitekturu. Sviđa mi se smjer i zapravo se
dosta poklapa s onim što već imamo — tool config s `ai`/`execution` podjelom,
execution+auth sloj (kod nas gateway + token manager), i OpenAI za odluku i
finalno formatiranje. To mi je ohrabrujuće jer znači da ne krećemo ispočetka.

Najveća vrijednost dijagrama mi je **`/actions` (Business API) sloj**. Mislim da
je to ključ za točnost: danas bot bira između ~950 granularnih Swagger
operacija, i najveći izvor grešaka su "sibling" parovi koji se razlikuju samo u
sufiksu ili PUT↔PATCH (`delete_X` vs `delete_X_id`). S ~30 čistih, intent-
oblikovanih akcija (`book_vehicle`, `add_mileage`, `report_incident`…) te se
kolizije strukturno gube — LLM bira iz puno manjeg i čišćeg skupa.

Da budem iskren oko trenutnog stanja: `/actions` sloj još ne postoji. Bot zove
granularne API-je direktno i sam radi orkestraciju (koja akcija, koji parametri,
dohvati-pa-upiši), a po tvom dijagramu bi to trebalo biti u Business API-ju.
Zato mi "AI Backend" nije thin — nosi posao koji bi inače nosio Business API.

Par stvari za koje bih volio da se uskladimo (ništa od ovog ne presuđujem,
donosim kao pitanja):
- **Tko gradi `/actions` i kojih je prvih ~15?** Ako vaš tim izloži top ~15
  akcija, ja bota repointam na njih i točnost skoči odmah.
- **MCP** — mislimo li na protokol (npr. da M365 Copilot može direktno zvati naše
  akcije), ili na execution sloj općenito? Bitno je jer mijenja što gradimo.
- **Auth na WhatsApp** — per-user token tamo ne ide (nema SSO/redirect), pa danas
  radimo service principal + tenant + provjeru ovlasti; htio bih potvrdu da je to
  prihvatljiv model.
- **Viber/multichannel** — vidim da računamo s više kanala; dobra vijest je da
  Viber ide kroz isti Infobip, pa je to mali posao bez diranja mozga bota.

Pripremio sam i kratku usporedbu po slojevima (što već imamo / što fali) pa
možemo proći zajedno kad ti paše prije ponedjeljka.

Pozdrav,
Filip
