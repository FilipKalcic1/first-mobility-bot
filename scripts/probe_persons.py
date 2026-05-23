"""One-off probe: REAL GET /Persons to see if `TenantRoles` is populated + its shape.

WHY — we found `TenantRoles` declared in the /Persons response schema
(config/tool_data.json:25346, processed_tool_registry.json:19509), but the
test mocks + config/tenants/_README.md:83 say role isn't exposed. A MOCK
can't settle this (it returns whatever we put in). Only a REAL call to
Damir's MobilityOne shows whether the live API actually fills TenantRoles
and what values it carries. This script makes that real call.

It reuses the SAME gateway path the bot uses (identity.py:421-427):
    GET /tenantmgt/Persons?Filter=Phone(=){phone}   (x-tenant = MOBILITY_TENANT_ID)

REQUIRES: populated .env (only MobilityOne creds + URL + MOBILITY_TENANT_ID;
NO Redis, NO Azure needed — the gateway runs with redis_client=None and
TokenManager just fetches a fresh OAuth token). Needs network to MobilityOne.
CANNOT run from the Claude sandbox (DNS blocked) — run locally or on bot.damir.com.

Usage:
    python scripts/probe_persons.py --phone +385951234567   # one known person
    python scripts/probe_persons.py --rows 5                 # sample any 5 persons
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _extract_persons(data) -> list:
    """Pull the list of person dicts out of whatever envelope M1 uses."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("Result", "Results", "data", "Data", "Items", "items", "value"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        # single object?
        if data.get("Id") or data.get("id"):
            return [data]
    return []


async def main_async(phone: str | None, rows: int) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from config import get_settings
    from services.api_gateway import APIGateway

    settings = get_settings()
    # No Redis: gateway with redis_client=None → TokenManager fetches a fresh
    # token (no cache). Fine for a one-off probe.
    gateway = APIGateway()

    if phone:
        query_params = {"Filter": f"Phone(=){phone}"}
        print(f"Calling GET /tenantmgt/Persons?Filter=Phone(=){phone}")
    else:
        query_params = {"Rows": str(rows)}
        print(f"Calling GET /tenantmgt/Persons?Rows={rows}")

    try:
        resp = await gateway.call(
            method="GET",
            service="tenantmgt",
            path="/Persons",
            query_params=query_params,
            tenant_id=settings.MOBILITY_TENANT_ID,
        )
    finally:
        try:
            await gateway.client.aclose()
        except Exception:  # noqa: BLE001
            pass

    print(f"\nsuccess={resp.success}  status={resp.status_code}")
    if not resp.success:
        print("Call failed — check .env creds / SWAGGER_URL / MOBILITY_TENANT_ID.")
        print("raw data:", str(resp.data)[:1000])
        return

    persons = _extract_persons(resp.data)
    print(f"persons returned: {len(persons)}")

    # The whole question: is TenantRoles present + what shape?
    for i, p in enumerate(persons[:5], 1):
        if not isinstance(p, dict):
            continue
        name = f"{p.get('FirstName', '')} {p.get('LastName', '')}".strip()
        print(f"\n--- person {i}: {name or '(no name)'} ---")
        print(f"  Id:          {p.get('Id') or p.get('id')}")
        print(f"  TenantId:    {p.get('TenantId') or p.get('tenantId')}")
        print(f"  TenantRoles: {json.dumps(p.get('TenantRoles'), ensure_ascii=False)}")
        print(f"  TenantUser:  {json.dumps(p.get('TenantUser'), ensure_ascii=False)}")
        has_role_field = "TenantRoles" in p
        print(f"  -> TenantRoles key present in response: {has_role_field}")

    # Dump the first person fully so we see ALL fields + nested shape.
    if persons and isinstance(persons[0], dict):
        print("\n=== full raw of person[0] (all fields, for shape) ===")
        print(json.dumps(persons[0], indent=2, ensure_ascii=False, default=str)[:6000])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--phone", type=str, default=None, help="E.164 phone to filter (a known manager/admin)")
    ap.add_argument("--rows", type=int, default=5, help="If no --phone: sample N persons")
    args = ap.parse_args()
    asyncio.run(main_async(args.phone, args.rows))


if __name__ == "__main__":
    main()
