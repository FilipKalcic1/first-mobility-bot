# Bot accuracy — pošten prihvatni kriterij (za dogovor s Damirom)

> Svrha: prije live testa dogovoriti **mjerljiv, pošten** kriterij uspjeha. Ovo zamjenjuje nejasno očekivanje "bot razumije svih 950 alata iz prve" — koje nije dostižno ni s najjačim modelima — preciznim ugovorom koji se može izmjeriti.

## Zašto "950 iz prve" nije prava metrika

- **950 Swagger operacija ≠ 950 korisničkih namjera.** Vozač ne traži "operaciju", nego sposobnost ("rezerviraj auto", "kolika km", "prijavi kvar").
- **Stvarni promet je Zipfov** — ~20 sposobnosti pokriva ~90% svih poruka; dugi rep (rijetki admin alati) gotovo se ne koristi. Mjeriti accuracy na "nasumičnom uzorku od 950" zato daje pesimističnu i irelevantnu brojku.
- Doslovno "svaki od 950 alata, prirodnim jezikom, iz prve" **ne postiže nitko** — to je fizički strop NL-routinga, ne mana implementacije.

## Predloženi ugovor (mjerljivo)

| Kriterij | Cilj |
|---|---|
| First-pick točnost na **golden setu iz stvarnog prometa** | **≥ 90%** |
| Točnost uz **najviše jedno potpitanje** (clarify) | **≥ 97%** |
| **Nepotvrđene mutacije** (write bez "Da") | **0** (tvrdo) |

"Golden set iz stvarnog prometa" = skup stvarnih poruka Damirovih vozača (uzorkovan iz live telemetrije), ne sintetičke rečenice. Mjeri se ono što ljudi STVARNO pišu, ne nasumični 950.

> **Bitno (osigurač protiv nesporazuma):** ove ciljne brojke (90/97/0) NISU trenutno stanje — to je cilj koji se **mjeri na golden setu iz stvarnog prometa** i **ažurira čim pilot krene**. **Trenutni iskreni baseline: ~35% na sintetičkom uzorku cijelog kataloga, ~90% na svakodnevnoj rutini** (detalji niže). Ne prodajemo 90% kao "danas radi 90%".

## Pošteno o trenutnom stanju (bez uljepšavanja)

- **Bot nikad nije bio live** — nula stvarnog prometa. Svaka accuracy brojka do sada je sintetički proxy.
- Sintetički test (50 nasumičnih alata, 30.05.): **~35% kad bot uopće pickne**, glavni izvor greške = sibling kolizije (`delete_X` vs `delete_X_id`). Driver-rutina (km/registracija/rezervacije) ide deterministički, **~90%**.
- **Druga polovica problema je M1, ne bot:** u tom testu **0× greška tipa 422** (bot strukturno ispravno gradi pozive) — blokade su bile **403 scope** (bot nema ovlasti) i 5xx. To se ne rješava boljim routingom, nego M1 proširenjem scope-a + Swagger metapodacima (mail je već poslan).

## Što ovo znači za go-live (redoslijed)

1. **Smoke test** (10 reprezentativnih upita) — gate ≥ 7/10 prije nego ide Damiru.
2. **Live s 5-10 testera** → mjerimo na STVARNOM prometu (golden set se sam puni iz "nije točno" signala + telemetrije).
3. **Ciljani popravak** top-5 stvarnih grešaka iz prvog tjedna — ne nagađanje, nego ono što podaci pokažu.
4. **M1 isporuka** (scope + enumi + opisi) podiže strop neovisno o routingu.

**Bottom line za Damira:** ne obećavamo "100% na 950". Obećavamo **pouzdano za ono što vozači stvarno rade (≥90%), sa sigurnosnom mrežom (potpitanje + potvrda prije svake izmjene), i mjerljivo na stvarnom prometu** — uz iskren plan kako rastemo odande.
