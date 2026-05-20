"""Shared atomic-write helper for config files (Filip 2026-05-18 Faza 11.2).

A regenerator script (sync_tools.py, audit_registry_body_schemas.py, ...)
that writes the config file directly with `path.write_text(...)` leaves
a window where the bot's factory could read a half-written JSON and
crash. The fix is dead simple: write to a sibling temp file, fsync if
possible, then `os.replace()` which is atomic on POSIX and Windows.

Use this in every script that overwrites a config file the bot reads
at startup or periodically.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> None:
    """Serialize ``data`` to JSON and write it to ``path`` atomically.

    Writes to ``path.with_suffix(path.suffix + '.tmp')`` first, then uses
    ``os.replace()`` which on every supported OS is atomic — readers either
    see the old file or the new file, never a partial one.

    ``indent``/``ensure_ascii`` mirror ``json.dumps`` defaults that the
    repo's config files use (indent=2, Croatian-safe ensure_ascii=False).
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=ensure_ascii, indent=indent),
        encoding="utf-8",
    )
    os.replace(tmp, path)
