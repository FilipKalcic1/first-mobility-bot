"""Phase 1a (offline, deterministic): does (entity x action) narrow the 950-tool
space? Measures the CEILING of structure-first routing — IF the entity+action
were known, how small is the candidate set + does it contain the answer?

No LLM, no engine. Tags every tool with a derived entity (from operation_id),
buckets by (entity, method), then checks where the eval expected-tools land.
Prints sample tags so derivation quality is eyeballable.

Run: python scripts/eval_structure_first.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL_DATA = REPO / "config" / "tool_data.json"
EVAL = REPO / "tests" / "benchmarks" / "expanded_benchmark_v2.json"

# Leading verbs in operation_ids (after method prefix) to strip so the noun
# (entity) remains. The HTTP method already carries the action; these are extra
# verbs in automation-style op_ids (post_AddMileage -> Mileage).
_VERBS = {
    "add", "get", "set", "update", "delete", "create", "remove", "list",
    "sync", "recalculate", "request", "upsert", "resend", "send", "export",
    "import", "generate", "assign", "approve", "reject", "cancel", "confirm",
    "move", "copy", "merge", "bulk", "fetch", "find", "search", "make",
    "register", "evidence", "calculate", "process", "run", "start", "stop",
    "enable", "disable", "activate", "deactivate", "order", "reorder",
}
_METHOD_PREFIX = re.compile(r"^(get|post|put|patch|delete)_", re.I)


def _split_camel(s: str) -> list[str]:
    return re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+", s)


def _singular(w: str) -> str:
    if len(w) > 4 and w.lower().endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.lower().endswith("ses"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.lower().endswith("ss"):
        return w[:-1]
    return w


def derive_entity(op_id: str) -> str:
    """operation_id -> rough primary entity (heuristic, for ceiling measurement)."""
    core = _METHOD_PREFIX.sub("", op_id)
    # drop trailing _id / _<something>Id path-key suffixes
    core = re.sub(r"_id$", "", core)
    core = re.sub(r"_[a-z][A-Za-z]*[Ii]d$", "", core)
    core = re.sub(r"_id_.*$", "", core)
    toks = [t for t in _split_camel(core.replace("_", "")) if t]
    # strip a single leading verb token
    if toks and toks[0].lower() in _VERBS:
        toks = toks[1:]
    if not toks:
        return core or "?"
    entity = "".join(toks)
    return _singular(entity)


def action_of(method: str) -> str:
    m = (method or "").upper()
    return {"GET": "read", "POST": "create",
            "PUT": "update", "PATCH": "update", "DELETE": "delete"}.get(m, m.lower())


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    tools = json.loads(TOOL_DATA.read_text(encoding="utf-8")).get("tools", {})
    bench = json.loads(EVAL.read_text(encoding="utf-8"))
    queries = [q for q in (bench.get("queries") or []) if q.get("expected_tool")]

    # tag every tool
    tag: dict[str, tuple[str, str]] = {}   # op_id -> (entity, action)
    grid: dict[tuple[str, str], list[str]] = defaultdict(list)
    for op_id, e in tools.items():
        ent = derive_entity(op_id)
        act = action_of(e.get("method", ""))
        tag[op_id] = (ent, act)
        grid[(ent, act)].append(op_id)

    n_tools = len(tools)
    n_entities = len({e for e, _ in tag.values()})
    cell_sizes = [len(v) for v in grid.values()]
    print(f"=== GRID: {n_tools} tools -> {n_entities} entiteta x {len(grid)} (entitet,akcija) celija ===")
    cs = Counter()
    for s in cell_sizes:
        cs["1" if s == 1 else "2-3" if s <= 3 else "4-5" if s <= 5 else "6+"] += 1
    print(f"  velicina celija: 1={cs['1']}  2-3={cs['2-3']}  4-5={cs['4-5']}  6+={cs['6+']}")

    print("\n=== SAMPLE entity-tag (eyeball kvalitete derivacije) ===")
    for op in ["get_Vehicles", "post_AddMileage", "delete_VehicleCalendar_id",
               "post_AddCase", "get_AvailableVehicles", "post_Expenses",
               "patch_Vehicles_id", "get_MasterData", "get_Persons"]:
        if op in tag:
            print(f"  {op:32s} -> {tag[op]}")

    # eval: where do expected tools land?
    print(f"\n=== EVAL ({EVAL.name}, {len(queries)} upita): velicina (entitet,akcija) celije expected toola ===")
    in_grid = 0
    sizes: list[int] = []
    le1 = le3 = le5 = 0
    examples = []
    for q in queries:
        exp = q["expected_tool"]
        if exp not in tag:
            continue
        in_grid += 1
        cell = grid[tag[exp]]
        sz = len(cell)
        sizes.append(sz)
        if sz <= 1:
            le1 += 1
        if sz <= 3:
            le3 += 1
        if sz <= 5:
            le5 += 1
        if len(examples) < 12:
            examples.append((q["query"][:42], exp, tag[exp], sz))

    if in_grid:
        sizes.sort()
        med = sizes[len(sizes) // 2]
        print(f"  expected tool prepoznat u gridu: {in_grid}/{len(queries)}")
        print(f"  CEILING (ako su entitet+akcija točni) — answer u celiji:")
        print(f"    ≤1 tool: {le1} ({le1*100//in_grid}%)   ≤3: {le3} ({le3*100//in_grid}%)   ≤5: {le5} ({le5*100//in_grid}%)")
        print(f"    medijan velicine celije: {med}")
        print(f"  (usporedba: anchor top-50 daje recall@3 ~47% na istom setu)")
        print("\n  primjeri (upit | expected | (entitet,akcija) | velicina celije):")
        for query, exp, t, sz in examples:
            print(f"    \"{query}\"  →  {exp}  {t}  cell={sz}")


if __name__ == "__main__":
    main()
