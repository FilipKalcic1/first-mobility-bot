"""Auth preflight (PP1): token scope introspection + route probing.

The guarantee this module provides: a missing OAuth grant is a STARTUP
fact, not a production 403 (the exact failure the 2026-05-30 live test
hit). Tests cover both layers + orchestration + env gating.
"""
from __future__ import annotations

import base64
import json

import pytest

from services.auth_preflight import (
    PreflightReport,
    ProbeRoute,
    default_probe_routes,
    granted_scopes,
    log_report,
    probe,
    probe_routes_from_actions,
    read_token_claims,
    required_scopes_from_env,
    run_preflight,
    run_startup_preflight,
)


def _jwt(claims: dict) -> str:
    """Minimal unsigned JWT-shaped token (header.payload.sig)."""
    def seg(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj).encode()
        ).decode().rstrip("=")
    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.fakesig"


# ---------------------------------------------------------------------------
# Layer 1 — introspection
# ---------------------------------------------------------------------------


def test_read_claims_from_valid_jwt():
    token = _jwt({"scope": "fleet.read fleet.write", "aud": "m1"})
    claims = read_token_claims(token)
    assert claims["aud"] == "m1"
    assert claims["scope"] == "fleet.read fleet.write"


def test_read_claims_handles_base64_padding():
    # Payload lengths that need 1 and 2 padding chars must both decode.
    for filler in ("x", "xy", "xyz"):
        token = _jwt({"scope": filler})
        assert read_token_claims(token)["scope"] == filler


def test_opaque_token_returns_empty_not_crash():
    assert read_token_claims("opaque-reference-token") == {}
    assert read_token_claims("") == {}
    assert read_token_claims("a.!!!notb64!!!.c") == {}


def test_non_dict_json_payload_returns_empty_dict():
    """Payload that decodes to valid JSON but NOT an object must yield {} —
    otherwise granted_scopes() crashes on .get and masks as token_fetch."""
    def seg_raw(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    for payload in (b'["a","b"]', b'"just-a-string"', b"42", b"null", b"true"):
        token = f"{seg_raw(b'{}')}.{seg_raw(payload)}.sig"
        claims = read_token_claims(token)
        assert claims == {}, f"payload {payload!r} leaked non-dict"
        assert granted_scopes(claims) == set()


def test_granted_scopes_space_separated_string():
    assert granted_scopes({"scope": "a b c"}) == {"a", "b", "c"}


def test_granted_scopes_list_form():
    # IdentityServer commonly emits a JSON array.
    assert granted_scopes({"scope": ["a", "b"]}) == {"a", "b"}


def test_granted_scopes_scp_fallback_and_empty():
    assert granted_scopes({"scp": "x"}) == {"x"}
    assert granted_scopes({}) == set()


def test_required_scopes_env_parsing(monkeypatch):
    monkeypatch.setenv("MOBILITY_REQUIRED_SCOPES", "fleet.read, fleet.write actions")
    assert required_scopes_from_env() == {"fleet.read", "fleet.write", "actions"}
    monkeypatch.setenv("MOBILITY_REQUIRED_SCOPES", "")
    assert required_scopes_from_env() is None


# ---------------------------------------------------------------------------
# Layer 2 — probing
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeGateway:
    """Scripted per-path statuses; records calls."""
    def __init__(self, statuses: dict, raise_on: str = ""):
        self._statuses = statuses
        self._raise_on = raise_on
        self.calls = []

    async def call(self, *, method, service, path, query_params=None, tenant_id=None):
        self.calls.append({"method": method, "service": service, "path": path,
                           "params": query_params, "tenant": tenant_id})
        if path == self._raise_on:
            raise ConnectionError("network down")
        return _Resp(self._statuses.get(path, 200))


async def test_probe_maps_statuses_and_survives_exceptions():
    gw = FakeGateway({"/a": 200, "/b": 403}, raise_on="/c")
    routes = [ProbeRoute("ok", "", "/a"), ProbeRoute("denied", "", "/b"),
              ProbeRoute("down", "", "/c")]
    result = await probe(gw, routes, tenant_id="t1")
    assert result == {"ok": 200, "denied": 403, "down": -1}


async def test_probe_passes_tenant_and_params():
    gw = FakeGateway({"/Persons": 200})
    await probe(gw, default_probe_routes(), tenant_id="t-xyz")
    assert gw.calls[0]["tenant"] == "t-xyz"
    assert gw.calls[0]["params"] == {"Rows": 1}
    assert gw.calls[0]["method"] == "GET"


def test_probe_routes_from_actions_only_get():
    actions = {
        "list_trips": {"execution": {"method": "GET", "action": "/actions/list-trips"}},
        "report_incident": {"execution": {"method": "POST", "action": "/actions/report-incident"}},
    }
    routes = probe_routes_from_actions(actions)
    assert [r.name for r in routes] == ["list_trips"]
    assert routes[0].path == "/actions/list-trips"


def test_probe_routes_skip_entries_without_usable_path():
    """GET action bez pravog patha ne smije postati ProbeRoute(path="") —
    probe na service root bi trovao verdikt (403 roota != 403 akcije)."""
    actions = {
        "no_exec": {},
        "exec_none": {"execution": None},
        "no_path": {"execution": {"method": "GET"}},
        "empty_path": {"execution": {"method": "GET", "action": ""}},
        "spec_not_dict": "malformed",
        "good": {"execution": {"method": "GET", "action": "/actions/list-trips"}},
    }
    routes = probe_routes_from_actions(actions)
    assert [(r.name, r.path) for r in routes] == [("good", "/actions/list-trips")]
    assert probe_routes_from_actions(None) == []
    assert probe_routes_from_actions({}) == []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class FakeTokenManager:
    def __init__(self, token):
        self._token = token

    async def get_token(self):
        if isinstance(self._token, Exception):
            raise self._token
        return self._token


async def test_preflight_ok_when_scopes_granted_and_routes_open():
    tm = FakeTokenManager(_jwt({"scope": "fleet.read fleet.write"}))
    gw = FakeGateway({"/Persons": 200})
    report = await run_preflight(tm, gw, "t1", required={"fleet.read"})
    assert report.ok is True
    assert report.missing_scopes == []
    assert report.forbidden_routes == []
    assert report.introspection_available is True
    assert report.verified is True


async def test_preflight_fails_on_missing_scope():
    tm = FakeTokenManager(_jwt({"scope": "fleet.read"}))
    gw = FakeGateway({"/Persons": 200})
    report = await run_preflight(tm, gw, "t1", required={"fleet.read", "actions.write"})
    assert report.ok is False
    assert report.missing_scopes == ["actions.write"]


async def test_preflight_fails_on_403_route():
    """The 2026-05-30 live-test failure mode, caught at startup."""
    tm = FakeTokenManager(_jwt({"scope": "fleet.read"}))
    gw = FakeGateway({"/Persons": 403})
    report = await run_preflight(tm, gw, "t1")
    assert report.ok is False
    assert report.forbidden_routes == ["persons"]
    assert report.verified is True  # 403 JE pravi odgovor — dokaz problema


async def test_preflight_opaque_token_skips_scope_diff_but_probes():
    """Opaque token → introspection unavailable → missing REQUIRED scopes
    must NOT be reported (we cannot know); probe layer still authoritative."""
    tm = FakeTokenManager("opaque-token")
    gw = FakeGateway({"/Persons": 200})
    report = await run_preflight(tm, gw, "t1", required={"anything"})
    assert report.introspection_available is False
    assert report.missing_scopes == []
    assert report.ok is True


async def test_preflight_transport_error_is_not_auth_verdict():
    """Network down (-1) / 5xx / 404 must not fail preflight — those are
    circuit-breaker territory, not missing grants."""
    tm = FakeTokenManager(_jwt({"scope": "s"}))
    gw = FakeGateway({"/Persons": 500}, raise_on="")
    report = await run_preflight(tm, gw, "t1")
    assert report.ok is True


async def test_preflight_token_fetch_failure_never_raises():
    tm = FakeTokenManager(RuntimeError("idp down"))
    gw = FakeGateway({"/Persons": 200})
    report = await run_preflight(tm, gw, "t1", required={"x"})
    assert report.error.startswith("token_fetch:")
    assert report.ok is True  # no PROVEN auth problem...
    assert report.verified is False  # ...but nothing verified either
    log_report(report)  # smoke: must not raise


async def test_verified_requires_a_real_http_answer():
    """Mrtvi kredencijali se često manifestiraju kao status 0 / transport
    error, NE kao 401 — ok ostaje True (ništa dokazano), ali verified mora
    biti False da strict gate ne propusti neverificiran auth."""
    tm = FakeTokenManager(_jwt({"scope": "s"}))

    report = await run_preflight(tm, FakeGateway({"/Persons": 0}), "t1")
    assert report.ok is True
    assert report.verified is False

    report = await run_preflight(
        tm, FakeGateway({}, raise_on="/Persons"), "t1")
    assert report.ok is True
    assert report.verified is False

    # Miješano: bar jedan pravi odgovor = verificirano.
    gw = FakeGateway({"/a": 200}, raise_on="/b")
    routes = [ProbeRoute("a", "", "/a"), ProbeRoute("b", "", "/b")]
    report = await run_preflight(tm, gw, "t1", routes=routes)
    assert report.verified is True


async def test_preflight_gateway_none_runs_layer1_only():
    """Layer-1-only pozivatelj (bez gatewaya) — probe sloj se preskače,
    ponašanje pinano: nema route_statusa, nema lažnih forbidden ruta."""
    tm = FakeTokenManager(_jwt({"scope": "a"}))
    report = await run_preflight(tm, None, "t1", required={"a"})
    assert report.ok is True
    assert report.route_status == {}
    assert report.forbidden_routes == []
    assert report.verified is True  # bez traženih probea Layer 1 je dovoljan


# ---------------------------------------------------------------------------
# Startup wrapper (env gating)
# ---------------------------------------------------------------------------


class _GW(FakeGateway):
    def __init__(self, statuses, token):
        super().__init__(statuses)
        self.token_manager = FakeTokenManager(token)


class _Settings:
    MOBILITY_TENANT_ID = "t-env"


def _clean_env(monkeypatch):
    """Startup testovi ne smiju ovisiti o ambijentalnom env-u — naslijeđeni
    AUTH_PREFLIGHT=0 / STRICT=1 / MOBILITY_REQUIRED_SCOPES bi ih rušili."""
    for var in ("AUTH_PREFLIGHT", "AUTH_PREFLIGHT_STRICT",
                "MOBILITY_REQUIRED_SCOPES"):
        monkeypatch.delenv(var, raising=False)


async def test_startup_disabled_via_env(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("AUTH_PREFLIGHT", "0")
    assert await run_startup_preflight(_GW({}, _jwt({})), _Settings()) is None


async def test_startup_log_only_by_default(monkeypatch):
    _clean_env(monkeypatch)
    gw = _GW({"/Persons": 403}, _jwt({"scope": "s"}))
    report = await run_startup_preflight(gw, _Settings())
    assert report is not None and report.ok is False  # logged, NOT raised


async def test_startup_strict_raises_on_failure(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("AUTH_PREFLIGHT_STRICT", "1")
    gw = _GW({"/Persons": 403}, _jwt({"scope": "s"}))
    with pytest.raises(RuntimeError, match="forbidden_routes"):
        await run_startup_preflight(gw, _Settings())


async def test_startup_strict_raises_when_token_fetch_fails(monkeypatch):
    """Fails-open fix: mrtvi kredencijali (IdP odbija token) ne smiju proći
    strict gate čak i ako probe sloj slučajno 'radi'."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("AUTH_PREFLIGHT_STRICT", "1")
    gw = _GW({"/Persons": 200}, RuntimeError("invalid_client"))
    with pytest.raises(RuntimeError, match="verified=False"):
        await run_startup_preflight(gw, _Settings())


async def test_startup_strict_raises_without_real_http_answer(monkeypatch):
    """Gateway koji nikad ne dobije pravi HTTP odgovor (status 0) = ništa
    verificirano → strict mora odbiti start, log-only smije proći."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("AUTH_PREFLIGHT_STRICT", "1")
    gw = _GW({"/Persons": 0}, _jwt({"scope": "s"}))
    with pytest.raises(RuntimeError, match="verified=False"):
        await run_startup_preflight(gw, _Settings())


async def test_startup_log_only_tolerates_unverified(monkeypatch):
    _clean_env(monkeypatch)
    gw = _GW({"/Persons": 0}, _jwt({"scope": "s"}))
    report = await run_startup_preflight(gw, _Settings())
    assert report is not None
    assert report.ok is True and report.verified is False


async def test_startup_gateway_without_token_manager(monkeypatch):
    """Gateway bez .token_manager atributa: log-only proguta (None),
    strict odbija start — unverifiable ne smije biti tiho preskočen."""
    _clean_env(monkeypatch)
    gw = FakeGateway({"/Persons": 200})  # nema token_manager
    assert await run_startup_preflight(gw, _Settings()) is None
    monkeypatch.setenv("AUTH_PREFLIGHT_STRICT", "1")
    with pytest.raises(RuntimeError, match="errored"):
        await run_startup_preflight(gw, _Settings())


async def test_startup_uses_settings_tenant(monkeypatch):
    _clean_env(monkeypatch)
    gw = _GW({"/Persons": 200}, _jwt({"scope": "s"}))
    await run_startup_preflight(gw, _Settings())
    assert gw.calls[0]["tenant"] == "t-env"
