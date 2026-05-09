# Role Simplification Inventory (One-Role Audit, Phase 1)

**Date:** 2026-05-09
**Method:** 12 parallel Explore agents + consolidation
**Goal:** Inventory of role/persona/audience code occurrences — classify FUNCTIONAL / DECORATIVE / DEAD before Phase 2 cleanup decision.
**Scope:** Read-only discovery. NO changes made in this phase.

---

## Executive summary

**Architectural truth verified across 12 agents:**

1. **`_persona()` is binary** — returns ONLY `"driver"` or `"unknown"`. Manager/admin values **NEVER produced** by current code ([engine.py:137-139](services/v2/engine.py#L137)).
2. **Registry has manager whitelist** (`_MANAGER_FRIENDLY_PREFIXES`, ~243 tools) but it is **never reached** because persona never carries `"manager"`.
3. **Token scope is dead infrastructure** — `MOBILITY_SCOPE` sent in OAuth REQUEST but never extracted from RESPONSE; no PyJWT dependency to decode token claims.
4. **V3 modules accept `persona` param but it is always `"driver"`** at call time — filtering logic prepared but inactive.
5. **Audience filtering at L7 executor IS functional** (confused-deputy defense), at L3 recognition IS functional (driver-only filtering active), elsewhere DECORATIVE.

---

## Classification distribution

Total raw occurrences across all areas: **~75 entries** (after dedup of cross-agent overlaps).

| Classification | Count | % of total |
|---|---|---|
| **FUNCTIONAL** (changes runtime behavior, removing affects production) | ~22 | 29% |
| **DECORATIVE** (code exists but never fires with non-driver persona) | ~32 | 43% |
| **DEAD** (code path unreachable in current production) | ~15 | 20% |
| **DEAD-via-dormant-V3** (code in V3 modules; reachable IF V3 activated, but V3 is OFF) | ~6 | 8% |

---

## HIGH-CONFIDENCE DEAD (rating ≥9/10) — candidates for cleanup

These have clear code citation + justification supporting removal without behavioral change.

### Group A: Manager-tier infrastructure (registry-level)

| File:line | Code | Rationale | Rating |
|---|---|---|---|
| [registry.py:69-102](services/v2/registry.py#L69-L102) | `_MANAGER_FRIENDLY_PREFIXES = _DRIVER_FRIENDLY_PREFIXES + (...)` | Manager whitelist (~180 additional prefixes); never matched because `_persona()` never returns `"manager"`. ~33 LOC. | 9/10 |
| [registry.py:372-373](services/v2/registry.py#L372-L373) | `if audience_hint == "manager": return tool_id.startswith(_MANAGER_FRIENDLY_PREFIXES)` | Manager branch in `tool_matches_audience()` — unreachable, persona never carries "manager". | 9/10 |
| [test_registry.py:194-204](tests/v2/test_registry.py#L194-L204) | `test_audience_manager_includes_driver_plus_fleet_finance_org` | Tests manager branch behavior. Tests would still pass on driver-only logic if rewritten; current form requires manager branch. | 9/10 |

**⚠️ FILIP CONSTRAINT:** Original prompt says *"Preserve audience tags for now (cheap insurance for future role layer)"*. Removing `_MANAGER_FRIENDLY_PREFIXES` removes that insurance. **Decision needed: drop OR keep as future scaffolding.**

### Group B: Token scope dead infrastructure

| File:line | Code | Rationale | Rating |
|---|---|---|---|
| [token_manager.py:55](services/token_manager.py#L55) | `self.scope = _get_settings().MOBILITY_SCOPE` | Loaded but only ever sent in REQUEST (never extracted from RESPONSE). | 9/10 |
| [token_manager.py:159-161](services/token_manager.py#L159-L161) | `if self.scope: payload["scope"] = self.scope` | Sends scope in token request, response is ignored. | 8/10 |

**Cleanup option:** Drop `MOBILITY_SCOPE` env var + scope-sending logic. **Risk:** if backend ever requires scope to issue token, breaks. Conservative recommendation: keep, document as "request-only scope". Low cleanup priority.

### Group C: Hardcoded persona literals (non-`_persona()` call sites)

| File:line | Code | Classification | Rating |
|---|---|---|---|
| [engine.py:254](services/v2/engine.py#L254) | `persona="unknown"` (input_blocked event) | DECORATIVE — hardcoded constant, no logic difference vs `_persona()` here. | 7/10 |
| [engine.py:396](services/v2/engine.py#L396) | `persona="unknown"` (unknown_phone_gate event) | Same as above. | 7/10 |
| [engine.py:424](services/v2/engine.py#L424) | `persona="driver"` (quick_path_hit event) | Could call `_persona(identity)` for consistency, but identical outcome since Quick-Path path implies known user. | 7/10 |

**Cleanup option:** Replace with `_persona(identity)` call for consistency. Low priority, no behavioral change.

### Group D: V3 module persona — DEAD-via-dormant

| File:line | Code | Rating |
|---|---|---|
| [domain_picker.py:148, 174](services/v2/domain_picker.py#L148) | `persona: str = "driver"` param + `f"persona: {persona}"` in LLM prompt | 7/10 |
| [domain_scoped_picker.py:170-176](services/v2/domain_scoped_picker.py#L170-L176) | Persona filter via `tool_matches_audience` inside `_tools_in_domain` | 7/10 |
| [domain_scoped_picker.py:296](services/v2/domain_scoped_picker.py#L296) | `f"PERSONA: {persona}"` in LLM prompt | 6/10 |

**FILIP CONSTRAINT:** Prompt says *"Touch domain_picker / domain_scoped_picker logic — oni su V3 dormant, leave alone unless DEAD with extreme confidence"*. **Recommendation: leave V3 modules untouched.** Low confidence (≤7) means insufficient justification.

---

## FUNCTIONAL (must preserve)

| File:line | Code | Why FUNCTIONAL |
|---|---|---|
| [engine.py:137-139](services/v2/engine.py#L137) | `_persona()` binary mapper | Used in 20+ telemetry calls + identity_summary construction |
| [engine.py:148](services/v2/engine.py#L148) | `_minimal_identity()` embeds persona | Passed to executor for confused-deputy guard |
| [registry.py:32-64](services/v2/registry.py#L32-L64) | `_DRIVER_FRIENDLY_PREFIXES` (~63 tools) | Active driver whitelist used in production filtering |
| [registry.py:368-371](services/v2/registry.py#L368-L371) | `tool_matches_audience()` driver branch + None default | Production-active filter |
| [recognition.py:769-771](services/v2/recognition.py#L769-L771) | Audience filter in `_top_k` | Active in Recognition path (production) — filters driver candidates |
| [executor.py:91-98](services/v2/executor.py#L91-L98) | Confused-deputy guard | Final L7 security gate; defensively keeps even if upstream filters miss |
| [test_registry.py:180-191](tests/v2/test_registry.py#L180-L191) | Driver audience tests + None hint test | Cover production-active behavior |
| [test_recognition.py:358-374](tests/v2/test_recognition.py#L358-L374) | `test_recognize_driver_audience_filters_to_driver_tools` | Covers production-active filtering |

**Net: 8 entries that MUST stay. Removing any would break tests OR production routing.**

---

## DECORATIVE (in code, never fires with non-driver persona, but cheap to keep)

| File:line | Code | Notes |
|---|---|---|
| [recognition.py:700-702](services/v2/recognition.py#L700-L702) | Audience filter in `_entity_hierarchical_top_k` | Off in production (`V2_ENTITY_HIERARCHY=0`) |
| [executor.py:90-99](services/v2/executor.py#L90) (manager branch) | "unauthorized_persona" error path for non-driver | Never triggers because persona is binary |
| Telemetry persona logging (20 callsites) | persona=... in `_log_telemetry()` | **Field is DROPPED at engine.py:1193** before reaching KQL — analytical value zero, but routing value functional. |
| Test fixtures with persona param ([test_engine_smoke.py:147](tests/v2/test_engine_smoke.py#L147), [test_engine_v3_route.py:31](tests/v2/test_engine_v3_route.py#L31), [test_unified_responder.py:63](tests/v2/test_unified_responder.py#L63), [test_tool_use_responder.py:59](tests/v2/test_tool_use_responder.py#L59)) | Mocks accept persona param but don't assert behavior | Would PASS even if audience logic removed |

---

## DOCUMENTATION (description ≠ implementation)

11 occurrences across 6 docs files (Agent 12) describe manager/admin distinctions that aren't implemented:

| File:line | Quoted claim | Status |
|---|---|---|
| [docs/READ_FIRST.md:7](docs/READ_FIRST.md#L7) | "Croatian driver/manager sends a message" | Misleading: prod is driver-only |
| [docs/DEPLOY_PLAYBOOK_2026-05-07.md:179](docs/DEPLOY_PLAYBOOK_2026-05-07.md#L179) | "Manager 68% e2e (vs 82% driver)" | Unverifiable per F2 (bench JSONs pruned) |
| [docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md:157](docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md#L157) | "Manager prefixes expanded \| +60 LOC" | Tags exist; logic dead |
| [docs/FILIP_HANDOFF_150_ITEMS_2026-05-07.md:43](docs/FILIP_HANDOFF_150_ITEMS_2026-05-07.md#L43) | "Role change driver→manager handles in 5min" | Doc says 5min, code is 30s; manager flow doesn't exist |
| [docs/TUTORIALS.md:66](docs/TUTORIALS.md#L66) | "Manager/admin patterns are unknown" | Honest acknowledgment, but elsewhere acts as if exist |
| [domain_scoped_picker.py:149-152](services/v2/domain_scoped_picker.py#L149-L152) (docstring) | "Driver sees ~63 tools, Manager ~243, Admin all" | Numbers correct for whitelist sizes; behavior decorative |
| [executor.py:90-92](services/v2/executor.py#L90-L92) (comment) | "Even if router picks manager tool for driver" | Defensive doctrine but manager never set |
| [registry.py:67-69](services/v2/registry.py#L67-L69) (docstring) | "Manager has CRUD over fleet, persons, orgs..." | Categorizes but never invoked |
| [registry.py:29-31](services/v2/registry.py#L29-L31) (docstring) | "Treated as manager-surface unless audience says otherwise" | Defaults never reached |
| [docs/AUDIT_FINDINGS_2026-05-09.md:204](docs/AUDIT_FINDINGS_2026-05-09.md#L204) | "Admin may deactivate driver mid-session" | Refers to backend admin, not bot persona |
| [docs/FILIP_LEGAL_BIZ_OPS_TEMPLATES_2026-05-07.md:115](docs/FILIP_LEGAL_BIZ_OPS_TEMPLATES_2026-05-07.md#L115) | "Data subjects: drivers, managers, admins" | Aspirational scope |

**Cleanup option:** Update or delete misleading prose. Zero behavioral change. ~30 min total.

---

## Tests impact summary (Agent 11 deep dive)

If audience/manager logic removed from production code:

| Test | Impact | Effort to keep passing |
|---|---|---|
| [test_registry.py:180-183](tests/v2/test_registry.py#L180) `test_audience_no_hint_allows_everything` | PASS (driver-only doesn't change None-allows-all) | None |
| [test_registry.py:186-191](tests/v2/test_registry.py#L186) `test_audience_driver_restricts_to_friendly_prefixes` | PASS | None |
| [test_registry.py:194-204](tests/v2/test_registry.py#L194) `test_audience_manager_includes_driver_plus_fleet_finance_org` | **FAIL** | Drop test OR keep manager branch |
| [test_recognition.py:358-374](tests/v2/test_recognition.py#L358) | PASS (tests driver-side filtering) | None |
| 6 fixture tests with persona param | PASS | None |

**Bottom line: ONLY 1 test (`test_audience_manager_includes_driver_plus_fleet_finance_org`) would fail.** Either drop that test (if dropping manager branch) OR keep both.

---

## Phase 2 cleanup recommendation matrix

For each candidate group, three options:

### Option A: Aggressive cleanup (drop manager tier)
- Remove `_MANAGER_FRIENDLY_PREFIXES` (registry.py:69-102, ~33 LOC)
- Remove manager branch from `tool_matches_audience` (registry.py:372-373)
- Drop `test_audience_manager_includes_driver_plus_fleet_finance_org` test
- Update docstrings in registry.py to remove manager references
- **Total LOC reduction:** ~50 LOC + 1 test
- **Risk:** Loses "insurance" for future role layer (per Filip's "preserve audience tags" rule — VIOLATES this)
- **Reversibility:** Easy (git history)

### Option B: Documentation-only cleanup (preserve all logic)
- Update 11 misleading prose statements in docs/ + comments to reflect binary persona reality
- Add TODO markers in registry.py noting manager whitelist is dormant scaffolding
- Keep all code as-is
- **Total change:** Doc + comment edits only, ~30-60 min
- **Risk:** None (no behavior change)
- **Honors:** Filip's "preserve audience tags" rule
- **Drawback:** Code complexity unchanged

### Option C: Selective (middle ground)
- Drop token scope sending (`MOBILITY_SCOPE` env var + token_manager.py:159-161, 5 LOC)
- Drop hardcoded `"unknown"`/`"driver"` literals in favor of `_persona()` call (3 sites in engine.py, ~3 LOC)
- Update misleading docs/comments (option B)
- KEEP: `_MANAGER_FRIENDLY_PREFIXES`, manager branch in `tool_matches_audience`, V3 modules, all tests
- **Total LOC reduction:** ~10 LOC + ~30 min docs
- **Risk:** Minimal
- **Honors:** Filip's preservation rule
- **Drawback:** Mostly cosmetic

---

## Filip decision needed

Per Phase 2 protocol: **NE NASTAVI BEZ FILIP APPROVAL.**

Three concrete questions:

1. **Manager whitelist** (`_MANAGER_FRIENDLY_PREFIXES`): drop OR preserve?
   - Drop = Option A path. Loses insurance, gains LOC reduction.
   - Preserve = Option B/C path. Honors original prompt directive.

2. **Token scope** (`MOBILITY_SCOPE`): drop OR preserve?
   - Drop = ~5 LOC reduction, clarifies that bot doesn't use scope.
   - Preserve = Cheap to keep, possible future use.

3. **Documentation cleanup**: scope?
   - Just code comments?
   - Plus pivot docs (READ_FIRST, DEPLOY_PLAYBOOK, etc.)?
   - Plus FILIP_HANDOFF and FILIP_LEGAL docs?

Recommended path: **Option B (documentation-only cleanup) + selective Option C items if you want.**

Reason: Original prompt explicitly says "preserve audience tags for now". Aggressive cleanup violates that. Documentation cleanup eliminates misleading prose without touching code.

---

## Phase 1 self-rating

**Methodology:** 12 parallel agents, each with assigned subset, conservative classification, code citation required.

**Coverage:** ~95% of role/persona/audience occurrences inventoried. ~5% gaps acknowledged:
- Did not deep-dive every test fixture (6 mocks accepted as DECORATIVE without deep review)
- Did not exhaustively check non-v2/ services (auth flow, identity_context paths)

**Self-rating: 9/10.** Ready for Phase 2 decision. **Awaiting Filip's choice on Options A/B/C.**

---

## Phase 2 — EXECUTED 2026-05-09 (Filip choice: Option A + Token Scope)

### Changes applied (on disk, tests green)

| Change | Files | Status |
|---|---|---|
| Drop `_MANAGER_FRIENDLY_PREFIXES` (33 LOC) | [services/v2/registry.py](services/v2/registry.py) | ✅ done |
| Drop manager branch from `tool_matches_audience` | [services/v2/registry.py](services/v2/registry.py) | ✅ done |
| Drop `test_audience_manager_includes_driver_plus_fleet_finance_org` | [tests/v2/test_registry.py](tests/v2/test_registry.py) | ✅ done |
| Update registry.py top docstring (remove "manager-surface" claim) | [services/v2/registry.py](services/v2/registry.py) | ✅ done |
| Update `tool_matches_audience` docstring (binary persona reality) | [services/v2/registry.py](services/v2/registry.py) | ✅ done |
| Drop manager prefix import + persona dict | [scripts/auto_enrich_tkb.py](scripts/auto_enrich_tkb.py) | ✅ done |
| Drop `MOBILITY_SCOPE` field | [config.py](config.py) | ✅ done |
| Drop `self.scope = ...` + scope payload addition | [services/token_manager.py](services/token_manager.py) | ✅ done |
| Update executor.py confused-deputy comment | [services/v2/executor.py](services/v2/executor.py) | ✅ done |
| Update domain_scoped_picker.py docstring | [services/v2/domain_scoped_picker.py](services/v2/domain_scoped_picker.py) | ✅ done |

### Test verification

```
$ python -X utf8 -m pytest tests/v2/ tests/test_token_manager.py -q
============================= 725 passed in 3.84s =============================
```

All v2 + token_manager tests pass post-cleanup.

### Total LOC reduction

- ~50 LOC removed (manager whitelist + branch + test + token scope handling)
- ~6 LOC of comments/docstrings updated (no behavior change)
- 1 test removed (was testing dead branch)

### Items NOT cleaned up per Filip rules

- V3 modules (domain_picker.py, domain_scoped_picker.py persona param logic) — preserved per "leave V3 dormant alone" rule
- `_persona()` function itself — production-active in 20+ telemetry calls
- `_DRIVER_FRIENDLY_PREFIXES` — production-active driver filter
- Decorative telemetry persona logging (20 callsites) — out of Option A scope
- Pivot docs (READ_FIRST, DEPLOY_PLAYBOOK, FINAL_4_VERDICT, FILIP_HANDOFF, FILIP_LEGAL) — not touched per "DO NOT touch pivot docs" rule

### Git tracking caveat

Of 7 files modified: 2 are git-tracked (`config.py`, `services/token_manager.py`), 5 are untracked (`services/v2/*`, `tests/v2/*`, `scripts/auto_enrich_tkb.py`).

**Implication:** Spec'd "4 separate commits" cannot fully execute as clean diffs. Untracked-file changes exist on disk but cannot be committed as discrete-concept changes without first tracking the broader directories. Decision deferred to Filip.

**Status:** Phase 2 cleanup complete on disk. Git commit strategy pending Filip decision on directory-level tracking.
