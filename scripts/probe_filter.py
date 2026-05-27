"""One-off probe: does MobilityOne accept `Field(=)Value` filter syntax, and
which FIELD name filters a vehicle by registration?

WHY — the filter-scope feature builds `LicencePlate(=)DA533` and sends it as
the `Filter` query param. `LicencePlate` came from get_Vehicles RESPONSE keys
(output_keys) — but response fields are NOT necessarily the names the API
accepts in a Filter. A MOCK can't settle this. Only a real GET against Damir's
MobilityOne shows (a) whether `Field(operator)Value` is the right syntax and
(b) which field name actually filters by plate. This script makes that call,
trying the primary field then a list of fallback candidates.

It reuses the SAME gateway path the bot uses (executor → api_gateway):
    GET /vehiclemgt/Vehicles?Filter=<field>(=)<plate>   (x-tenant = MOBILITY_TENANT_ID)

REQUIRES: populated .env (MobilityOne creds + URL + MOBILITY_TENANT_ID; NO
Redis, NO Azure — gateway runs with no cache, TokenManager fetches a fresh
OAuth token). Needs network to MobilityOne. CANNOT run from the Claude sandbox
(DNS blocked) — run locally or on bot.damir.com.

Usage:
    python scripts/probe_filter.py --plate DA533
    python scripts/probe_filter.py --plate DA533 --service vehiclemgt --path /MonthlyMileages
    python scripts/probe_filter.py --plate DA533 --fields LicencePlate Plate RegistrationNumber

Read the output: the FIRST field that returns success=True with rows is the
one to put in config/filter_fields.json. If NONE work, the syntax/operator may
differ — check the Swagger field-level metadata (x-filterable or similar).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Filip's candidate field names (2026-05-23), primary first.
_DEFAULT_FIELDS = [
    "LicencePlate",        # from get_Vehicles output_keys (primary guess)
    "Vehicle.LicencePlate",
    "VehicleLicencePlate",
    "RegistrationNumber",
    "Plate",
    "LicensePlate",        # US spelling, just in case
]


def _extract_rows(data) -> list:
    """Pull the list of rows out of whatever envelope M1 uses."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("Result", "Results", "data", "Data", "Items", "items", "value"):
            v = data.get(key)
            if isinstance(v, list):
                return v
        if data.get("Id") or data.get("id"):
            return [data]
    return []


def _snippet(data) -> str:
    import json
    s = json.dumps(data, ensure_ascii=False, default=str)
    return s if len(s) <= 300 else s[:300] + "…"


async def main_async(plate: str, service: str, path: str, fields: list[str]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from config import get_settings
    from services.api_gateway import APIGateway

    settings = get_settings()
    gateway = APIGateway()

    print(f"Tenant: {settings.MOBILITY_TENANT_ID}")
    print(f"Target: GET /{service}{path}   plate={plate}")
    print(f"Trying {len(fields)} candidate field(s), '(=)' operator.\n")

    winner = None
    try:
        for field in fields:
            filt = f"{field}(=){plate}"
            try:
                resp = await gateway.call(
                    method="GET", service=service, path=path,
                    query_params={"Filter": filt},
                    tenant_id=settings.MOBILITY_TENANT_ID,
                )
            except Exception as e:  # noqa: BLE001
                print(f"  Filter={filt:<32} EXC {type(e).__name__}: {e}")
                continue

            rows = _extract_rows(resp.data) if resp.success else []
            status = getattr(resp, "status_code", "?")
            mark = "OK " if (resp.success and rows) else ("200-empty" if resp.success else "FAIL")
            print(f"  Filter={filt:<32} [{mark}] status={status} rows={len(rows)}")
            if not resp.success:
                print(f"      body: {_snippet(resp.data)}")
            if resp.success and rows and winner is None:
                winner = field
                print(f"      sample: {_snippet(rows[0])}")
    finally:
        try:
            await gateway.client.aclose()
        except Exception:  # noqa: BLE001
            pass

    print()
    if winner:
        print(f"WINNER: Filter field = '{winner}'  →  put this in config/filter_fields.json")
        print(f'  {{"registration": "{winner}"}}')
    else:
        print("NO candidate returned rows. Either the plate has no match, the")
        print("operator differs (try '(eq)'/'(contains)'), or the field name is")
        print("something else — check Swagger field-level filter metadata.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Probe MobilityOne Filter field for plate.")
    ap.add_argument("--plate", default="DA533", help="registration to filter by")
    ap.add_argument("--service", default="vehiclemgt")
    ap.add_argument("--path", default="/Vehicles")
    ap.add_argument("--fields", nargs="+", default=_DEFAULT_FIELDS,
                    help="candidate filter field names (primary first)")
    args = ap.parse_args()
    asyncio.run(main_async(args.plate, args.service, args.path, args.fields))


if __name__ == "__main__":
    main()
