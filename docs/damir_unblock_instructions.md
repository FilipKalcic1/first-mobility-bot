# How to unblock the +10-15pp Top-1 accuracy lift

Updated 2026-04-27 (post 12.20.0 — Top-1 at 59% lenient on 108-query benchmark).

## TL;DR — what I need from Damir

**Send a JSON file with the production routing log from the last 7-14
days. ~5 minutes of his time. Unblocks an estimated +10-15pp Top-1
lift PLUS Recipe Book expansion to 100% on top intents.**

That's it. Everything else flows from this single export.

## What "production routing log" means

The bot already logs every routing decision to Redis. The key in
production is:
```
routing:accuracy_log
```
It's a Redis list of JSON-encoded entries, one per user query.

## The 5-minute export procedure

### Option A — direct Redis dump (fastest)

On any machine that can reach the production Redis (Damir's laptop
on VPN, or a kubectl exec into the bot pod):

```bash
redis-cli -h <prod-redis-host> -p 6379 \
  LRANGE routing:accuracy_log 0 -1 \
  > routing_log_last_n.jsonl
```

Send me `routing_log_last_n.jsonl` — any path works. Even
~50,000 entries is a small file (~10-50 MB).

### Option B — kubectl exec (if no VPN)

```bash
kubectl exec -n <prod-namespace> <bot-pod-name> -- \
  redis-cli LRANGE routing:accuracy_log 0 -1 \
  > routing_log_last_n.jsonl
```

### Option C — minimal Python script (if neither tool available)

Run on the prod machine:

```python
import asyncio, json, redis.asyncio as redis
async def main():
    r = redis.from_url("redis://localhost:6379/0")
    entries = await r.lrange("routing:accuracy_log", 0, -1)
    with open("routing_log.jsonl", "w", encoding="utf-8") as f:
        for raw in entries:
            f.write(raw.decode("utf-8") + "\n")
    print(f"wrote {len(entries)} entries")
asyncio.run(main())
```

Email me `routing_log.jsonl`. Done.

## What's in each log entry

The bot writes structured JSON per query. Expected shape (from
existing P0 mining scripts in `scripts/mine_routing_log.py`):

```json
{
  "ts": "2026-04-26T14:32:11Z",
  "sender_suffix": "1234",
  "query": "moja registracija",
  "selected_tool": "get_MasterData",
  "confidence": 0.81,
  "intent": "READ",
  "execution_status": "success",
  "user_role": "driver"
}
```

Even a partial export with just `query` + `selected_tool` is
enough to start.

## What I'll do with the file when you send it

Within 30 minutes of receiving the file:

1. **Parse + categorize** — `scripts/mine_routing_log.py` already
   exists; I'll run it against the file.

2. **Top-50 query patterns** — group similar queries, count
   frequency. Tells us which intents fire the most.

3. **Add to benchmark corpus** — pick 30-50 queries from the top
   patterns, label them, and measure current accuracy on real data
   (not my hand-crafted queries).

4. **Identify Recipe candidates** — any intent firing >50 times/week
   becomes a recipe candidate. Each shipped recipe = **100%** on that
   intent (deterministic).

5. **Identify systematic misroutes** — patterns where the bot picks
   the wrong tool consistently. Each becomes a target for boost-
   engine tuning OR docs cleanup.

6. **Re-prioritize the next 5 fixes** — instead of guessing, the
   data tells us where the +10-15pp is hiding.

## Estimated lift after Damir-unblock

| Source | Estimated Top-1 lift |
|---|---|
| Recipe Book expansion to top-20 production intents | +5-8pp |
| Production-grounded boost-engine tuning | +3-5pp |
| LLM-pass doc cleanup with grounding | +5-10pp |
| Targeted slang/synonym additions from real queries | +2-3pp |
| **Total realistic** | **+15-25pp** |

Current Top-1 lenient: **59%**. With Damir-unblock: realistic ~74-84%.

## What I CANNOT do without this file

- Speculate which intents to add as recipes (I'd add wrong ones)
- Tune for theoretical query patterns (would optimize the wrong things)
- Validate that today's gains actually translate to real users
- Detect the "long tail" of weird queries that production users send
  but I'd never think to test

## What I CAN keep doing without it

- Ship parallel rerank batching (+5-8pp est, ~1d)
- Ship LLM-pass doc cleanup with carefully-designed prompt (~1d)
- Continue benchmark expansion if you suggest categories I missed

These keep moving the dial without the production data, but they
hit a ceiling around 65-70% Top-1. The Damir-unblock breaks past
that ceiling.

## Privacy / sensitivity considerations

The routing log contains user queries — these are messages real
users sent the bot. Considerations:

- **Sender_suffix** is the last 4 digits of phone number (already
  anonymized). Safe.
- **Query text** may contain personal info if users type things
  like "moj broj telefona je 0911234567 koliko mi je km".
- **Selected_tool** is just operation_id (no PII).

If you want to redact phone numbers or full names from the queries
before sending, here's a one-liner:

```bash
sed -E 's/[+0-9]{8,}/<REDACTED>/g; s/[A-ZČĆŽŠĐ][a-zčćžšđ]+ [A-ZČĆŽŠĐ][a-zčćžšđ]+/<NAME>/g' routing_log.jsonl > routing_log_clean.jsonl
```

Or just send as-is — I'll handle the file locally without re-sharing.

## How long until I can ship the unblocked work

- Day of receipt: Top-50 patterns + first 5 production-grounded
  recipes + first measurable accuracy lift
- Day +1: 10-15 recipes + LLM doc cleanup with grounded prompts
- Day +2: re-tuned boost engine + paraphrase normalization
  expansion
- Day +3: re-run full 108+ benchmark + assess remaining gap

Estimate: **3 working days from receipt to a system at 75-85% Top-1**.

## The single concrete action

```
1. Connect to production Redis (VPN, kubectl, or direct)
2. Run:
   redis-cli LRANGE routing:accuracy_log 0 -1 > routing_log.jsonl
3. Email me routing_log.jsonl
4. I take it from there.
```

Five minutes of Damir-time. Three days of accuracy work for me. ~+15-25pp Top-1 if it works as expected.
