"""Faza 11.2 (Filip 2026-05-18) — atomic_write_json safety.

The bot factory reads config files (tool_data.json, risky_tools.json,
param_labels_hr.json) at startup. If a regenerator script writes those
files non-atomically and the bot reads mid-write, it gets a half-written
file and crashes. atomic_write_json writes to a sibling .tmp first then
os.replace's atomically.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.v2.atomic_io import atomic_write_json


def test_writes_valid_json(tmp_path: Path):
    target = tmp_path / "config.json"
    atomic_write_json(target, {"hello": "svijet", "n": 42})
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed == {"hello": "svijet", "n": 42}


def test_preserves_croatian_characters(tmp_path: Path):
    target = tmp_path / "hr.json"
    atomic_write_json(target, {"poruka": "Šećer ćeš čuvati"})
    text = target.read_text(encoding="utf-8")
    assert "Šećer ćeš čuvati" in text  # ensure_ascii=False default


def test_overwrites_existing_file(tmp_path: Path):
    target = tmp_path / "config.json"
    target.write_text('{"old": true}', encoding="utf-8")
    atomic_write_json(target, {"new": True})
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed == {"new": True}


def test_no_tmp_file_left_behind(tmp_path: Path):
    target = tmp_path / "config.json"
    atomic_write_json(target, {"x": 1})
    assert target.exists()
    tmp = target.with_suffix(target.suffix + ".tmp")
    assert not tmp.exists(), f"temp file {tmp} should have been os.replace()d"


def test_indent_default_is_two_spaces(tmp_path: Path):
    target = tmp_path / "config.json"
    atomic_write_json(target, {"a": {"b": 1}})
    text = target.read_text(encoding="utf-8")
    # Nested key should be indented by 2 spaces (one level deep = 2 spaces)
    assert '\n  "a"' in text
    assert '\n    "b"' in text


def test_handles_list_at_top_level(tmp_path: Path):
    target = tmp_path / "list.json"
    atomic_write_json(target, ["a", "b", "c"])
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed == ["a", "b", "c"]


def test_serialization_failure_does_not_truncate_target(tmp_path: Path):
    """If json.dumps raises, the existing target file must remain untouched.
    Without atomic write, a direct write_text on the original would create
    a 0-byte file before crashing."""
    target = tmp_path / "config.json"
    target.write_text('{"original": true}', encoding="utf-8")
    bad_data = {"key": object()}  # not JSON-serializable
    with pytest.raises(TypeError):
        atomic_write_json(target, bad_data)
    # Target still has the original content
    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed == {"original": True}
