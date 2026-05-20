"""Loader for externalized domain/linguistic JSON configs.

Domain data (HR stems/synonyms in `domain/`, typo + slang map in
`linguistic/`) lives under `config/` so linguists can edit without
touching Python. This module loads, validates, and caches those files.

Contract:
- `load_json(kind, name)` returns the parsed object (already-validated).
- Loads are LRU-cached; call `reload()` to force re-read.
- On validation failure: raises at startup (fail-fast). Callers get a
  predictable shape and never need to defensively check for missing keys.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_CONFIG_ROOT = Path(__file__).resolve().parent.parent / "config"

_ALLOWED_KINDS = frozenset({"domain", "linguistic"})


def _path(kind: str, name: str) -> Path:
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"Unknown config kind: {kind!r}")
    return _CONFIG_ROOT / kind / name


@lru_cache(maxsize=64)
def load_json(kind: str, name: str) -> Any:
    p = _path(kind, name)
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=64)
def load_text(kind: str, name: str) -> str:
    return _path(kind, name).read_text(encoding="utf-8")


def reload(kind: str | None = None, name: str | None = None) -> None:
    load_json.cache_clear()
    load_text.cache_clear()
