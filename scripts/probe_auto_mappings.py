"""Comprehensive read-only probe: provjeri SVE auto-injected `context` params
(180 instanci / 19 imena), ne samo sumnjive.

WHY (Filip 2026-05-24): bot auto-ubacuje tvoju identity-vrijednost u params
tagirane dependency_source=context. To je ispravno SAMO ako polje zaista znači
"korisnikov vlastiti X". Imena to ne dokazuju — pravi zapisi da. Klasifikacija
je heuristika koja je već griješila (55 demotano), pa svaki mapping treba
provjeru, ne nagađanje.

Tri metode pokrivaju svih 19 context-imena:
  1) SELF-CHECK na MasterData — dohvati MOJ zapis (--phone) i provjeri je li
     Id==moj vehicle, DriverId==moj person, TenantId/CompanyId/OrgUnitId==moji.
     (Pokriva anchor-e: VehicleId, PersonId/DriverId, TenantId, CompanyId, OrgUnitId.)
  2) WITHIN-RECORD SIBLING — u zapisima koji imaju i anchor i qualified polje,
     usporedi (npr. VehiclePoolId vs VehicleId u istom Case-u). Razlikuju li se
     ⇒ različit entitet ⇒ auto-inject tvog id-a je KRIV.
  3) VARIATION — person-polja bez in-record anchora (AssignedToId, CreatedBy,
     UserId): variraju li po zapisima ⇒ per-record polje, auto-inject je točan
     samo kad si baš ti.
  4) Ne-probabilna iz lista (vehicleId/personId lc, personIdOrEmail, companyId
     lc) — nijedan GET-list ih ne vraća ⇒ provjeri semantiku na toolu/backendu.

PURE READ — samo GET, nijedan write/mutacija. Reuse bot gatewaya (isti
auth/URL/headeri kao produkcija). REQUIRES populiran .env (M1 creds + URL +
MOBILITY_TENANT_ID), mrežu do M1, i OTKLJUČAN OAuth token. NE radi iz Claude
sandboxa (DNS blokiran) — pokreni lokalno / na bot.damir.com.

Usage:
    python scripts/probe_auto_mappings.py --phone 0915087196
    python scripts/probe_auto_mappings.py --phone 0915087196 --rows 20
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _rows(data) -> list:
    """Izvuci listu zapisa iz bilo kojeg M1 envelope-a."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("Result", "Results", "data", "Data", "Items", "items", "value"):
            if isinstance(data.get(k), list):
                return data[k]
        if data.get("Id") or data.get("id"):
            return [data]
    return []


def _nsn(phone: str) -> str:
    """National significant number: digits, skini 00/385 i vodeću 0."""
    dig = re.sub(r"\D", "", phone or "")
    if dig.startswith("00"):
        dig = dig[2:]
    if dig.startswith("385"):
        dig = dig[3:]
    return dig.lstrip("0")


async def _get(gw, tenant, service, path, qp):
    try:
        return await gw.call(method="GET", service=service, path=path,
                             query_params=qp, tenant_id=tenant)
    except Exception as e:  # noqa: BLE001
        print(f"   EXC {type(e).__name__}: {e}")
        return None


# within-record sibling: (service, path, anchor, [qualified varijante])
SIBLING = [
    ("vehiclemgt", "/Cases", "VehicleId", ["VehicleAssetId", "VehiclePoolId"]),
    ("tenantmgt", "/OrgUnits", "Id", ["ParentOrgUnitId"]),
    ("tenantmgt", "/Partners", "TenantId", ["LinkedTenantId"]),
    ("tenantmgt", "/Tenants", "Id", ["TenantOwnerId"]),
    ("vehiclemgt", "/LatestVehicleCalendar", "VehicleId", ["VehicleExternalId"]),
]

# variation: person-polja bez in-record anchora
VARIATION = [
    ("vehiclemgt", "/LatestVehicleCalendar", ["AssignedToId"]),
    ("tenantmgt", "/DashboardItems", ["CreatedBy"]),
    ("tenantmgt", "/Persons", ["UserId", "Id"]),
    ("tenantmgt", "/LatestPersonPeriodicActivities", ["PersonId"]),
]

# Filter-test (V1 generički / V2 operatori / V3 computed) — svi na get_Vehicles.
# (label, filter_expr). 200 = polje/operator prihvaćen; 500 = nepoznato/ne-filtrabilno.
FILTER_TESTS = [
    ("control LicencePlate(contains)", "LicencePlate(contains)D"),
    ("V1 VIN(contains)", "VIN(contains)1"),
    ("V1 Manufacturer(contains)", "Manufacturer(contains)a"),
    ("V1 Model(contains)", "Model(contains)a"),
    ("V1 GeneralStatusId(=)", "GeneralStatusId(=)1"),
    ("V3 FullVehicleName(contains)", "FullVehicleName(contains)a"),
    ("V3 LicencePlateNorm(contains)", "LicencePlateNorm(contains)a"),
    ("V2 LastMileage(>)", "LastMileage(>)0"),
    ("V2 LastMileage(>=)", "LastMileage(>=)0"),
    ("V2 LastMileage(<=)", "LastMileage(<=)999999999"),
    ("V2 OR join", "Manufacturer(contains)a or Manufacturer(contains)b"),
]


async def main_async(phone: str, rows: int) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from config import get_settings
    from services.api_gateway import APIGateway

    s = get_settings()
    gw = APIGateway()
    tenant = s.MOBILITY_TENANT_ID
    me_id = {"tenant_id": tenant, "person_id": None, "vehicle_id": None,
             "company_id": None, "orgunit_id": None}
    print(f"Tenant: {tenant}\nPURE READ — sve context mapinge (19 imena / 180 instanci)")
    try:
        # 1) SELF iz /Persons + SELF-CHECK na MasterData (ground truth) ---------
        if phone:
            nsn = _nsn(phone)
            r = await _get(gw, tenant, "tenantmgt", "/Persons",
                           {"Filter": f"Phone(contains){nsn}", "Rows": 5})
            recs = _rows(r.data) if r and r.success else []
            me = next((p for p in recs if _nsn(str(p.get("Phone"))) == nsn),
                      recs[0] if recs else None)
            if me:
                me_id.update(person_id=me.get("Id"), company_id=me.get("CompanyId"),
                             orgunit_id=me.get("OrgUnitId"),
                             tenant_id=me.get("TenantId") or tenant)
                print(f"\nSELF /Persons: {me_id}")
                md = await _get(gw, tenant, "automation", "/MasterData",
                                {"personId": me_id["person_id"]})
                mrec = (_rows(md.data) or [None])[0] if md and md.success else None
                if mrec:
                    me_id["vehicle_id"] = mrec.get("VehicleId") or mrec.get("Id")
                    print("--- SELF-CHECK MasterData (moj zapis = ground truth) ---")
                    for fld, key in [("Id", "vehicle_id"), ("DriverId", "person_id"),
                                     ("TenantId", "tenant_id"), ("CompanyId", "company_id"),
                                     ("OrgUnitId", "orgunit_id")]:
                        v = mrec.get(fld)
                        ok = v is not None and str(v) == str(me_id[key])
                        print(f"   {fld}={v}  vs moj {key}={me_id[key]}  "
                              f"⇒ {'OK (auto-inject točan)' if ok else 'RAZLIKA — provjeri'}")
                else:
                    print("   MasterData za mene: nema zapisa (preskačem self-check)")
            else:
                print("SELF: nije razriješen iz /Persons (nastavljam bez self-checka)")
        else:
            print("\n(bez --phone → preskačem SELF-CHECK; pokreni s --phone <broj> za anchor provjeru)")

        # 2) WITHIN-RECORD SIBLING (definitivno za qualified varijante) ---------
        for svc, path, anchor, quals in SIBLING:
            print(f"\n--- sibling: {quals} vs {anchor}  (GET /{svc}{path}) ---")
            r = await _get(gw, tenant, svc, path, {"Rows": rows})
            if not r or not r.success:
                print(f"   FAIL status={getattr(r, 'status_code', '?')}")
                continue
            recs = _rows(r.data)[:rows]
            if not recs:
                print("   0 zapisa")
                continue
            for q in quals:
                d = sm = n = 0
                for rec in recs:
                    a, qv = rec.get(anchor), rec.get(q)
                    if qv in (None, ""):
                        n += 1
                    elif str(qv) == str(a):
                        sm += 1
                    else:
                        d += 1
                if d > sm:
                    verdict = "RAZLIČIT ENTITET → auto-inject KRIV → demotaj"
                elif sm and not d:
                    verdict = "ISTI kao anchor → OK → ostavi"
                elif n >= len(recs):
                    verdict = "uglavnom prazno → ne injectati svejedno"
                else:
                    verdict = "miješano — pogledaj ručno"
                print(f"   {q}: differ={d} same={sm} null={n}  ⇒ {verdict}")

        # 3) VARIATION (person-polja bez in-record anchora) --------------------
        for svc, path, flds in VARIATION:
            print(f"\n--- varijacija {flds}  (GET /{svc}{path}) ---")
            r = await _get(gw, tenant, svc, path, {"Rows": rows})
            if not r or not r.success:
                print(f"   FAIL status={getattr(r, 'status_code', '?')}")
                continue
            recs = _rows(r.data)[:rows]
            if not recs:
                print("   0 zapisa")
                continue
            for f in flds:
                vals = {str(rec.get(f)) for rec in recs if rec.get(f) not in (None, "")}
                note = ("VARIRA (per-record polje; auto-inject točan samo kad si baš ti)"
                        if len(vals) > 1 else "konstantno (možda = tvoj)")
                print(f"   {f}: {len(vals)} različitih u {len(recs)} zapisa  ⇒ {note}")

        # 4) ne-probabilna iz GET-lista ----------------------------------------
        print("\n--- NE mogu provjeriti iz GET-lista (nijedan ih ne vraća; "
              "vjerojatno path/query param) ---")
        print("   vehicleId(lc), personId(lc), personIdOrEmail, companyId(lc)")
        print("   → provjeri semantiku na konkretnom toolu / backend, ili 422-probe.")

        # 5) FILTER-TEST (V1 generički / V2 operatori / V3 computed) ------------
        print("\n========== FILTER-TEST  (GET /vehiclemgt/Vehicles?Filter=…) ==========")
        for label, expr in FILTER_TESTS:
            r = await _get(gw, tenant, "vehiclemgt", "/Vehicles", {"Filter": expr, "Rows": 1})
            if r is None:
                print(f"   {label:32} [{expr:46}] EXC")
                continue
            st = getattr(r, "status_code", "?")
            if r.success:
                verdict = "PRIHVAĆEN (polje/operator radi)"
            elif str(st) == "500":
                verdict = "ODBIJENO (nepoznato polje / ne-filtrabilno)"
            else:
                verdict = f"ODBIJEN (status {st} — operator/sintaksa?)"
            nrows = len(_rows(r.data)) if r.success else 0
            print(f"   {label:32} [{expr:46}] status={st} rows={nrows} ⇒ {verdict}")
        print("   Tumačenje: control mora PROĆI. Ako V1 polja PROĐU → GENERIČKI filter "
              "→ output_keys = popis polja (String-MVP buildable bez backend popisa). "
              "Operatori koji prođu = naš set (V2). Computed (V3) → vidi prolaze li.")
    finally:
        try:
            await gw.client.aclose()
        except Exception:  # noqa: BLE001
            pass
    print("\nProtumači: SELF-CHECK 'RAZLIKA' / sibling 'RAZLIČIT ENTITET' / 'VARIRA' "
          "⇒ kandidat za demote. 'OK'/'ISTI'/'konstantno==tvoj' ⇒ ostavi. "
          "Demote tek nakon ovog outputa — bez nagađanja.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Comprehensive read-only probe of ALL context params.")
    ap.add_argument("--phone", default="", help="test user phone za SELF-CHECK (npr. 0915087196)")
    ap.add_argument("--rows", type=int, default=15)
    args = ap.parse_args()
    asyncio.run(main_async(args.phone, args.rows))


if __name__ == "__main__":
    main()
