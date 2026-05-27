"""Echo razriješenih vrijednosti prije mutacije (Filip 2026-05-27).

Before a mutation confirm, the bot now shows the user-provided values it is
about to send (QB-style transparency) so the user can verify before "Da".
Tests cover the value formatter, the echo builder (skips context/auto params),
and that _render_confirm_pending prepends the echo before the confirm question.
"""
from __future__ import annotations

from types import SimpleNamespace

from services.v2.engine import V2Engine


# ---- _fmt_echo_value (static) ----

def test_fmt_echo_value_iso_date():
    assert V2Engine._fmt_echo_value("2026-05-17") == "17.05.2026"


def test_fmt_echo_value_iso_datetime():
    assert V2Engine._fmt_echo_value("2026-05-17T09:30:00") == "17.05.2026 09:30"


def test_fmt_echo_value_bool():
    assert V2Engine._fmt_echo_value(True) == "da"
    assert V2Engine._fmt_echo_value(False) == "ne"


def test_fmt_echo_value_number_verbatim():
    assert V2Engine._fmt_echo_value(50000) == "50000"
    assert V2Engine._fmt_echo_value(12.5) == "12.5"


def test_fmt_echo_value_plain_string_untouched():
    # not an ISO date → verbatim
    assert V2Engine._fmt_echo_value("DA053F") == "DA053F"


# ---- _render_param_echo ----

def _eng_with(spec: dict) -> V2Engine:
    eng = object.__new__(V2Engine)
    eng.tool_parameters = spec
    return eng


def test_render_param_echo_shows_user_value():
    eng = _eng_with({
        "post_AddMileage": {
            "Value": {"param_type": "integer", "dependency_source": "user_input"},
        }
    })
    out = eng._render_param_echo("post_AddMileage", {"Value": 50000})
    assert "50000" in out
    assert "provjeri prije slanja" in out.lower()


def test_render_param_echo_skips_context_params():
    """Auto-injected identity params (UUIDs the user never typed) must NOT
    appear in the echo."""
    eng = _eng_with({
        "post_AddMileage": {
            "Value": {"param_type": "integer", "dependency_source": "user_input"},
            "VehicleId": {"param_type": "string", "dependency_source": "context"},
        }
    })
    out = eng._render_param_echo(
        "post_AddMileage", {"Value": 50000, "VehicleId": "49ede20e-uuid"}
    )
    assert "50000" in out
    assert "49ede20e-uuid" not in out


def test_render_param_echo_formats_date():
    eng = _eng_with({
        "post_VehicleCalendar": {
            "FromTime": {"param_type": "string", "format": "date-time",
                         "dependency_source": "user_input"},
        }
    })
    out = eng._render_param_echo(
        "post_VehicleCalendar", {"FromTime": "2026-05-17T09:00:00"}
    )
    assert "17.05.2026 09:00" in out


def test_render_param_echo_empty_when_only_context():
    eng = _eng_with({
        "t": {"VehicleId": {"dependency_source": "context"}}
    })
    assert eng._render_param_echo("t", {"VehicleId": "uuid"}) == ""


def test_render_param_echo_empty_when_no_params():
    eng = _eng_with({"t": {}})
    assert eng._render_param_echo("t", {}) == ""


def test_render_param_echo_skips_none_and_blank():
    eng = _eng_with({
        "t": {
            "A": {"dependency_source": "user_input"},
            "B": {"dependency_source": "user_input"},
        }
    })
    out = eng._render_param_echo("t", {"A": None, "B": ""})
    assert out == ""


# ---- _render_confirm_pending prepends echo ----

def _render(echo: str, *, risky=False, confirm="Potvrđuješ? Da/Ne") -> str:
    fake = SimpleNamespace(
        risky_tool_ids={"post_X"} if risky else set(),
        _RISKY_WARN_HR=V2Engine._RISKY_WARN_HR,
    )
    mut = SimpleNamespace(confirm_message=confirm)
    return V2Engine._render_confirm_pending(fake, mut, tool_id="post_X", echo=echo)


def test_confirm_prepends_echo_before_question():
    out = _render("Provjeri prije slanja:\n• Value: 50000\n\n")
    assert out == "Provjeri prije slanja:\n• Value: 50000\n\nPotvrđuješ? Da/Ne"


def test_confirm_empty_echo_unchanged():
    assert _render("") == "Potvrđuješ? Da/Ne"


def test_confirm_risky_warn_then_echo_then_question():
    out = _render("ECHO\n\n", risky=True)
    assert out.startswith("Napomena:")          # risky warn first
    assert "ECHO\n\n" in out                     # then echo
    assert out.rstrip().endswith("Da/Ne")        # then confirm


# ---- *TypeId: show resolved NAME, not the raw id ----

def test_render_param_echo_shows_type_name_not_id():
    eng = _eng_with({
        "post_Expenses": {
            "Amount": {"param_type": "number", "dependency_source": "user_input"},
            "ExpenseTypeId": {"param_type": "integer",
                              "dependency_source": "user_input"},
        }
    })
    out = eng._render_param_echo(
        "post_Expenses",
        {"Amount": 50, "ExpenseTypeId": 3},
        type_display={"ExpenseTypeId": "Gorivo"},
    )
    assert "Gorivo" in out          # human name shown
    assert "50" in out              # plain value still shown
    assert "3" not in out           # raw FK id NOT shown


def test_render_param_echo_falls_back_to_id_without_type_display():
    """No type_display (e.g. resolver couldn't fetch) → graceful id fallback."""
    eng = _eng_with({
        "post_Expenses": {
            "ExpenseTypeId": {"dependency_source": "user_input"},
        }
    })
    out = eng._render_param_echo("post_Expenses", {"ExpenseTypeId": 3})
    assert "3" in out


# ---- context-as-name: show the TARGET (Vozilo: DA053F), never the UUID ----

def test_build_context_display_resolves_vehicle_name():
    eng = _eng_with({
        "post_AddMileage": {
            "VehicleId": {"dependency_source": "context", "context_key": "vehicle_id"},
            "Value": {"dependency_source": "user_input"},
        }
    })
    identity = SimpleNamespace(
        vehicle_name="DA053F", company_name=None, org_unit_name=None,
    )
    assert eng._build_context_display("post_AddMileage", identity) == {"Vozilo": "DA053F"}


def test_build_context_display_skips_person_and_tenant_plumbing():
    """Only vehicle/company/org-unit are meaningful — person (= you) and tenant
    are implicit plumbing and stay hidden."""
    eng = _eng_with({
        "t": {
            "DriverId": {"dependency_source": "context", "context_key": "person_id"},
            "TenantId": {"dependency_source": "context", "context_key": "tenant_id"},
        }
    })
    identity = SimpleNamespace(vehicle_name="X", company_name="Y", org_unit_name="Z")
    assert eng._build_context_display("t", identity) == {}


def test_build_context_display_omits_context_without_identity_name():
    eng = _eng_with({
        "t": {"VehicleId": {"dependency_source": "context", "context_key": "vehicle_id"}}
    })
    identity = SimpleNamespace(vehicle_name=None, company_name=None, org_unit_name=None)
    assert eng._build_context_display("t", identity) == {}


def test_render_param_echo_shows_context_target_before_values():
    eng = _eng_with({
        "post_AddMileage": {
            "Value": {"param_type": "integer", "dependency_source": "user_input"},
            "VehicleId": {"dependency_source": "context", "context_key": "vehicle_id"},
        }
    })
    out = eng._render_param_echo(
        "post_AddMileage", {"Value": 50000},
        context_display={"Vozilo": "DA053F"},
    )
    assert "Vozilo: DA053F" in out          # target shown as NAME
    assert "50000" in out                    # value shown
    assert out.index("Vozilo") < out.index("50000")   # target first
    assert "provjeri prije slanja" in out.lower()


def test_render_param_echo_no_context_display_unchanged():
    """context_display=None → behaves exactly as before (no target line)."""
    eng = _eng_with({
        "post_AddMileage": {
            "Value": {"param_type": "integer", "dependency_source": "user_input"},
        }
    })
    out = eng._render_param_echo("post_AddMileage", {"Value": 50000})
    assert "Vozilo" not in out
    assert "50000" in out
