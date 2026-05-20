"""Faza 9 (Filip 2026-05-17) — _render_confirm_pending injects a Croatian
warn line when the tool sits in risky_tool_ids.

Tools flagged by scripts/audit_registry_body_schemas.py
(MISSING_BODY / LIKELY_MISSING_BODY) get a prefix on their mutation
confirm so the user knows the call may 422. Tools with declared body
schemas — and the empty-set default — pass through unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from services.v2.engine import V2Engine


REPO_ROOT = Path(__file__).resolve().parents[2]


def _call_render(risky_ids: set, tool_id: str, confirm_message: str) -> str:
    """Invoke _render_confirm_pending without constructing the full
    dataclass (which needs ~14 deps). The helper only touches
    self.risky_tool_ids and the class-level _RISKY_WARN_HR constant."""
    fake_self = SimpleNamespace(
        risky_tool_ids=risky_ids,
        _RISKY_WARN_HR=V2Engine._RISKY_WARN_HR,
    )
    mut = SimpleNamespace(confirm_message=confirm_message)
    return V2Engine._render_confirm_pending(fake_self, mut, tool_id=tool_id)


def test_risky_tool_gets_warn_prefix():
    result = _call_render(
        risky_ids={"post_SyncExternalIdAndLicencePlate"},
        tool_id="post_SyncExternalIdAndLicencePlate",
        confirm_message="Sinkroniziraj vozilo ABC123?",
    )
    assert result.startswith("Napomena:")
    assert "kontaktiraj managera" in result
    assert "Sinkroniziraj vozilo ABC123?" in result


def test_safe_tool_passes_through_unchanged():
    result = _call_render(
        risky_ids={"post_SyncExternalIdAndLicencePlate"},
        tool_id="delete_VehicleCalendar_id",
        confirm_message="Briši rezervaciju #45?",
    )
    assert result == "Briši rezervaciju #45?"


def test_empty_risky_set_never_warns():
    result = _call_render(
        risky_ids=set(),
        tool_id="post_SyncExternalIdAndLicencePlate",
        confirm_message="Sinkroniziraj?",
    )
    assert result == "Sinkroniziraj?"


def test_missing_tool_id_never_warns():
    result = _call_render(
        risky_ids={"post_SyncExternalIdAndLicencePlate"},
        tool_id="",
        confirm_message="Konfirmacija?",
    )
    assert result == "Konfirmacija?"


def test_warn_includes_blank_line_separator():
    """The warn must end with \\n\\n so the user sees a clear visual
    break before the actual confirm question."""
    result = _call_render(
        risky_ids={"post_X"},
        tool_id="post_X",
        confirm_message="Pitanje?",
    )
    assert "\n\nPitanje?" in result


# ----------------------------------------------------------------------
# Audit script output sanity (Faza 9 generator contract)
# ----------------------------------------------------------------------


def test_risky_tools_json_exists_and_has_expected_shape():
    """If the audit has been run, the JSON must declare the keys the
    engine factory reads. Missing file = test skipped (CI artifact)."""
    path = REPO_ROOT / "config" / "risky_tools.json"
    if not path.exists():
        import pytest
        pytest.skip("risky_tools.json not present — run audit_registry_body_schemas.py")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data.get("missing_body"), list)
    assert isinstance(data.get("likely_missing_body"), list)
    # Sanity: at least one of the 4 known MISSING_BODY tools is present
    expected_known = {
        "post_SyncExternalIdAndLicencePlate",
        "post_SyncExternalIdAndVIN",
        "post_Metadata_Order",
        "post_MonthlyMileages_RecalculateAll",
    }
    found = expected_known & set(data["missing_body"])
    assert found, (
        f"None of {expected_known} found in missing_body. "
        f"audit script may have changed classification."
    )
