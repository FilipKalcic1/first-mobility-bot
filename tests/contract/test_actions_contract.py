"""Contract testovi za /actions sloj (PP2/PP3).

PRAVILO iz PRESSURE_POINTS docs §2: akcija se u produkciji uključuje TEK
kad njen fixture prođe (a) offline validaciju i (b) LIVE run protiv dev
Business API-ja. Isti fixturei su "provider contract" koji Damirov tim može
vrtjeti u svom CI-ju.

Modovi:
  OFFLINE (uvijek):  struktura fixturea + placeholder sintaksa + (kad
                     postoji config/actions.json) križna provjera request
                     polja protiv ai.parameters + policy.inject.
  LIVE (gated):      CONTRACT_BASE_URL + CONTRACT_BEARER_TOKEN u env →
                     stvarni POST/GET na /actions/<name>; response mora
                     biti superset očekivanog (placeholder-aware). Bez env
                     varijabli testovi se SKIPaju (isti pattern kao probe
                     skripte — siguran default u CI-ju). Mutacijski (POST)
                     fixturei dodatno traže CONTRACT_ALLOW_MUTATIONS=1 —
                     guard da run protiv okoline s pravim podacima ne
                     ostavi booking/incident zapise. Error slučajevi
                     (errors[].request_override → expect) verificiraju se
                     istim live runom.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
ACTIONS_JSON = Path(__file__).resolve().parents[2] / "config" / "actions.json"

_PLACEHOLDER_TYPES = {
    "<any>": object,
    "<any-string>": str,
    "<any-int>": int,
    "<any-number>": (int, float),
    "<any-bool>": bool,
}


def _fixtures() -> list:
    return sorted(FIXTURES_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def match_expected(expected, actual, path="$") -> list:
    """Superset match s placeholderima. Vraća listu mismatch opisa
    (prazna = OK). `expected` dict polja MORAJU postojati u `actual`;
    extra polja u `actual` su dopuštena (backend smije vraćati više)."""
    problems: list = []
    if isinstance(expected, str) and expected in _PLACEHOLDER_TYPES:
        want = _PLACEHOLDER_TYPES[expected]
        if want is object:
            if actual is None:
                problems.append(f"{path}: očekivano bilo što ne-None, dobiveno None")
        elif isinstance(actual, bool) and want is not bool:
            # bool je subclass int — ne prihvaćaj True kao <any-int>/<any-number>
            problems.append(f"{path}: očekivan {expected}, dobiven bool")
        elif not isinstance(actual, want):
            problems.append(
                f"{path}: očekivan {expected}, dobiven {type(actual).__name__}"
            )
        return problems
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: očekivan objekt, dobiven {type(actual).__name__}"]
        for key, val in expected.items():
            if key not in actual:
                problems.append(f"{path}.{key}: polje nedostaje u odgovoru")
            else:
                problems.extend(match_expected(val, actual[key], f"{path}.{key}"))
        return problems
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: očekivana lista, dobiven {type(actual).__name__}"]
        for i, item in enumerate(expected):
            if i >= len(actual):
                problems.append(f"{path}[{i}]: element nedostaje")
            else:
                problems.extend(match_expected(item, actual[i], f"{path}[{i}]"))
        return problems
    if expected != actual:
        problems.append(f"{path}: očekivano {expected!r}, dobiveno {actual!r}")
    return problems


# ---------------------------------------------------------------------------
# OFFLINE — uvijek se vrti
# ---------------------------------------------------------------------------


def test_fixture_directory_not_empty():
    assert _fixtures(), "nema contract fixturea — PP2 lanac je prekinut"


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_fixture_structure(path):
    fx = _load(path)
    for key in ("action", "method", "path", "request", "response"):
        assert key in fx, f"{path.name}: nedostaje '{key}'"
    assert fx["method"] in ("GET", "POST"), f"{path.name}: nepoznata metoda"
    assert fx["path"].startswith("/actions/"), f"{path.name}: path mora biti /actions/*"
    assert fx["action"] == path.stem, f"{path.name}: action != ime filea"
    assert isinstance(fx["request"], dict) and fx["request"], "prazan request"
    assert isinstance(fx["response"], dict) and fx["response"], "prazan response"
    for err in fx.get("errors", []):
        assert "when" in err and "expect" in err, f"{path.name}: error bez when/expect"
        assert "error_code" in err["expect"], f"{path.name}: expect bez error_code"
        override = err.get("request_override", {})
        assert isinstance(override, dict), (
            f"{path.name}: request_override mora biti objekt")
        unknown = set(override) - set(fx["request"])
        assert not unknown, (
            f"{path.name}: request_override uvodi polja koja request nema "
            f"{sorted(unknown)} — live error-run bi slao payload izvan sheme")


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_fixture_placeholders_are_known(path):
    """Tipfeler u placeholderu ('<any-str>') bi tiho postao literal match."""
    def walk(node):
        if isinstance(node, str) and node.startswith("<") and node.endswith(">"):
            assert node in _PLACEHOLDER_TYPES, f"nepoznat placeholder {node}"
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    fx = _load(path)
    walk(fx["response"])
    for err in fx.get("errors", []):
        walk(err["expect"])


@pytest.mark.skipif(not ACTIONS_JSON.exists(),
                    reason="config/actions.json još ne postoji (F1)")
@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_fixture_request_matches_action_schema(path):
    """F1 križna provjera: svako request polje mora biti ili ai.parameter
    ili policy.inject polje te akcije — fixture ne smije driftati od sheme."""
    fx = _load(path)
    actions = {a["name"]: a for a in json.loads(
        ACTIONS_JSON.read_text(encoding="utf-8"))["actions"]}
    assert fx["action"] in actions, f"akcija {fx['action']} nije u actions.json"
    spec = actions[fx["action"]]
    allowed = set(spec["ai"]["parameters"].keys()) | set(
        spec.get("policy", {}).get("inject", []))
    unknown = set(fx["request"]) - allowed
    assert not unknown, f"request polja izvan sheme akcije: {sorted(unknown)}"
    for i, err in enumerate(fx.get("errors", [])):
        merged = {**fx["request"], **err.get("request_override", {})}
        unknown = set(merged) - allowed
        assert not unknown, (
            f"errors[{i}] merged request izvan sheme: {sorted(unknown)}")


# ---------------------------------------------------------------------------
# Matcher unit testovi (offline)
# ---------------------------------------------------------------------------


def test_matcher_superset_and_placeholders():
    expected = {"status": "success", "id": "<any-string>", "n": "<any-int>"}
    actual = {"status": "success", "id": "INC-1", "n": 3, "extra": "ok"}
    assert match_expected(expected, actual) == []


def test_matcher_reports_missing_and_wrong_type():
    expected = {"status": "success", "id": "<any-string>"}
    problems = match_expected(expected, {"status": "fail", "id": 42})
    assert any("status" in p for p in problems)
    assert any("id" in p and "any-string" in p for p in problems)


def test_matcher_bool_is_not_any_int():
    assert match_expected({"v": "<any-int>"}, {"v": True})
    assert match_expected({"v": "<any-bool>"}, {"v": True}) == []


def test_matcher_list_rejects_non_list_actual():
    problems = match_expected({"items": ["<any-string>"]}, {"items": "nije-lista"})
    assert problems and "očekivana lista" in problems[0]


def test_matcher_list_reports_missing_element():
    problems = match_expected({"items": [1, 2]}, {"items": [1]})
    assert any("[1]" in p and "nedostaje" in p for p in problems)


def test_matcher_list_prefix_semantics_and_nested_placeholders():
    # Actual smije biti DUŽI (superset pravilo vrijedi i za liste)…
    assert match_expected(
        {"items": [{"id": "<any-string>"}]},
        {"items": [{"id": "x", "extra": 1}, {"id": "y"}]},
    ) == []
    # …ali element na istom indexu mora matchati.
    assert match_expected(
        {"items": [{"id": "<any-int>"}]}, {"items": [{"id": "ne-broj"}]},
    )


# ---------------------------------------------------------------------------
# LIVE — gated (CONTRACT_BASE_URL + CONTRACT_BEARER_TOKEN)
# ---------------------------------------------------------------------------

_LIVE = bool(os.environ.get("CONTRACT_BASE_URL"))
_ALLOW_MUTATIONS = os.environ.get("CONTRACT_ALLOW_MUTATIONS") == "1"


def _skip_unless_safe(fx: dict) -> None:
    """Mutation guard: POST fixture protiv živog backenda TEK uz eksplicitni
    CONTRACT_ALLOW_MUTATIONS=1 — CONTRACT_BASE_URL može pokazivati na
    okolinu s pravim podacima, a booking/incident POST ostavlja trag."""
    if fx["method"] != "GET" and not _ALLOW_MUTATIONS:
        pytest.skip("mutacijski fixture — treba CONTRACT_ALLOW_MUTATIONS=1")


def _auth_headers(request_body: dict):
    """Bearer token završava u httpx.Headers čiji repr REDAKTIRA
    Authorization — ni `pytest -l` na network-failu ne ispisuje token."""
    import httpx

    headers = {"Content-Type": "application/json"}
    if os.environ.get("CONTRACT_BEARER_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['CONTRACT_BEARER_TOKEN']}"
    if request_body.get("tenant_id"):
        headers["x-tenant"] = request_body["tenant_id"]
    return httpx.Headers(headers)


async def _call_live(fx: dict, request_body: dict):
    """Pošalje zahtjev; token NIJE u localsima test framea ni ovog framea
    (samo unutar httpx objekata s redaktiranim reprom) — `pytest -l` u CI
    logu ne smije procuriti credentials."""
    import httpx

    base = os.environ["CONTRACT_BASE_URL"].rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await client.request(
            fx["method"], f"{base}{fx['path']}",
            json=request_body if fx["method"] == "POST" else None,
            # GET: request polja idu kao query string (tenant i u x-tenant).
            params=request_body if fx["method"] == "GET" else None,
            headers=_auth_headers(request_body),
        )


@pytest.mark.skipif(not _LIVE, reason="CONTRACT_BASE_URL nije postavljen (live mod)")
@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
async def test_live_action_contract(path):
    """Stvarni poziv na dev Business API — provider verifikacija ugovora."""
    fx = _load(path)
    _skip_unless_safe(fx)
    resp = await _call_live(fx, fx["request"])
    assert resp.status_code < 500, f"backend 5xx: {resp.text[:300]}"
    problems = match_expected(fx["response"], resp.json())
    assert not problems, "contract mismatch:\n" + "\n".join(problems)


@pytest.mark.skipif(not _LIVE, reason="CONTRACT_BASE_URL nije postavljen (live mod)")
@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
async def test_live_action_error_contract(path):
    """Error ugovori (VEHICLE_NOT_FOUND…) se verificiraju živo: request s
    override poljima mora vratiti 4xx čiji body matcha `expect` — bez ovoga
    'akcija ON tek kad fixture prođe' ne pokriva error grane."""
    fx = _load(path)
    if not fx.get("errors"):
        pytest.skip("fixture nema error slučajeva")
    _skip_unless_safe(fx)
    problems = []
    for i, err in enumerate(fx["errors"]):
        body = {**fx["request"], **err.get("request_override", {})}
        resp = await _call_live(fx, body)
        label = f"errors[{i}] ({err['when']})"
        if not 400 <= resp.status_code < 500:
            problems.append(
                f"{label}: očekivan 4xx, dobiven {resp.status_code}")
            continue
        problems.extend(match_expected(err["expect"], resp.json(), label))
    assert not problems, "error contract mismatch:\n" + "\n".join(problems)
