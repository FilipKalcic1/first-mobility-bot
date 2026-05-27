# Honest mjerenje: FORMAT ekstrahiranih INPUT params (datumi/rasponi) — 2026-05-25

**Pitanje (Filip):** *"Kad bot izvuče podatke iz poruke, formatira li ih dobro kad ih stavi u poziv toola? Posebno datumi, rasponi i stvari pisane na specifičan način."*

**Pristup:** "izmjeri prvo" (Filip 2026-05-25). Prošireni `scripts/bench_extraction.py` (Part B) + `tests/benchmarks/extraction_eval.json` (14 `datetime` upita). **BEZ ikakvih promjena engine/router koda** — samo mjerni alat. Pokrenuto protiv pravog Azure gpt-4o-mini + ada-002, pravi `tool_schema_builder` (dakle odražava produkciju).

**Metoda:** za datum/raspon upite router je **forsiran na ciljani tool** (`tool_filter={expected_tool}`) da se FORMAT ekstrakcije izolira od routing-točnosti. Skoriranje je **format-aware** (NE digit-normalizirano): `CORRECT` (ISO 8601, točan trenutak) / `WRONG_FORMAT` (točan trenutak, kriv oblik — npr. `17.05.2026` ili ostavljeno "sutra") / `WRONG_VALUE` (ISO oblik ali kriv datum) / `MISSING`.

---

## ✅ UPDATE 2026-05-25 — fix primijenjen i verificiran

Relativni datumi POPRAVLJENI u [llm_router.py](services/router/llm_router.py) (sistem prompt sad sadrži današnji datum + tablicu sljedećih 7 dana s nazivima dana; pravilo traži pretvorbu relativnih izraza u ISO). **BEZ promjene schema/executora.** Re-mjereno:

| | PRIJE fixa | POSLIJE fixa |
|---|---|---|
| Relativni datumi (param-level, među routiranima) | **0%** (`sutra`→`2023-10-04`) | **100% (5/5)** |
| `sutra` | ❌ 2023-10-04 | ✅ 2026-05-26 |
| `prekosutra u 10` | (nije routiralo) | ✅ 2026-05-27T10:00:00 |
| `u petak` | ❌ 2023 / kasnije srijeda | ✅ 2026-05-29 (tablica 7 dana riješila weekday-aritmetiku) |
| `sutra od 9 do 15` (raspon) | ❌ 2023 | ✅ 2026-05-26T09:00:00 / T15:00:00 |
| Apsolutni datumi | 100% | 100% (nepromijenjeno) |

Trebalo je **dvije iteracije prompta**: (1) ubaci današnji datum → riješio `sutra`/`danas`/`prekosutra`; (2) dodaj tablicu sljedećih 7 dana → riješio nazive dana (`u petak`), jer je LLM znao datum ali je krivo *računao* koji je dan petak. `pytest` ostao zelen (1603). Preostali `no_tool_call` u izlazu je odvojena routing-nestabilnost (~strop), ne datum.

---

## Rezultati

### Part A — jednostavna ekstrakcija (id / broj), routing-ovisno
| Mjera | Broj |
|---|---|
| Tool pogođen točno | 5/12 (41.7%) |
| Param-ekstrakcija (među tool-točnima) | **5/5 = 100%** |
| Promašaji | 7× `no_tool_call`, 1× krivi tool (e06→put_Cases_id) |

Kad je tool točan, brojčana/id ekstrakcija je **savršena** (uklj. `42.500 km`→`42500`). Usko grlo su `no_tool_call` odbijanja routera, **ne ekstrakcija**.

### Part B — FORMAT datuma/raspona (tool forsiran)

**Apsolutni datumi** (`17.05.2026`, `od 8 do 16`, `od X do Y`):
| Mjera | Broj |
|---|---|
| Routirano (od forsiranih) | 5/9 |
| **Format ispravan (ISO) među routiranima** | **8/8 = 100%** |
| WRONG_FORMAT / WRONG_VALUE / MISSING | 0 / 0 / 0 |

Primjeri (stvarni LLM output):
- `17.05.2026` → `2026-05-17` ✓
- `od 17.05.2026 09:00 do 17.05.2026 17:00` → `2026-05-17T09:00:00` / `2026-05-17T17:00:00` ✓ (raspon savršeno)
- `20.05.2026 od 8 do 16` → `2026-05-20T08:00:00` / `2026-05-20T16:00:00` ✓ (sam zaključio sate iz "8"/"16")
- `od 1.6.2026 do 5.6.2026` → `2026-06-01T00:00:00Z` / `2026-06-05T00:00:00Z` ✓ (dodao Z, isti trenutak)

**Relativni datumi** (`sutra`, `danas u 9h`, `u petak`, `prekosutra`):
| Mjera | Broj |
|---|---|
| Routirano (od forsiranih) | 1/5 |
| Format/value ispravan | **0/2 = 0%** |
| Failure mode | **WRONG_VALUE** (ISO oblik, halucinirani datum) |

Jedini koji je routirao — `rezerviraj vozilo sutra od 9 do 15`:
- `FromTime` → `2023-10-04T09:00:00` (očekivano `2026-05-26T09:00:00`)
- `ToTime` → `2023-10-04T15:00:00` (očekivano `2026-05-26T15:00:00`)
- **Vrijeme i format točni, datum izmišljen iz 2023.** LLM ne zna koji je danas dan.

---

## Zaključak (pošteno)

Filipov strah se **dijeli na dva odvojena problema, oba precizno locirana:**

1. **FORMAT apsolutnih datuma/raspona = NIJE problem.** Mjereno **100% (8/8)** ispravan ISO 8601 — čak iako schema NE prosljeđuje `format` LLM-u ([tool_schema_builder.py:227](services/router/tool_schema_builder.py#L227)). gpt-4o-mini pouzdano pretvara HR datum u ISO, parsira "od 8 do 16" u sate, i "od X do Y" u from/to par. Echo/coerce za format apsolutnih datuma **nije potreban** prema ovim brojkama.

2. **RELATIVNI datumi = bio VALUE problem (ne format), SAD POPRAVLJEN.** `sutra` → `2023-10-04` jer llm_router nije injektirao današnji datum. **Fix primijenjen** (vidi UPDATE gore): prompt sad nosi današnji datum + tablicu 7 dana → re-mjereno **100% (5/5)**. Format je oduvijek bio točan; nedostajao je samo datumski kontekst.

3. **Dominantni blocker (kontekst): `no_tool_call`.** Čak i kad je tool JEDINI ponuđen, gpt-4o-mini ga je odbio pozvati u ~60% slučajeva (7/12 Part A, 8/14 Part B). To je ista routing-nestabilnost dokumentirana drugdje (~20% long-tail) — ekstrakcija/format se ne mogu ni mjeriti dok tool nije pozvan. **Routing ostaje pravi strop.**

### Preporuka (CLAUDE.md format — odluka je Filipova)
1. **Konkretno:** ✅ NAPRAVLJENO — relativni datumi popravljeni injekcijom današnjeg datuma + tablice 7 dana u router prompt (vidi UPDATE). Apsolutni format je već bio 100%. Preostaje (odvojeno, ako se želi): echo izvučenih params u mutation-confirm kao dodatni safety net.
2. **Glavna slabost:** uzorak je malen i `no_tool_call` ga je dodatno prorijedio (8 apsolutnih params, 2 relativna). Brojke su **indikativne, ne konačne** — relativni signal posebno tanak (1 routiran). Pravu sliku da samo live promet.
3. **Alternativa:** i dalje dodati **echo izvučenih params u mutation-confirm** (tvoja originalna "prekontrola" ideja) kao safety net neovisno o formatu — hvata i value-greške (npr. 2023 datum) PRIJE write-a, ne samo format. Komplementarno injekciji datuma.
4. **Trošak ako pogriješiš:** ako se relativni datumi ne fiksaju → "rezerviraj sutra" tiho upiše 2023 datum (krivi write, najgori scenarij). Ako se troši na format apsolutnih → rješavaš ne-problem.

---

## Reprodukcija
```
python scripts/bench_extraction.py
# Part A (id/broj) + Part B (datum/raspon, format-aware). Treba Azure (ada-002 + gpt-4o-mini).
```
Caveati: forsiranje toola ograničava KANDIDATE, ne prisiljava poziv (otud `no_tool_call`); relativni expected se računa u runtime-u (`now`); eval set je ručno pisan proxy, ne pravi user logovi.
