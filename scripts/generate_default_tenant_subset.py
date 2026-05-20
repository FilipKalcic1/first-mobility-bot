"""Phase C1 — Generate config/tenants/_default/tool_subset.json from current
personas_strict tags.

Default subset = every tool tagged with at least one user-facing persona
(driver / manager / admin). Internal-only tools (form-dropdowns, helpers,
_Agg variants used only via flows) are excluded.

After Damir's audit (Phase B), this file will be REGENERATED from his
is_user_facing field. For now (pre-audit), the LLM strict classification
is the source of truth.

Re-run anytime tool_data.json's personas_strict changes:
    python scripts/generate_default_tenant_subset.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent
TOOL_DATA = REPO / "config" / "tool_data.json"
OUT_DIR = REPO / "config" / "tenants" / "_default"
OUT_FILE = OUT_DIR / "tool_subset.json"

USER_FACING = {"driver", "manager", "admin"}


def main() -> None:
    doc = json.loads(TOOL_DATA.read_text(encoding="utf-8"))
    tools = doc.get("tools", {})

    allowed = sorted(
        op for op, t in tools.items()
        if any(p in USER_FACING for p in (t.get("personas_strict") or []))
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(
        json.dumps({
            "tenant_id": "_default",
            "description": (
                "Default tool subset for any tenant. Tools with at least one "
                "user-facing persona (driver/manager/admin) according to "
                "personas_strict in tool_data.json. Internal-only tools "
                "(form-helpers, _Agg, _DistinctXxx, _ProjectTo) excluded."
            ),
            "source": "personas_strict in config/tool_data.json (Phase A2 strict LLM classification)",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "allowed_tool_ids": allowed,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {OUT_FILE.relative_to(REPO)}")
    print(f"  allowed: {len(allowed)} / {len(tools)} tools")
    print(f"  excluded: {len(tools) - len(allowed)} (internal-only)")


if __name__ == "__main__":
    main()
