"""Scope the 950-tool catalog down to a per-tenant subset.

Phase E of the 2026-05-16 persona-restricted routing plan. Routing
accuracy is bottlenecked by sibling rivalry — for short Croatian
queries like "marka", the LLM router can't tell `get_MasterData`
(the driver's vehicle-snapshot tool) apart from
`get_VehicleInputHelper_DistinctMakes` (an internal form-dropdown
helper). Reducing the candidate set BEFORE anchor retrieval makes
sibling competition disappear structurally.

Scoping rule (precedence, first match wins):
  1. Per-tenant override at config/tenants/{tenant_id}/tool_subset.json
     — explicit `allowed_tool_ids` list. Used as-is.
  2. Default subset at config/tenants/_default/tool_subset.json
     — used when tenant has no override.

Persona filtering was removed 2026-05-28 (Filip): we have no reliable
source of user role (MobilityOne `TenantRoles` unverified), backend OAuth
scope (HTTP 403) is the real ACL, and the filter had been a no-op since
2026-05-22 (`persona=None`). The `personas_strict` field stays in the
registry as harmless metadata — it's still read by audit/bench scripts.

The function is PURE (no I/O at call-time): loading tenant configs
happens once at engine factory. Repeated calls with the same args
return the same set instantly.

API:
    scoper = CatalogScoper(tool_data, tenants_dir)
    allowed: set[str] = scoper.scope(tenant_id="damir", methods={"GET"})
    # → ~50-100 operation_ids (instead of 950)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_TENANT = "_default"

# Internal helpers that are never user-facing: form-dropdown values,
# grouping endpoints, projection helpers, schema introspection.
# Filtered out of any user-facing candidate set (Model A 2026-05-17).
#
# 2026-05-19 fix (Filip benchmark finding): `_Agg` was REMOVED from this
# regex. Most _Agg tools are legitimate manager-facing aggregation queries
# (e.g. "agregat troškova po centrima troškova"). The few internal _Agg
# tools are tagged `personas_strict=['internal']` but that filter went
# away with the 2026-05-28 persona rip — they're now filtered only by
# this regex via _DistinctX/etc. (acceptable: noise risk is low).
_INTERNAL_HELPER_SUFFIX = re.compile(
    r"_(Distinct[A-Z][A-Za-z0-9]*|GroupBy|ProjectTo|metadata|"
    r"DeleteByCriteria|multipatch)$"
)


def is_internal_helper(operation_id: str) -> bool:
    """Heuristic: tool is internal (UI helper / aggregation / schema)
    rather than a user-facing operation. Filtered from candidate pools
    shown to end-users."""
    if not operation_id:
        return False
    return bool(_INTERNAL_HELPER_SUFFIX.search(operation_id))


@dataclass
class CatalogScoper:
    tool_data: dict
    tenants_dir: Path
    # Cache: (tenant_id, methods_tuple, drop_internal) → frozenset[op_id]
    _scope_cache: dict[tuple, frozenset[str]] = field(default_factory=dict)
    # Cache: tenant_id → (Optional[set[str]], source_path_or_None, mtime)
    # source_path is the resolved tenants/<id>/tool_subset.json OR the _default
    # path OR None when no config exists. mtime invalidation re-reads on push.
    _tenant_allowed: dict[
        str, tuple[Optional[set[str]], Optional[Path], float]
    ] = field(default_factory=dict)
    # Parallel to _scope_cache: stores the (source_path, mtime) the cached
    # frozenset was computed against, so we can invalidate on file change.
    _scope_cache_source: dict[
        tuple, tuple[Optional[Path], float]
    ] = field(default_factory=dict)

    def scope(
        self,
        tenant_id: Optional[str],
        *,
        methods: Optional[frozenset[str]] = None,
        drop_internal: bool = False,
    ) -> frozenset[str]:
        """Return allowed operation_ids for this tenant.

        Empty tenant collapses to "_default".

        ``methods`` (optional): set of HTTP methods to allow (e.g. {"GET"}
        or {"PUT", "PATCH"}). When provided, filter further by tool's
        method field in tool_data.

        ``drop_internal`` (Model A 2026-05-17): when True, exclude tools
        whose operation_id matches the internal-helper regex (UI dropdowns,
        aggregations, schema endpoints). Used to clean candidate pools
        before user-facing tool picker.

        Per-call cost: typically O(1) cache hit + cheap stat(). Cache key
        includes methods/drop_internal so each combination is cached
        separately.
        """
        t = (tenant_id or _DEFAULT_TENANT).strip() or _DEFAULT_TENANT
        m_key = tuple(sorted(methods)) if methods else ()
        key = (t, m_key, drop_internal)

        tenant_allowed, current_source, current_mtime = self._tenant_subset(t)

        # Cache hit only valid if both the scope key AND the underlying file
        # are unchanged. We invalidate by checking against the stored entry.
        cached_scope = self._scope_cache.get(key)
        cached_source, cached_mtime = self._scope_cache_source.get(key, (None, -1.0))
        if (
            cached_scope is not None
            and cached_source == current_source
            and cached_mtime == current_mtime
        ):
            return cached_scope

        if tenant_allowed is None:
            base = set(self.tool_data.get("tools", {}).keys())
        else:
            base = tenant_allowed

        tools = self.tool_data.get("tools", {})

        def _keep(op_id: str) -> bool:
            entry = tools.get(op_id, {})
            if methods:
                tool_method = (entry.get("method") or "").upper()
                if tool_method not in methods:
                    return False
            if drop_internal and is_internal_helper(op_id):
                return False
            return True

        filtered = {op_id for op_id in base if _keep(op_id)}

        scope = frozenset(filtered)
        self._scope_cache[key] = scope
        self._scope_cache_source[key] = (current_source, current_mtime)
        logger.debug(
            "catalog_scoper: tenant=%s methods=%s drop_internal=%s "
            "→ %d tools (base=%d, src=%s)",
            t, m_key, drop_internal, len(scope), len(base), current_source,
        )
        return scope

    def _tenant_subset(
        self, tenant_id: str,
    ) -> tuple[Optional[set[str]], Optional[Path], float]:
        """Resolve the tenant's tool subset with mtime invalidation.

        Returns ``(allowed_set, source_path, mtime)``:
          - ``allowed_set`` is None if neither tenant file nor _default exists,
            instructing the scope() caller to fall back to "all tools allowed".
          - ``source_path`` identifies which file was used (for change detection
            even between two valid sources e.g. tenant added an override).
          - ``mtime`` is the stat-mtime of the source file (or 0.0 if None).
        """
        # Determine the source file the way it would be picked TODAY.
        chosen_path: Optional[Path] = None
        for candidate in (tenant_id, _DEFAULT_TENANT):
            path = self.tenants_dir / candidate / "tool_subset.json"
            if path.exists():
                chosen_path = path
                break

        chosen_mtime = 0.0
        if chosen_path is not None:
            try:
                chosen_mtime = chosen_path.stat().st_mtime
            except OSError:
                chosen_path = None  # treat as absent; below returns (None, None, 0.0)

        # Cache hit only when source path AND mtime match what's on disk now.
        cached = self._tenant_allowed.get(tenant_id)
        if cached is not None:
            _, cached_path, cached_mtime = cached
            if cached_path == chosen_path and cached_mtime == chosen_mtime:
                return cached

        # Miss — re-read.
        allowed: Optional[set[str]] = None
        if chosen_path is not None:
            try:
                doc = json.loads(chosen_path.read_text(encoding="utf-8"))
                parsed = set(doc.get("allowed_tool_ids") or [])
                if parsed:
                    allowed = parsed
                else:
                    logger.warning(
                        "tenant config %s has empty allowed_tool_ids — ignoring",
                        chosen_path,
                    )
            except (OSError, ValueError) as e:
                logger.warning("failed to load tenant config %s: %s", chosen_path, e)

        result = (allowed, chosen_path, chosen_mtime)
        self._tenant_allowed[tenant_id] = result
        return result
