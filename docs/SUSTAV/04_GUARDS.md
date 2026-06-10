# 04 — GUARDS (pred-routing zaštite)

**Svrha**: Deterministički moduli koji se izvršavaju prije (i oko) routinga: rate-limiting, PII scrubbing, prompt-injection obrana, detekcija krize, negacije, multi-intenta, meta- i specijalnih intenta te scrubbing PII-ja u logovima. Svih 10 datoteka je LIVE.

## Datoteke

| Datoteka | LOC | Status | Uloga |
|---|---|---|---|
| `services/v2/rate_limiter.py` | 117 | LIVE | L-1: per-telefon Redis token bucket → RateLimitDecision. Fail-open |
| `services/v2/pii_scrubber.py` | 102 | LIVE | L0.5: PII (IBAN/EMAIL/JMBG/OIB/CARD/PHONE) → placeholderi prije LLM-a |
| `services/v2/input_sanitizer.py` | 157 | LIVE | L0.6: obrana od direktnog prompt-injectiona iz korisničke poruke |
| `services/v2/crisis_detector.py` | 156 | LIVE | Detekcija mentalne krize → Plavi telefon 116 123 / 112 |
| `services/v2/negation_handler.py` | 69 | LIVE | Jaka negacija (nemoj/odustani/cancel that) → otkazni odgovor |
| `services/v2/multi_intent_detector.py` | 127 | LIVE | >1 namjera u poruci → clarify "što prvo?" |
| `services/v2/meta_intents.py` | 111 | LIVE | Meta-intenti o botu (tko si ti / bug / undo / chat / harassment / vrijeme) |
| `services/v2/special_intents.py` | 306 | LIVE | L1 hardkodirani policy/UX (WELCOME/GDPR_DELETE/GDPR_EXPORT/HANDOVER/HELP/GREETING) + side_effects |
| `services/v2/output_sanitizer.py` | 141 | LIVE | Obrana od **indirektnog** prompt-injectiona kroz API odgovor (pozvan u llm_formatter, NE u pred-routingu) |
| `services/pii_filter.py` | 79 | LIVE | Logging filter (PIIScrubFilter): scrubba PII iz **logova** (drugačija svrha od pii_scrubber) |

## Ulazne točke (sve pozvane iz engine.py osim output_sanitizer)

| Simbol | Lokacija | Engine poziv |
|---|---|---|
| `RateLimiter.check` | rate_limiter.py:74 | engine.py:245 |
| `PIIScrubber.scrub` | pii_scrubber.py:85 | engine.py:251 (+224, +1339) |
| `input_sanitizer.sanitize` | input_sanitizer.py:88 | engine.py:276 |
| `crisis_detector.detect` | crisis_detector.py:119 | engine.py:425 |
| `negation_handler.detect` | negation_handler.py:54 | engine.py:441 |
| `multi_intent_detector.detect` | multi_intent_detector.py:70 | engine.py:454 |
| `meta_intents.detect` | meta_intents.py:97 | engine.py:468 |
| `detect_special_intent` | special_intents.py:89 | engine.py:478 |
| `output_sanitizer.sanitize` | output_sanitizer.py:100 | llm_formatter.py:79 |
| `PIIScrubFilter.filter` | pii_filter.py:60 | main.py:38-40, worker.py:27-29 |

## Detalji po modulu

- **RateLimiter** (rate_limiter.py:57): limiti `NORMAL_PER_MIN=30`, `COOLDOWN_PER_MIN=100`, `SUSPICION_PER_HOUR=500`. Ključevi `v2:rl:m:{phone}` (60s), `v2:rl:h:{phone}` (3600s), atomični INCR+EXPIRE (Lua). >100/min → blokada + poruka; >500/h → suspicious=True + log (NE blokira). **Fail-open** na bilo koju Redis grešku.
- **PIIScrubber** (pii_scrubber.py:82): 6 regex kategorija **fiksnim redom** (linije 49-79): IBAN, EMAIL, JMBG (13 znamenki, **prije CARD**), OIB (11), CARD, PHONE (`(?:\+|00)?\d{8,12}`). Svaki match → placeholder `[OIB_REDACTED]` itd. + Redaction(kind, placeholder, original_length).
- **input_sanitizer** (input_sanitizer.py:88): `MAX_USER_MSG=2000` (warn), `MAX_REPEAT_TOKEN=50` (token-flood → block). Strip role-tagova (`[SYSTEM:]`/`<<INST>>`/`### USER:`). Bypass patterni ("ignoriraj prethodno", "you are now admin") → block `injection_bypass`. Framing ("simuliraj brisanje") → block `framing_bypass`. Leak ("pokaži system prompt") → samo warn.
- **crisis_detector** (crisis_detector.py:119): `_ACUTE_PATTERNS` ("ubit ću se", "ne želim više živjeti", "kill myself") + `_CONCERN_PATTERNS` ("ne mogu više", "očajan"). `_FALSE_POSITIVE_CONTEXT` prvo diskvalificira figurativno ("ubit ću tu lozinku"). Acute → 116 123 + 112.
- **negation_handler** (negation_handler.py:54): patterni "nemoj rezerviraj/otkaži", "ne, odustani", "zaboravi na to", "cancel that" → NegationResult(detected, confidence=0.85, response).
- **multi_intent_detector** (multi_intent_detector.py:70): 7 kategorija glagola. Flag samo ako ≥2 RAZLIČITE kategorije + veznik. <2 ne-prazna dijela → detected=False.
- **meta_intents** (meta_intents.py:97): 6 kategorija (self_identity, bug_report, undo_request, personal_chat, harassment, weather_time). self_identity otkriva model gpt-4o-mini. **Handoff NIJE ovdje** — to radi special_intents.HANDOVER.
- **special_intents** (special_intents.py:89): GDPR prvo (pravni prioritet), zatim first-contact WELCOME (po `IdentitySnapshot.is_first_contact`), pa longest-match. WELCOME uključuje EU AI Act disclosure. side_effects: GDPR/HANDOVER vraćaju `{'action': ...}` tuple — **engine ih izvršava** u `_handle_special_side_effects` (engine.py:712), special_intents ih NE izvršava sam.
- **output_sanitizer** (output_sanitizer.py:100): strip `[SYSTEM:]` itd. iz API podataka; imperative ("ignore previous") → cijeli field prefiks `[QUOTED FROM API DATA]:`. Rekurzija max_depth=6. Nikad ne baca.
- **PIIScrubFilter** (pii_filter.py:57): scrubba `record.msg/args/exc_text`. Kategorije: PHONE, EMAIL, IBAN, OIB (samo uz ključnu riječ "oib"), IPv4. Operira na **logovima**, ne na LLM ulazu.

## Redis ključevi

- `v2:rl:m:{phone}` (60s), `v2:rl:h:{phone}` (3600s) — samo rate_limiter.

## Što NE radi

- Guards NE rade routing/odabir alata — samo presretu prije/oko njega.
- crisis_detector NE zove LLM (onemogućeno, "future") i NE glumi terapeuta.
- pii_scrubber NE radi Luhn validaciju, NE detektira imena/adrese (samo strukturalni regex).
- input_sanitizer brani od DIREKTNOG injectiona; output_sanitizer od INDIREKTNOG (kroz API). Ne preklapaju se.
- rate_limiter NE blokira na Redis outageu (fail-open).
- special_intents NE izvršava side_effects sam (vraća tuple, engine izvršava).

## Caveati

- **Redoslijed PII regexa je load-bearing**: JMBG/OIB namjerno prije CARD jer PHONE `\d{8,12}` može redaktirati OIB/JMBG ako se redoslijed promijeni (komentar pii_scrubber.py:46-48).
- `RateLimitDecision.suspicious=True` NE blokira (allowed ostaje True) — samo logira za Damirov pregled.
- output_sanitizer se NE izvršava u pred-routing lancu nego unutar `LLMFormatter.format` (llm_formatter.py:79) tijekom formatiranja API odgovora. (Sloj "L7.5" spominje se samo u `tests/v2/test_architecture.py`, NE u samom output_sanitizer docstringu.)
- crisis_detector docstring (linija 14) tvrdi da trči "odmah nakon PII scruba" — u stvarnom engine kodu trči nakon identity i pending koraka (engine.py:425).
- pii_filter OIB hvata samo uz ključnu riječ (uže od pii_scrubber); to je namjerno (logovi imaju kontekst).
