"""Empirically test whether MobilityOne deduplicates by Idempotency-Key.

The question (review §6): the bot sends `Idempotency-Key: <uuid>` on every
mutation, reusing the SAME uuid across retries. If a mutation times out after
the write landed and we retry, does M1 collapse the two into ONE record, or
create TWO? Our side is correct (we send the key); whether the SERVER honors
it is unknown — this probe answers it definitively by COUNTING records.

Method: send the same mutation TWICE with the SAME Idempotency-Key + identical
body, then count the records that match a distinctive marker.
  - count delta 1 (and/or same record id back twice) → M1 DEDUPLICATES → our
    retry is safe, change nothing.
  - count delta 2 (two distinct ids)                 → M1 IGNORES the header →
    disable mutation retry on our side (contingency in the plan).

Caveat: this proves ONE endpoint on the instance you point it at (likely
behaves the same elsewhere, but dev may differ from prod, and some endpoints
may dedup while others don't). Asking M1 "do you honor Idempotency-Key, and on
which endpoints?" is the more complete answer; this is the empirical check.

SAFETY (this script WRITES real data):
  - DRY-RUN by default. Nothing is sent unless you pass --arm.
  - --confirm-host SUBSTR must be a substring of MOBILITY_API_URL, or it
    aborts. Point this at DEV/STAGING, never production. The substring is your
    manual confirmation you know which host you're hitting.
  - It ALWAYS attempts cleanup (DELETE every created record id) at the end.
  - Use a test vehicle + a far-future window so leftover test data is obvious
    and collides with nothing real. Coordinate with the M1 team if possible.

Usage:
  python scripts/probe_idempotency.py --print-example > probe_cfg.json
  # edit probe_cfg.json with a DEV person/vehicle id + far-future window
  python scripts/probe_idempotency.py --config probe_cfg.json            # dry-run
  python scripts/probe_idempotency.py --config probe_cfg.json \
      --arm --confirm-host dev                                            # real test

Requires: .env with DEV MobilityOne creds + URL, reachable DEV instance,
unlocked OAuth client. Run locally / on the bot host, NOT from a sandbox.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("probe_idempotency")

REPO_ROOT = Path(__file__).resolve().parents[1]

_LIST_KEYS = ("Result", "Results", "data", "Data", "Items", "items", "value", "list")


def _extract_list(data) -> list:
    """Pull the record list out of a MobilityOne response envelope."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in _LIST_KEYS:
            if isinstance(data.get(k), list):
                return data[k]
        if data.get("Id") or data.get("id"):
            return [data]
    return []


def _count_matching(rows: list, match: dict | None) -> int:
    """Count rows; if `match` {field, value} given, only those that equal it.

    Matching on a distinctive marker (e.g. Description or FromTime) isolates
    our test records from concurrent activity, so the count delta is clean.
    """
    if not match:
        return len(rows)
    field, value = match.get("field"), match.get("value")
    return sum(1 for r in rows
               if isinstance(r, dict) and str(r.get(field)) == str(value))


def verdict(count_before: int, count_after: int,
            id1, id2, post1_ok: bool, post2_ok: bool) -> tuple[str, str]:
    """Definitive read of the experiment. Returns (verdict, explanation)."""
    if not (post1_ok and post2_ok):
        return ("INCONCLUSIVE",
                "Jedan/oba POST-a nisu uspjela (403/422/5xx) — ne možemo "
                "zaključiti o dedup-u. Vidi sirove odgovore gore.")
    delta = count_after - count_before
    same_id = id1 is not None and id2 is not None and str(id1) == str(id2)
    if delta <= 1 or same_id:
        return ("DEDUP",
                f"delta={delta}, isti_id={same_id} → M1 DEDUPLICIRA po "
                "Idempotency-Key. Retry je siguran; ništa ne mijenjamo.")
    if delta == 2 or (id1 and id2 and str(id1) != str(id2)):
        return ("NO_DEDUP",
                f"delta={delta}, id1={id1}, id2={id2} → M1 IGNORIRA header → "
                "dva zapisa. Ugasiti mutation-retry (kontingencija u planu).")
    return ("INCONCLUSIVE", f"Nejasna delta={delta} — pregledaj ručno.")


_EXAMPLE = {
    "_comment": "DEV vrijednosti! person/vehicle iz dev tenanta, termin daleko u budućnost.",
    "tenant_id": "",  # prazno = uzmi settings.MOBILITY_TENANT_ID
    "id_field": "Id",  # polje u POST odgovoru koje drži id novog zapisa
    "create": {
        "service": "vehiclemgt", "path": "/VehicleCalendar",
        "body": {
            "AssignedToId": "<DEV_person_id>", "AssigneeType": 1, "EntryType": 0,
            "VehicleId": "<DEV_vehicle_id>",
            "FromTime": "2030-01-01T09:00:00", "ToTime": "2030-01-01T15:00:00",
            "Description": "IDEMPOTENCY_PROBE_DELETE_ME"
        }
    },
    "count": {
        "service": "vehiclemgt", "path": "/VehicleCalendar",
        "query": {"Filter": "VehicleId(=)<DEV_vehicle_id>", "Rows": 200},
        "match": {"field": "Description", "value": "IDEMPOTENCY_PROBE_DELETE_ME"}
    },
    "delete": {"service": "vehiclemgt", "path": "/VehicleCalendar/{id}"}
}


async def _request(client, token, tenant, method, url, *, query=None,
                   body=None, idem_key=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/plain, application/json",
        "x-tenant": tenant,
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    resp = await client.request(method, url, params=query, json=body,
                                headers=headers, timeout=30.0)
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON body
        data = resp.text
    return resp.status_code, data


async def _count(client, token, tenant, base, cfg) -> int:
    c = cfg["count"]
    url = f"{base}/{c['service']}{c['path']}"
    status, data = await _request(client, token, tenant, "GET", url,
                                  query=c.get("query"))
    if status >= 400:
        logger.warning("  count GET status=%s — tretiram kao 0 (provjeri filter)", status)
        return 0
    return _count_matching(_extract_list(data), c.get("match"))


async def main_async(cfg: dict, arm: bool, confirm_host: str):
    from config import get_settings
    from services.token_manager import TokenManager
    import httpx

    s = get_settings()
    base = s.MOBILITY_API_URL.rstrip("/")
    tenant = cfg.get("tenant_id") or s.MOBILITY_TENANT_ID

    logger.info("=" * 60)
    logger.info("TARGET: %s   tenant=%s", base, tenant)
    logger.info("MODE:   %s", "ARMED (will WRITE)" if arm else "DRY-RUN (no writes)")
    logger.info("=" * 60)

    if arm:
        if not confirm_host or confirm_host not in base:
            logger.error(
                "ABORT: --confirm-host '%s' nije substring od MOBILITY_API_URL "
                "(%s). Sigurnosna brava protiv slučajnog prod-write-a.",
                confirm_host, base)
            sys.exit(2)

    create = cfg["create"]
    create_url = f"{base}/{create['service']}{create['path']}"
    method = create.get("method", "POST").upper()
    body = create["body"]
    id_field = cfg.get("id_field", "Id")
    idem_key = str(uuid.uuid4())

    logger.info("Create: %s %s", method, create_url)
    logger.info("Idempotency-Key (isti za oba poziva): %s", idem_key)
    logger.info("Body: %s", json.dumps(body, ensure_ascii=False))

    if not arm:
        logger.info("\nDRY-RUN: ne šaljem ništa. Pokreni s --arm --confirm-host <dev-substr>.")
        return

    token = await TokenManager().get_token()
    created_ids: list = []
    async with httpx.AsyncClient() as client:
        before = await _count(client, token, tenant, base, cfg)
        logger.info("\ncount BEFORE = %d", before)

        st1, d1 = await _request(client, token, tenant, method, create_url,
                                 body=body, idem_key=idem_key)
        id1 = d1.get(id_field) if isinstance(d1, dict) else None
        logger.info("POST #1 → status=%s id=%s", st1, id1)

        st2, d2 = await _request(client, token, tenant, method, create_url,
                                 body=body, idem_key=idem_key)
        id2 = d2.get(id_field) if isinstance(d2, dict) else None
        logger.info("POST #2 (isti key) → status=%s id=%s", st2, id2)

        after = await _count(client, token, tenant, base, cfg)
        logger.info("count AFTER  = %d", after)

        v, why = verdict(before, after, id1, id2,
                         200 <= st1 < 300, 200 <= st2 < 300)
        logger.info("\n%s\nVERDIKT: %s\n%s\n%s", "=" * 60, v, why, "=" * 60)

        # Cleanup — delete every distinct created id.
        for rid in {str(i) for i in (id1, id2) if i is not None}:
            created_ids.append(rid)
        if cfg.get("delete") and created_ids:
            d = cfg["delete"]
            for rid in created_ids:
                durl = f"{base}/{d['service']}{d['path'].replace('{id}', rid)}"
                dst, _ = await _request(client, token, tenant,
                                        d.get("method", "DELETE").upper(), durl,
                                        idem_key=str(uuid.uuid4()))
                logger.info("CLEANUP delete %s → status=%s", rid, dst)
        elif created_ids:
            logger.warning("Kreirani id-evi %s — NEMA delete configa, OČISTI RUČNO!",
                           created_ids)


def main():
    # Windows console defaults to cp1252 → Croatian diacritics crash on pipe.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 — older/odd streams
            pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="JSON config (vidi --print-example)")
    ap.add_argument("--arm", action="store_true",
                    help="STVARNO šalji write-ove (default: dry-run)")
    ap.add_argument("--confirm-host", default="",
                    help="Substring koji MORA biti u MOBILITY_API_URL (brava protiv proda)")
    ap.add_argument("--print-example", action="store_true",
                    help="Ispiši primjer config JSON-a i izađi")
    args = ap.parse_args()

    if args.print_example:
        print(json.dumps(_EXAMPLE, ensure_ascii=False, indent=2))
        return
    if not args.config:
        ap.error("treba --config <json> (ili --print-example)")

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    sys.path.insert(0, str(REPO_ROOT))
    asyncio.run(main_async(cfg, args.arm, args.confirm_host))


if __name__ == "__main__":
    main()
