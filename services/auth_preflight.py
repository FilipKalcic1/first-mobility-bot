"""Auth preflight — scope introspection + route probing (PP1).

Turns "will auth work?" from a runtime surprise (403 in production — the
exact failure the 2026-05-30 live test hit: "bot nema ovlasti") into a
startup FACT, using two independent layers:

  LAYER 1 — token introspection (Filip's insight: the scope is VISIBLE in
      the token). We request a scope (token_manager.py:171) but never used
      to check what the token actually GRANTED. This layer decodes our own
      JWT's payload (no signature verification needed — it is our token,
      we only read claims) and diffs granted vs required scopes.

  LAYER 2 — route probing (empirical; works even if the IdentityServer
      issues opaque tokens or coarse scopes). Fire a benign GET per route
      and map name → HTTP status. 401/403 = auth problem, loudly reported.

Neither layer ever raises — a broken preflight must not take down the
worker. Default is LOG-ONLY; set AUTH_PREFLIGHT_STRICT=1 to treat failures
as fatal (readiness gate in CI/deploy). Strict mode demands POSITIVE
verification (report.verified) — a token-fetch error or a probe run that
never got a real HTTP answer (dead credentials often surface as status
0/-1, not 401) must NOT pass the gate.

Env knobs:
  AUTH_PREFLIGHT=0            disable entirely (default: enabled)
  AUTH_PREFLIGHT_STRICT=1     refuse startup unless report.ok AND
                              report.verified (fail-closed gate)
  MOBILITY_REQUIRED_SCOPES    space/comma separated scope names the token
                              MUST carry (empty/unset → skip Layer 1 diff;
                              Layer 2 still runs)

Wire-up:
  worker.py startup            → run_startup_preflight(gateway, settings)
  scripts/verify_production_readiness.py → verify_auth_preflight()
Future (F1): probe list derives from config/actions.json via
  probe_routes_from_actions() so every enabled action is scope-checked
  before it is ever offered to a user.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layer 1 — token introspection
# ---------------------------------------------------------------------------


def read_token_claims(access_token: str) -> dict:
    """Decode the payload segment of a JWT and return its claims.

    No signature verification — we are introspecting OUR OWN token to read
    what the IdentityServer granted us, not validating a third party's.

    Returns {} for anything that is not a decodable JWT (opaque tokens,
    malformed input). Callers must treat {} as "introspection unavailable",
    NOT as "no scopes granted" — Layer 2 covers that case empirically.
    """
    if not access_token or access_token.count(".") < 2:
        return {}
    payload_b64 = access_token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)  # restore base64 padding
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return {}
    # A JWT payload can be valid JSON that is NOT an object ("x", [...], 42);
    # the contract here is "dict or {}" — never leak a non-dict to callers.
    return claims if isinstance(claims, dict) else {}


def granted_scopes(claims: dict) -> set:
    """Extract the scope set from JWT claims.

    OAuth2 serialises `scope` as a space-separated string; some servers
    (IdentityServer included) emit a JSON list instead. Accept both.
    """
    raw = claims.get("scope") or claims.get("scp") or ""
    if isinstance(raw, str):
        return {s for s in raw.split() if s}
    if isinstance(raw, (list, tuple)):
        return {str(s) for s in raw if s}
    return set()


def required_scopes_from_env() -> Optional[set]:
    """Parse MOBILITY_REQUIRED_SCOPES ("a b,c" → {a,b,c}). None = unset →
    skip the scope diff (we don't yet know M1's scope taxonomy; the route
    probe still runs and is authoritative)."""
    raw = (os.environ.get("MOBILITY_REQUIRED_SCOPES") or "").replace(",", " ")
    scopes = {s for s in raw.split() if s}
    return scopes or None


# ---------------------------------------------------------------------------
# Layer 2 — route probing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeRoute:
    """One benign read to fire during preflight."""
    name: str
    service: str            # gateway service segment ("tenantmgt", "" for /actions)
    path: str                # "/Persons" or "/actions/list-trips"
    params: Optional[dict] = None


def default_probe_routes() -> list:
    """Today's minimal probe set: one cheap read per M1 service the bot
    depends on at boot. Grows to per-action probes in F1 (see
    probe_routes_from_actions)."""
    return [
        ProbeRoute(name="persons", service="tenantmgt", path="/Persons",
                   params={"Rows": 1}),
    ]


def probe_routes_from_actions(actions: dict) -> list:
    """F1: derive one probe per enabled action from config/actions.json.
    GET actions probe directly; mutations probe their route with a benign
    method the backend agrees to (part of the §8 contract — until agreed,
    only GET actions are probed). Entries without a usable path are skipped
    — a ProbeRoute(path="") would probe the service root and poison the
    verdict."""
    routes = []
    for name, spec in (actions or {}).items():
        execution = spec.get("execution") if isinstance(spec, dict) else None
        if not isinstance(execution, dict):
            continue
        path = execution.get("action") or ""
        if path and (execution.get("method") or "").upper() == "GET":
            routes.append(ProbeRoute(name=name, service="", path=path))
    return routes


async def probe(gateway, routes: list, tenant_id: str) -> dict:
    """Fire each probe; return {name: http_status}. Status -1 = transport
    error (network/DNS — NOT an auth verdict). Never raises."""
    results: dict = {}
    for route in routes:
        try:
            resp = await gateway.call(
                method="GET",
                service=route.service,
                path=route.path,
                query_params=dict(route.params) if route.params else None,
                tenant_id=tenant_id,
            )
            results[route.name] = int(resp.status_code or 0)
        except Exception as e:  # noqa: BLE001 — preflight must never crash caller
            logger.warning("auth preflight probe %s failed: %s", route.name, e)
            results[route.name] = -1
    return results


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class PreflightReport:
    ok: bool
    granted: set = field(default_factory=set)
    missing_scopes: list = field(default_factory=list)
    route_status: dict = field(default_factory=dict)
    forbidden_routes: list = field(default_factory=list)
    introspection_available: bool = False
    error: Optional[str] = None
    # POSITIVE evidence obtained: token fetch succeeded AND (if probes ran)
    # at least one route answered with a real HTTP status. ok=True with
    # verified=False means "no proven problem, but no proof it works either".
    verified: bool = False


async def run_preflight(
    token_manager,
    gateway,
    tenant_id: str,
    *,
    required: Optional[set] = None,
    routes: Optional[list] = None,
) -> PreflightReport:
    """Run both layers; never raises.

    ok=False iff an AUTH problem is proven: a required scope is missing
    (Layer 1) or a probed route answered 401/403 (Layer 2). Transport
    errors / 5xx / 404 are NOT auth verdicts and don't fail preflight —
    they surface through the normal circuit-breaker path instead.

    verified=True iff POSITIVE evidence was obtained: the token was fetched
    without error AND, when probes ran, at least one returned a real HTTP
    status (>=200; a 403 IS real evidence — of a real problem). Strict
    callers must require ok AND verified, otherwise dead credentials
    (status 0 / transport errors everywhere) would pass the gate.
    """
    report = PreflightReport(ok=True)

    # Layer 1 — introspection
    try:
        token = await token_manager.get_token()
        claims = read_token_claims(token)
        report.introspection_available = bool(claims)
        report.granted = granted_scopes(claims)
        if required and report.introspection_available:
            report.missing_scopes = sorted(required - report.granted)
    except Exception as e:  # noqa: BLE001
        report.error = f"token_fetch:{type(e).__name__}"
        logger.warning("auth preflight token fetch failed: %s", e)

    # Layer 2 — probing
    if gateway is not None:
        report.route_status = await probe(
            gateway, routes or default_probe_routes(), tenant_id,
        )
        report.forbidden_routes = sorted(
            name for name, status in report.route_status.items()
            if status in (401, 403)
        )

    report.ok = not report.missing_scopes and not report.forbidden_routes
    statuses = list(report.route_status.values())
    report.verified = report.error is None and (
        not statuses or any(s >= 200 for s in statuses)
    )
    return report


def log_report(report: PreflightReport) -> None:
    """One loud, grep-able line per outcome (`auth_preflight` marker)."""
    if report.ok and report.verified:
        logger.info(
            "auth_preflight OK granted=%d introspection=%s routes=%s",
            len(report.granted), report.introspection_available,
            report.route_status,
        )
        return
    if report.ok:
        logger.warning(
            "auth_preflight NEVERIFICIRAN — nema dokazanog auth problema, "
            "ali ni pozitivne potvrde da auth radi (error=%s routes=%s); "
            "u strict modu ovo NE prolazi gate",
            report.error, report.route_status,
        )
        return
    logger.error(
        "auth_preflight FAILED missing_scopes=%s forbidden_routes=%s "
        "routes=%s error=%s — bez ovih ovlasti pozivi će vraćati 403 "
        "(isti failure kao živi test 2026-05-30)",
        report.missing_scopes, report.forbidden_routes,
        report.route_status, report.error,
    )


async def run_startup_preflight(gateway, settings) -> Optional[PreflightReport]:
    """Worker-startup convenience: honors AUTH_PREFLIGHT / _STRICT env.

    Returns the report (None when disabled). Raises RuntimeError ONLY in
    strict mode — when the report is not ok, when auth could not be
    POSITIVELY verified (report.verified False: dead credentials, IdP down,
    no real HTTP answer from any probe), or when the preflight itself
    errored. Log-only mode never raises.
    """
    if (os.environ.get("AUTH_PREFLIGHT") or "1").strip() in ("0", "false", "no"):
        return None
    strict = os.environ.get("AUTH_PREFLIGHT_STRICT") == "1"
    try:
        report = await run_preflight(
            gateway.token_manager, gateway,
            tenant_id=settings.MOBILITY_TENANT_ID,
            required=required_scopes_from_env(),
        )
    except Exception as e:  # noqa: BLE001 — belt and braces
        if strict:
            # Strict = fail-closed: an unverifiable preflight must not be
            # silently skipped (that is exactly the fails-open bug).
            raise RuntimeError(f"auth preflight errored (strict): {e}") from e
        logger.warning("auth preflight unexpected error (skipping): %s", e)
        return None
    log_report(report)
    if strict and (not report.ok or not report.verified):
        raise RuntimeError(
            "auth preflight failed strict gate: "
            f"ok={report.ok} verified={report.verified} "
            f"missing_scopes={report.missing_scopes} "
            f"forbidden_routes={report.forbidden_routes} error={report.error}"
        )
    return report
