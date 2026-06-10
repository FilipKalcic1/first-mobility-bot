# 01 — EDGE / Ulaz-izlaz (WhatsApp ↔ bot)

**Svrha**: Prima Infobip WhatsApp webhook (HMAC + dedup + xadd u Redis stream), worker čita stream preko consumer grupe, prosljeđuje u V2Engine i šalje odgovor natrag preko Infobip API-ja.

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `webhook_simple.py` | 1044 | LIVE | FastAPI router: Infobip webhook (HMAC, rate-limit, dedup, xadd) + admin/dijagnostički GET endpointi + webhook-razinski DLQ |
| `worker.py` | 1479 | LIVE | Pozadinski potrošač: XREADGROUP petlja, idempotency lock, `_process_message`→V2Engine, outbound petlja, delayed-outbound, health-reporter, DLQ |
| `main.py` | 427 | LIVE | FastAPI aplikacija (samo API proces): lifespan, montira webhook router pod `/webhook`, `/health`, `/ready`, `/admin/cache-invalidate`. **NE pokreće V2Engine.** |
| `middleware.py` | 84 | LIVE | PayloadSizeGuard (413 za >1MB), RequestID (X-Request-ID), HTTPSRedirect (301+HSTS u prod) |
| `services/whatsapp_service.py` | 662 | LIVE | Slanje WhatsApp poruka preko Infobip REST API-ja (validacija broja, UTF-8 sanitizacija, retry+backoff) |
| `services/queue_service.py` | 218 | PARTIAL | Redis queue wrapper. **Žive samo** `read_stream()` (worker inbound) i `create_consumer_group()` (main lifespan); ostale metode (enqueue/dequeue/store_dlq/ack) nigdje se ne pozivaju iz živog koda |

## Ulaz → izlaz

- **Ulaz**: Infobip HTTP POST na `POST /webhook/whatsapp` (JSON s `results[]`, svaki ima `from`, `message.text`, `messageId`).
- **Izlaz**: WhatsApp poruka korisniku preko Infobip `/whatsapp/1/message/text`; telemetrija u Redis; DLQ za neuspjehe.

## Ključne komponente (ulazne točke)

| Simbol | Lokacija | Što radi |
|---|---|---|
| `whatsapp_webhook` | webhook_simple.py:350 | `POST /webhook/whatsapp` — glavni handler. Rate-limit (429), trace span, delegira `_process_webhook` |
| `_process_webhook` | webhook_simple.py:385 | APP_STOPPING (503), HMAC (401), JSON parse, iteracija `results[]`, tenant rezolucija, dedup, xadd, DLQ. Uvijek HTTP 200 osim 401/429/503 |
| `verify_webhook_signature` | webhook_simple.py:96 | HMAC-SHA256 nad raw body. **Fail-closed**: nema secreta/potpisa → False. Pozvan SAMO ako `settings.VERIFY_WHATSAPP_SIGNATURE=True`, header `X-Hub-Signature-256` |
| Pre-push dedup | webhook_simple.py:565 | `wh_dedup:{message_id}` preko `redis.set(nx=True, ex=60)`. Infobip retry (~500ms) → preskoči |
| xadd u stream | webhook_simple.py:592 | `xadd('whatsapp_stream_inbound', …, maxlen=100000, approximate=True)`, do 3 pokušaja + backoff |
| `_webhook_check_ip` | webhook_simple.py:332 | Per-IP rate-limit 200 req/60s (in-memory deque) → 429 |
| `_write_dlq` | webhook_simple.py:240 | DLQ za neuspjeli xadd: Redis `dlq:webhook` (30d) → fallback `/tmp/dlq.jsonl` → stderr (maskiran PII) |
| `whatsapp_webhook_health` | webhook_simple.py:691 | `GET /webhook/whatsapp` — health ping (PlainText "ok") |
| `Worker.start` | worker.py:250 | Signali, čeka Redis/DB, `_init_services`, consumer grupa, `asyncio.gather` 5 petlji |
| `Worker._process_inbound_loop` | worker.py:690 | `read_stream` (XREADGROUP count=MAX_CONCURRENT, block=1000ms) → svaki msg kao semaforom-ograničen task |
| `Worker._handle_message` | worker.py:812 | session tenant_id, idempotency lock, per-sender lock, `_process_message` (timeout 90s), enqueue_outbound, **ACK tek nakon uspješnog enqueuea** |
| `Worker._process_message` | worker.py:993 | `self._v2_engine.process_message(sender, text)`. **Bez fallbacka** — ako V2Engine baci, greška se propagira |
| `Worker._create_consumer_group` | worker.py:504 | `xgroup_create(..., '0', mkstream=True)` — ID `'0'` čita SVE poruke; čisti zombi consumere |
| `Worker._acquire_message_lock` | worker.py:757 | `msg_lock:{sender}:{message_id}` SET NX EX=300s. **Fail-open** na Redis grešci |
| `Worker._ack_message` | worker.py:1402 | XACK + XDEL, sve pod `suppress(Exception)`. Zove se TEK nakon uspješnog enqueue_outbound |
| `WhatsAppService.send` | services/whatsapp_service.py:380 | Šalje prema Infobip `/whatsapp/1/message/text`, vraća SendResult |
| `main:app` | main.py:201 | FastAPI instanca; webhook_router pod `/webhook` (main.py:253); lifespan (main.py:99) |

## Redis ključevi

- `whatsapp_stream_inbound` — inbound stream (maxlen 100k), consumer grupa `workers`
- `whatsapp_outbound` — outbound queue (RPUSH; chunking >4000 znakova)
- `whatsapp_outbound_processing` — crash-recovery staging (BLMOVE)
- `whatsapp_outbound_delayed` — sorted set za retry (score=timestamp)
- `wh_dedup:{message_id}` — webhook dedup (60s)
- `msg_lock:{sender}:{message_id}` — idempotency lock (300s)
- `dlq:webhook` (30d), `dlq:inbound` (7d), `dlq:outbound` (7d) — dead letter queues
- `sent:{idempotency_key}` — outbound idempotency (600s)
- `session:{sender}:tenant_id` — session tenant isolation (3600s)

## ⚠️ Dvije consumer grupe — nesklad starting ID-a

- `worker._create_consumer_group` (worker.py:504) koristi **`'0'`** (čita sve poruke uključujući stare).
- `QueueService.create_consumer_group` (queue_service.py:136), pozvan iz `main.lifespan:155`, koristi **`'$'`** (preskače stare poruke).

Obje stvaraju istu grupu `workers` na istom streamu; `BUSYGROUP` se ignorira pa prva koja uspije postavi starting ID. Worker se obično pokreće i kreira grupu — ali ovo je load-bearing detalj.

## Što NE radi

- Ne pokreće V2Engine (to radi worker, ne API proces).
- Webhook uvijek vraća 200 osim 401/429/503 — da Infobip ne retry-a beskonačno.
- `queue_service.py` većina metoda (enqueue/dequeue/store_dlq/ack/enqueue_inbound) je **mrtva** — worker ima vlastite ekvivalente.

## Caveati

- DLQ je 3-tier (Redis → file → stderr) jer gubitak poruke = gubitak korisnikove akcije.
- Dedup je oportunistički: na Redis grešci propušta dalje (worker lock i dalje hvata duplikate).
- `resolve_tenant_for_phone` na rubu (webhook_simple.py:487) na iznimci tretira broj kao nepoznat (needs_onboarding) — **nikad ne ispušta poruku** (GW-A4 fix); worker radi auto-onboarding.
