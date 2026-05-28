# Zahtjev — Azure OpenAI deploy (gpt-4o + text-embedding-3-large)

> Ide na primatelja koji drži Azure resurs (vjerojatno isti tim koji je deployao `m1-ai-dev`).

## Zašto

WhatsApp bot trenutno koristi `gpt-4o-mini` + `text-embedding-ada-002`. Te modele koristimo za:
- **Routing**: LLM bira 1 od ~50 kandidata-toolova za svaku poruku
- **Embedding**: pretraga ~12 anchora po toolu kroz cosine similarity

Mjerenje na sintetičkom benchmarku pokazuje p@1 ~32-37% / recall@3 ~47-49% s trenutnim modelima. Jači modeli (`gpt-4o` + `text-embedding-3-large`) bi mogli dignuti accuracy:
- **`gpt-4o`** bolje razlikuje slične endpointe i prati upute (procjena: +5-15pp na pick među sličnima)
- **`text-embedding-3-large`** bolji rank kvalitete retrieval-a (procjena: +recall za rijetkostavarne fraze)

To je **mjereno-then-keep** odluka — ako ne digne, vraćamo se na `mini`. Switch je 2-line `.env` promjena na našoj strani.

---

## Što tražimo

Na Azure resursu `m1-ai-dev` (ili produkcijskoj alternativi koju koristimo):

1. **Deploy `gpt-4o`** (chat completion deployment)
   - Bilo koja regija je OK (najbolje Sweden Central / West Europe za latency)
   - Quota: **≥30k TPM (tokens per minute)** i **≥100 RPM (requests per minute)** — za očekivani volume 5-10 simultaneous user-a

2. **Deploy `text-embedding-3-large`** (embedding deployment)
   - Ista regija kao gpt-4o ako moguće
   - Quota: ≥10k TPM (embedding pozivi su manji)

3. **Deployment ime** (naziv kojim ću zvati API) — npr. `gpt-4o-bot` i `text-embedding-3-large-bot`, ili kako preferirate. Recite mi ta dva imena nakon deploy-a.

---

## Što time NE rješavamo (iskreno)

- Jači LLM ne razlikuje 2 toola koji imaju **identičan opis** u Swagger-u (data problem, ne model problem)
- Ne diže accuracy iznad data-stropa M1 metapodataka (zaseban dokument o tome)
- Cost ide gore — `gpt-4o` je ~15-20× po tokenu skuplji od `mini` (provjerite aktualni Azure pricing). Damirov scale (~120 vozača, povremeni upiti) trebao bi podnijeti, ali izmjerit ćemo.

---

## Što ću napraviti čim stigne

1. Update `.env`: `AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4o` + `AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large` (2 linije).
2. Anchor cache se automatski rebuilduje (fingerprint sadrži ime deployment-a).
3. `python scripts/bench_router_e2e.py` → mjeri accuracy delta prije/poslije.
4. Pošaljem broj. Ako je lift ≥+5pp i cost u prihvatljivom rangu → ostavljamo. Inače → vraćam na `mini`.

---

## Hitnost

Niska. Trenutno čekamo i druge stvari (M1 Swagger update, dev test access). Možete deploy kad stigne — bot će raditi i s trenutnim modelima do tad.

Hvala!
