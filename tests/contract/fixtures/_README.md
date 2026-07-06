# Contract fixtures — golden request/response par po akciji

Ovi fixturei su UGOVOR između bota i Business API-ja (`/actions/*`):
- **offline mod** (uvijek u CI): validira strukturu fixturea + (kad postoji
  `config/actions.json`) križa request polja s `ai.parameters`+`policy.inject`.
- **live mod** (CONTRACT_BASE_URL + kredencijali): šalje request na
  `/actions/<name>` i asserta da je odgovor superset očekivanog.

Placeholder vrijednosti u `response`: `<any-string>`, `<any-int>`,
`<any-number>`, `<any-bool>`, `<any>` — "polje mora postojati, vrijednost
tog tipa". Točne vrijednosti se matchaju doslovno.

⚠ Sadržaj je PRIJEDLOG ugovora — konačna polja/kodovi se dogovaraju s
Damirovim timom (spec §8, 8-točki checklist) i fixturei se ažuriraju da
odražavaju dogovoreno. Fixture = izvor istine nakon dogovora.
