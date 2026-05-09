# Routing System Audit — Findings Log

Living document. Findings appended chronologically (F1, F2, ...). Each finding is plain-format: claim → evidence → implication → status.

---

## F1 — Three Dead Opt-In Routing Paths

**Found:** 2026-05-09 (Faza 2.0+2.1, closed 10/10)
**Status:** Documented; decision pending Faza 2.0.5 (pivot docs read)

### Claim

V2Engine has **5 routing implementations**, but production env (`.env`) does not enable 3 of them. Recognition + Quick-Path are the only active paths.

### Evidence

**Implementations and their gates** ([services/v2/engine.py:381-535](services/v2/engine.py#L381-L535), in precedence order):

| # | Path | Entry line | Env flag | Default | Active? |
|---|---|---|---|---|---|
| 1 | Driver Quick-Path | [engine.py:381](services/v2/engine.py#L381) | none (always-on if config loads) | ON | ✅ |
| 2 | Tool-Use Responder | [engine.py:411](services/v2/engine.py#L411) | `V2_USE_TOOL_USE` | `"0"` | ❌ |
| 3 | Unified Responder | [engine.py:437](services/v2/engine.py#L437) | `V2_USE_UNIFIED_RESPONDER` | `"0"` | ❌ |
| 4 | V3 Hierarchical | [engine.py:463](services/v2/engine.py#L463) | `V2_USE_V3_ROUTER` | `"0"` | ❌ |
| 5 | Legacy V2 Recognition | [engine.py:533](services/v2/engine.py#L533) | none (fallback) | ALWAYS | ✅ |

**Production .env check** (2026-05-09):
```
$ grep -E "V2_USE|V2_ROUTER" c:/Users/filip/Desktop/damir/nova-verzija/.env
(no matches)
```
None of `V2_USE_TOOL_USE`, `V2_USE_UNIFIED_RESPONDER`, `V2_USE_V3_ROUTER` are set in production env. All defaults to `"0"`.

**Dead paths have full infrastructure:**
- Env flags wired
- Telemetry integration (`_log_telemetry` calls in each route)
- Mutation gate hooks (each path calls `mutation_gate.decide_mutation()`)
- Smoke tests via `monkeypatch.setenv` ([tests/v2/test_engine_smoke.py:227+](tests/v2/test_engine_smoke.py#L227))

**Pivot decision documents found:**
- [docs/UNIFIED_VS_V3_COMPARISON_2026-05-07.md](docs/UNIFIED_VS_V3_COMPARISON_2026-05-07.md)
- [docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md](docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md)
- [docs/DEPLOY_PLAYBOOK_2026-05-07.md](docs/DEPLOY_PLAYBOOK_2026-05-07.md)
- [docs/READ_FIRST.md](docs/READ_FIRST.md)

Cluster of 2026-05-07 dated docs suggests 3-architecture pivot decision happened that week. Contents not yet analyzed (Faza 2.0.5 task).

### Implication

Three possible outcomes for redesign discussion, each fundamentally different:

1. **Drop all 3 dead paths** — pre-redesign cleanup. Removes ~estimated 1500+ LOC + tests + docs. Recognition stays as production backbone.
2. **Activate one of dead paths** — possibly the redesign itself. V3 ship-ready per DEPLOY_PLAYBOOK. Unified is post-V3 pivot. Tool-Use is highest precedence experimental.
3. **Keep all 3 dormant** — only valid if pivot decisions explicitly defer activation pending external signal (frontier LLM availability, etc.).

### Status

**Decision blocked on:** Faza 2.0.5 (read 4 pivot docs, answer 4 questions).

Per plan early-exit clause: if pivot docs give clear verdict + action items, audit may terminate after 2.0.5 without full Recognition deep dive (Faza 2.2-2.6).

### Open question (deferred to 2.0.5)

Quick-Path is `always-on` and runs BEFORE all opt-in paths. Even with V3/Unified/Tool-Use enabled, Quick-Path intercepts ~40% driver traffic first. Is this intentional design or pre-pivot artifact? Pivot docs may answer.

---

## F2 — Deployment Gap: V3 Is Documented Default But Was Never Activated

**Found:** 2026-05-09 (Faza 2.0.5, complete read of 4 pivot docs)
**Status:** F1 must be **re-categorized** — this is more serious than "3 dead paths"
**Severity:** Higher than F1. F1 was "code without execution"; F2 is "documented production deploy that never happened".

### Claim

The 4 pivot docs (all dated 2026-05-07, READ_FIRST archived 2026-05-08) **explicitly designate V3+TKB as the default production architecture**, with a 7-step deploy playbook. The deploy was never executed. Production is running on Legacy V2 Recognition, which the docs describe as "LEGACY FALLBACK".

The "3 dead opt-in paths" framing in F1 is wrong. Reality:
- **V3 Hierarchical** = intended production default (deploy not executed)
- **Unified Responder** = deliberate opt-in escape hatch (waits for frontier LLM)
- **Tool-Use Responder** = empirically falsified, kept as opt-in for future model swap

### Evidence — answers to 4 questions

#### a) Why was each path built?

| Path | Origin | Empirical result |
|---|---|---|
| V3 Hierarchical | Replace V2 27% canonical accuracy with TKB-aware 2-stage chain (2026-05-06) | **81% canonical, 62% Slice B natural Croatian** ([UNIFIED_VS_V3_COMPARISON:11](docs/UNIFIED_VS_V3_COMPARISON_2026-05-07.md#L11)) |
| Unified Responder | Architecture pivot — ONE LLM call vs V3's 2-stage (2026-05-07) | 70% canonical (-11pp vs V3), tied 62% on natural, **+6pp on adversarial** |
| Tool-Use Responder | Native LLM tool_use loop (2-pass) | **FALSIFIED ON gpt-4o-mini** ([FINAL_4:41](docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md#L41)) — 33% canonical, "LLM responds 'Bok!' instead of calling tool for short queries" |

#### b) Why opt-in instead of deleted?

| Path | Reason for retaining |
|---|---|
| V3 | **Not opt-in by design.** Docs explicitly: "Wired by default. Toggle OFF: V2_USE_V3_ROUTER=0" ([FINAL_4:29](docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md#L29)). Code default is `"0"`. **Deployment gap.** |
| Unified | Deliberate. "Best path forward when frontier LLM lands" ([FINAL_4:36](docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md#L36)). Architecture predicts +5-15pp lift across all benches with Sonnet 4.7 / gpt-4o full. |
| Tool-Use | Deliberate. "Code retained as opt-in for future Sonnet 4.7 / gpt-4o full swap. Re-bench at that point." ([FINAL_4:48](docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md#L48)) |

#### c) Documented plan for production enablement?

**YES, complete.** [DEPLOY_PLAYBOOK_2026-05-07.md](docs/DEPLOY_PLAYBOOK_2026-05-07.md) has 7 steps:

1. Run `python scripts/v3_production_bootstrap.py` (verify readiness)
2. **Add to `.env`:** `V2_USE_V3_ROUTER=1`, `V2_TELEMETRY=1`, `V2_TOP_K=50` ← **NOT DONE**
3. Wire 4 stores into V2Engine bootstrap (DomainPicker, ScopedPicker, PendingClarifyStore, ConversationHistoryStore) ← partial wiring exists in code, needs verification
4. `docker-compose restart bot worker`
5. Smoke test 5 messages
6. Monitor first 48h for `v3_stage1`/`v3_clarify_top3`/`mutation_gate` events
7. Rollback if needed: `V2_USE_V3_ROUTER=0` + restart

Step 2 is the deployment gap. None of `V2_USE_V3_ROUTER`, `V2_TELEMETRY`, `V2_TOP_K` are set in production `.env` (verified 2026-05-09).

For Unified path: same playbook + `V2_ROUTER_DEPLOYMENT_NAME=<frontier>` + `V2_USE_UNIFIED_RESPONDER=1`. Conditional on frontier LLM availability.

#### d) Verdict from latest doc + implementation status

**Latest doc:** [FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md](docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md)

**Verdict (from doc:21-50):**
- ✅ **V3 + TKB**: "DEFAULT FOR PRODUCTION TODAY"
- ⚠️ **Unified single-call**: "OPT-IN ESCAPE HATCH" — when frontier LLM lands
- ❌ **Tool-use 2-pass loop**: "FALSIFIED ON gpt-4o-mini"
- 📁 **V2 retrieval+rerank**: "LEGACY FALLBACK. Kicks in when V3 confidence too low. Stays for safety net."

**Implementation status:** **NOT IMPLEMENTED.** Verdict says V3 is default, code says V3 is opt-in (default `"0"`), production `.env` does not enable V3.

Three possible explanations (cannot distinguish without Damir input):

1. **Forgotten deploy step** — playbook was written, step 2 never executed. Most likely scenario.
2. **Intentional pause** — deploy pre-flight step 1 (`python scripts/v3_production_bootstrap.py`) failed and never resolved.
3. **Strategic hold** — Filip waiting on Damir-export real production queries (per READ_FIRST:122 "external blocker") before flipping switch.

### Open question (Quick-Path) — **RESOLVED**

Per [READ_FIRST:14-21](docs/READ_FIRST.md#L14) and [FINAL_4:111](docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md#L111), Quick-Path is **part of the V3 architecture** — L2 layer, runs BEFORE V3 Stage 1, handles ~40% driver hot-path with 0 LLM cost. It's not standalone or pre-pivot artifact. Designed to coexist with V3 (and Unified, when activated).

### Implications

**Original audit assumption was wrong.** The redesign discussion is not "Recognition is broken, what should replace it?". It's:

> "Why was V3 (the documented production default with measured 80% effective accuracy) never activated, and is the path forward (a) execute the deploy, or (b) document why we're not?"

This **fundamentally changes the audit direction**:

- ❌ Faza 2.2-2.6 Recognition deep dive is **wrong target** — Recognition is documented as legacy fallback, not the system to redesign.
- ❌ "Routing redesign" framing is wrong — there's no redesign to do, the design exists and is empirically validated, just not deployed.
- ✅ Real questions: (1) Does `python scripts/v3_production_bootstrap.py` still pass? (2) Is the engine bootstrap wiring (DEPLOY_PLAYBOOK step 3) actually complete? (3) Why was the deploy not executed?

### Status — Audit pivot recommended (per plan early-exit clause)

Per plan decision tree row 1:
> | Pivot docs kažu Recognition je deprecated u korist V3/Unified | **Audit pivot.** NE deep dive Recognition. Ressuscitate dead path or document indecision. |

This matches **exactly**. Recommendation: **terminate audit Fazas 2.2-8** as currently scoped. New scope:

**Faza 2.2' (replacement) — V3 Deploy Readiness Audit (~2-4h):**
1. Run `python scripts/v3_production_bootstrap.py` — does it still pass?
2. Verify engine.py bootstrap has DomainPicker + ScopedPicker + PendingClarifyStore + ConversationHistoryStore wired
3. Re-run benches: `python -X utf8 -m pytest tests/v2/ -q` (expect 431+ pass per READ_FIRST:108)
4. Quick V3 smoke test in dev (no production deploy)
5. Document deployment gap reasons (ask Damir/Filip)

**Decision for Filip:** continue audit on V3 deploy gap, OR audit completely complete (questions answered, redesign discussion is misframed) and pivot to deploy-or-document conversation with Damir.

### F2 update (Faza 4, 2026-05-09) — cost claim verified + TOP_K caveat

**Cost claim partially verified:**
Pivot docs claim "$0.0006 per query" ([FINAL_4_VERDICT:14](docs/FINAL_4_ARCHITECTURE_VERDICT_2026-05-07.md#L14)). Faza 4.4 code-level estimate yields ~$0.0006-0.0009 per Recognition Judge call (gpt-4o-mini @ Azure pricing per [.env.example:90-91](.env.example#L90)). **Cost claim consistent with actual implementation.**

**Important nuance:** This does NOT verify accuracy claims (81% canonical / 62% Slice B). Those benchmark JSONs were pruned in commit `02267e0` and are unreproducible from current code. **Cost is deterministic from code; accuracy is not.**

**TOP_K inconsistency caveat (additional finding, Filip catch 2026-05-09):**
- Code default: `V2_TOP_K=20` ([recognition.py:100](services/v2/recognition.py#L100))
- Pivot docs default: `V2_TOP_K=50` ([DEPLOY_PLAYBOOK Step 2:61](docs/DEPLOY_PLAYBOOK_2026-05-07.md#L61))

**Hidden bug surface for V3 deploy:** if V3 ever activated, env values from DEPLOY_PLAYBOOK would set TOP_K=50, but code default is 20. Bench numbers (if real) were measured at one of these — unclear which. If active production differs from benched config, accuracy projection is invalid even if code path is correct.

This adds 4th implication category to F2:
4. **Activation requires environment validation step** — not just toggle V2_USE_V3_ROUTER=1 but also verify V2_TOP_K matches benched config.

### F2 update (Faza 5, 2026-05-09) — third aspirational-numbers caveat

**Pattern continues.** Faza 5 audit found another unverified pivot-docs accuracy claim:

> "A+B+C clarify-rescuable = 43.5% on entity_hybrid config — user-perceived accuracy ~2x of strict 1-shot." ([clarify_ui.py:18-22](services/v2/clarify_ui.py#L18-L22))

This is the third specific number from pivot-docs era that cannot be verified from current repo state:
1. 81% canonical accuracy (unverifiable, bench JSONs pruned)
2. 62% Slice B natural accuracy (same)
3. **43.5% Top-3 clarify rescue rate (new)**

All three follow F2 pattern: docstring/doc cites specific empirical number, no result file exists in repo, regenerating requires re-running benches Filip never executed.

**Caveat:** ako V3 deploy ever happens, all three need fresh measurement. Don't assume aspirational numbers will hold under current registry/anchors/TKB state — those have drifted since 2026-05-07.

### F2 update (Faza 7, 2026-05-09) — third sub-pattern: outdated doc facts

D14 (identity TTL 30s vs docs 5min) is **distinct category** from unverifiable benchmark claims:

**[identity.py:40-44](services/v2/identity.py#L40):**
> "TTL conservative — Pass 4 DEVIL critique: admin may deactivate driver mid-session. 30s caps the staleness window at one minute worst case. Worth the safety."

The 30s is **intentional safety**, with documented rationale. [READ_FIRST.md:13](docs/READ_FIRST.md#L13) "5-min Redis cache" is **wrong-doc-fact** (code changed, docs didn't follow), not aspirational.

**F2 final categorization — 3 sub-patterns:**

| Sub-pattern | Examples | Resolution |
|---|---|---|
| **Unverifiable benchmark claims** | 81% canonical, 62% Slice B, 43.5% rescue | Re-bench required (result JSONs pruned) |
| **Aspirational deploy steps** | `scripts/v3_production_bootstrap.py` cited in DEPLOY_PLAYBOOK Step 1, doesn't exist | Build script before deploy attempt |
| **Outdated doc facts** | Identity 5-min cache (actual 30s), TOP_K=50 docs vs 20 code | Either update docs OR change code to match docs |

All three suggest pivot docs were hastily written or AI-generated without parallel code verification. **Pivot docs are unreliable as planning material without code-level cross-check.**

---

## F4 — Drift bug surfaces (code-level duplications)

**Found:** 2026-05-09 (Faza 4.1 + Filip catch + Faza 3.2 cumulative)
**Status:** Documented; trivial fixes available, not breaking production

### Claim

Multiple value-equivalent constants and concepts are **defined in 2+ places without shared import**, creating drift surfaces where one change requires synchronized edits across files. Production not currently broken; latent risk.

### Evidence

#### Drift surface #1: `MIN_RATIONALE_LEN`

Defined identically (=30) in two production-active modules:
- [recognition.py:101](services/v2/recognition.py#L101) — gates `llm_conf` cap (≥30 → keep, <30 → cap at 0.5)
- [confidence_gate.py:32](services/v2/confidence_gate.py#L32) — gates HIGH zone admission

**Risk:** If recognition's interpretation changes (e.g., to 50), confidence_gate's 30 stays — decisions become inconsistent. LLM Judge would be told "min 30" but gate would gate at 30, while recognition could downgrade at 50.

**Fix:** Single canonical definition in `services/utils/` constants module, import both places. ~15 min.

#### Drift surface #2: `is_mutation` ↔ `mutation_safe` (already in F3)

- [`processed_tool_registry.json` tool field `is_mutation`](config/processed_tool_registry.json) (boolean)
- [`tool_knowledge_base.json` tool field `mutation_safe`](config/tool_knowledge_base.json) (boolean, **inverted**)

Same data, opposite polarity, no automated sync. Bug surface if generators run independently.

**Note:** TKB is dormant per F1, so currently low-impact. If V3 ever activates, both must agree.

**Fix:** Either drop TKB entirely (F3 cleanup) OR generate TKB from registry with inversion in single place.

#### Drift surface #3: TOP_K code vs docs

Already covered in F2 update. Code 20, docs 50.

**Fix:** Decide canonical value, update one to match the other. Document the rationale.

#### Drift surface #4: 3-way description redundancy

Same conceptual field across 3 files:
- `processed_tool_registry.json` → `description` + `summary` (from Swagger)
- `tool_knowledge_base.json` → `intent_summary` (Croatian, generated)
- `rich_tool_docs.json` → `description` (Croatian, manual subset)

Per F3 cleanup, dropping TKB + rich removes 2 of 3 redundancies. registry stays as canonical Swagger source.

#### Drift surface #5: `method` 3-way redundancy

- `processed_tool_registry.json` → `method` (from Swagger HTTP method)
- `tool_knowledge_base.json` → `method` (copied)
- `config/driver_quick_path.json` patterns → implicit via `tool` field (e.g., `delete_VehicleCalendar_id` encodes DELETE)

Driver_quick_path is parsing tool_id prefix as method. Redundant with registry. NISAM SIGURAN je li ovo intentional (defensive duplicate) ili technical debt.

### Implication

F4 is the **technical debt category** of audit findings. None of these break production today, but each is a small bug surface. Most resolve naturally if F3 cleanup executes (drop TKB, drop rich, drop categories). One (#1 MIN_RATIONALE_LEN) is independent and trivially fixable.

**Total trivial-fix work to eliminate F4 surfaces:** ~30-60 min (after F3 cleanup decisions made).

### F4 update (Faza 5, 2026-05-09) — 2 additional drift surfaces

#### Drift surface #6: `post_VehicleCalendar` range declared but not enforced

[mutation_gate.py:46-48](services/v2/mutation_gate.py#L46-L48) declares:
```python
"post_VehicleCalendar": {
    "from_time_max_days_ahead": 90,
    "to_time_after_from": True,
}
```

But [`_check_range()`](services/v2/mutation_gate.py#L143) implementation at [line 153-169](services/v2/mutation_gate.py#L153-L169) handles ONLY `post_AddMileage`. For `post_VehicleCalendar`, fallthrough at [line 171-172](services/v2/mutation_gate.py#L171-L172) returns `None` → no range violation ever fires.

**Behavior:** booking 5 years in advance passes mutation_gate as **single CONFIRM** instead of expected **DOUBLE**. Documented protection is illusory.

**Severity:** Low (single confirm still requires Da/Ne). But user-facing safety claim doesn't match implementation.

**Fix:** Either implement `_check_range` for VehicleCalendar OR remove the dead config entry. ~1h.

#### Drift surface #7: `mutation_ranges.json` referenced but never created

[mutation_gate.py:36-39](services/v2/mutation_gate.py#L36-L39) docstring says:
> "Real production values come from config/mutation_ranges.json (per CLAUDE.md: data in JSON, not Python). For v2 POC we ship sensible defaults."

`config/mutation_ranges.json` **does not exist** in repo. Hardcoded Python defaults are the only source of truth. Module documents itself as transitional but transition never completed.

**Severity:** Low — Python defaults work. But violates CLAUDE.md "data in JSON, not Python" doctrine cited in own docstring.

**Fix:** Either extract to JSON OR update docstring to remove false promise. ~30 min.

**F4 total drift surfaces: 7.**

---

## F5 — Functional integration gaps in production-active path

**Found:** 2026-05-09 (Faza 6.1-6.2)
**Status:** Documented; **two real bugs**, not just drift

### Claim

Two pieces of safety/observability infrastructure exist in code (defined, tested) but are **NEVER ACTUALLY INVOKED in the production-active Recognition path**. Result: silent functional degradation that bench testing can't catch (because tests run with paths/configs different from production env).

### Evidence

#### F5.1 — `turn_number` permanently stuck at 1 in production

`engine.py:182-188` derives turn_number for telemetry:
```python
turn_number = 0
if self.conversation_history_store is not None:
    try:
        hist = await self.conversation_history_store.load(phone)
        turn_number = (len(hist) if hist else 0) + 1
    except Exception:
        turn_number = 0
```

But `conversation_history_store.append()` is called ONLY in dormant routes:
- [engine.py:423](services/v2/engine.py#L423) — Tool-Use Responder route (V2_USE_TOOL_USE=0 in production)
- [engine.py:449](services/v2/engine.py#L449) — Unified Responder route (V2_USE_UNIFIED_RESPONDER=0)
- [engine.py:503](services/v2/engine.py#L503) — V3 route (V2_USE_V3_ROUTER=0)

**Recognition (production-active) fallthrough at [engine.py:533](services/v2/engine.py#L533) does NOT append.**

Therefore in production:
- Every turn: `load(phone)` returns `[]`
- `[] if []` → falsy → returns 0
- `turn_number = 0 + 1 = 1`

**Effect:** Every TelemetryEvent in production logs `turn_number=1`. Damir's "this is turn 5 of conversation" support context is silently broken. Cross-turn analysis impossible from telemetry alone.

**Note:** This was masked by an earlier bug fix that switched `.get(phone)` → `.load(phone)` (mentioned in conversation summary). The method-name fix made the code RUN, but didn't fix the underlying issue: appends still don't happen on production path.

**Fix:** Add `conversation_history_store.append(phone, ConversationTurn(...))` at end of Recognition path success (around [engine.py:600-618](services/v2/engine.py#L600)). ~30 min.

#### F5.2 — `token_budget.py` is orphan dead code

[`services/v2/token_budget.py`](services/v2/token_budget.py) (205 LOC) defines `TokenBudgetStore` with sliding 1-min window, 50K tokens/phone budget. Documented as DoS defense per "brain-dump #99".

**Grep across services/v2/:** ZERO imports of `TokenBudgetStore` or `token_budget`. Only references:
- Module itself
- Architecture invariant test ([test_architecture.py:110](tests/v2/test_architecture.py#L110)) which says `tenant_rate_limit composes TokenBudgetStore`
- Unit tests

But **`tenant_rate_limit` module DOES NOT EXIST** in `services/v2/`. The composing module was never built.

**Effect:** No per-phone token budget enforcement in production. DoS defense documented but not active. Existing rate_limiter caps queries-per-minute but NOT per-query token cost (per token_budget.py docstring lines 1-7).

**Severity:** Operational risk if a buggy client or attacker sends large queries. Damir scale (1 client) makes this low-priority but real.

**Fix options:**
- Drop module + tests (per F3 cleanup) if Filip decides DoS defense unnecessary at current scale
- OR build the missing `tenant_rate_limit` module that composes TokenBudgetStore (~3-5h)

### Implication

F5 differs from F1-F4 in that these are **active functional bugs**, not merely:
- F1: dead code (still works if activated)
- F2: aspirational claims (true → unverifiable)
- F3: orphan configs (loaded but unused)
- F4: drift surfaces (latent)

F5 means **production-active path has silent gaps**. Telemetry shows fewer "turn 3+" events than reality. DoS protection documented but absent. These are the kind of bugs that don't show up in tests because tests don't exercise the exact env config production runs under.

**Total fixable work for F5:**
- F5.1 (turn_number append): ~30 min
- F5.2 (token_budget): ~30 min drop OR ~3-5h build missing tenant_rate_limit

### Status

- F5.1: high-priority quick fix (telemetry actually used by Damir)
- F5.2: low-priority pending Filip decision (current scale = no real DoS)

---

## F3 — Orphan config files from dormant paths

**Found:** 2026-05-09 (Faza 3.1-3.2)
**Status:** Documented; drop candidates listed. Decision pending Filip post-audit.

### Claim

Of 7 plan-listed config files, **4 (57%) are exclusively used by dormant or inactive paths** in production. Plus 2 V1 legacy config files exist that V2 routing never accesses. Total ~1.74MB orphan/dormant JSON in `config/`.

### Evidence — usage map

**Production-active (Quick-Path + Recognition):**
- `processed_tool_registry.json` (3.3MB) → [registry.py:3](services/v2/registry.py#L3)
- `tool_anchor_enrichments.json` (1.3MB) → [registry.py:202](services/v2/registry.py#L202) sidecar
- `driver_quick_path.json` (13KB) → [driver_quick_path.py:31](services/v2/driver_quick_path.py#L31)

**Dormant — V3/Unified paths only (per F1, none enabled in `.env`):**
- `tool_domains.json` (11KB) → [domain_picker.py:36](services/v2/domain_picker.py#L36) only
- `rich_tool_docs.json` (24KB) → [domain_scoped_picker.py:31](services/v2/domain_scoped_picker.py#L31) only
- `tool_knowledge_base.json` (1.5MB) → [domain_scoped_picker.py:32](services/v2/domain_scoped_picker.py#L32) + [unified_retriever.py:23](services/v2/unified_retriever.py#L23)

**Dead-data in production (loaded but consumer OFF):**
- `tool_categories.json` (188KB) — loaded by [registry.py:214](services/v2/registry.py#L214), consumer is [recognition.py:579 _apply_hierarchy_filter](services/v2/recognition.py#L579) only when `enable_hierarchy=True` (default OFF in production)

**V1 legacy (not in plan list):**
- `entity_descriptions.json` (11KB) → [services/faiss_vector_store.py:43](services/faiss_vector_store.py#L43) only
- `context_param_schemas.json` (3.3KB) → [services/api_capabilities.py:112](services/api_capabilities.py#L112) + [services/registry/swagger_parser.py:26](services/registry/swagger_parser.py#L26)

### Cross-file sync

| Pair | Tool count | Status |
|---|---|---|
| registry ↔ anchors | 950/950 | ✅ Perfect sync |
| registry ↔ TKB | 950/950 | ✅ Perfect sync (TKB is dormant) |
| registry ↔ categories | 950/950 | ✅ Perfect sync |
| registry ↔ rich_docs | 80 of 950 + 3 stale | ⚠️ 9% subset by design + 3 orphan entries |

**No automated cross-file consistency check exists.** Sync je manual coordination via lockstep generator runs.

### Field redundancy + drift surfaces

- `is_mutation` (registry) vs `mutation_safe` (TKB) — **inverted boolean semantics**, same data, opposite polarity. Bug surface.
- `method` redundantly stored in registry, TKB, and quick_path patterns
- `description` redundantly in registry (`description`/`summary`), TKB (`intent_summary`), rich (`description`)

### Drop candidates (post-audit cleanup, IF Filip decides)

| File | Drop with | Risk |
|---|---|---|
| `tool_domains.json` | + domain_picker.py | V3 unrecoverable without regen |
| `rich_tool_docs.json` | + domain_scoped_picker.py portion | Same |
| `tool_knowledge_base.json` | + unified_retriever.py + scoped_picker | Same + Unified locked |
| `tool_categories.json` | + sidecar load + `enable_hierarchy` opt-in | Smaller — feature was OFF anyway |
| `entity_descriptions.json` | + V1 module if confirmed unused | Faza 7 D6 dependency |
| `context_param_schemas.json` | + V1 module if confirmed unused | Faza 7 D6 dependency |
| `services/v2/hallucination_guard.py` (154 LOC) | + `hallucination_check_fn` injection in tool_use_responder | Tool-Use Responder is also drop candidate (whole opt-in path dead) |
| `services/v2/token_budget.py` (205 LOC) | + tests/v2/test_token_budget.py | Documented `tenant_rate_limit` consumer never built; orphan |

**Effort:** ~3-5h cleanup. **Reversibility:** Irreversible without redoing F2-era work.

### Implication

F3 reinforces F1+F2: deprecated infrastructure represents ~50%+ of audit-target complexity. Cleanup work is its own category, separate from "audit" — should not be conflated.

### F3 update (Faza 5+6, 2026-05-09) — 8 drop candidates total

Added by Faza 5: `hallucination_guard.py` (Tool-Use only consumer, dormant).
Added by Faza 6: `token_budget.py` (no production consumer).

**Total cleanup target:** 6 config files (~1.74MB) + 2 Python modules (~360 LOC) + their generators + tests.

### F3 update (Faza 7, 2026-05-09) — D6 reclassification

**`entity_descriptions.json` REMOVED from drop list.** Faza 7 grep revealed [services/registry/__init__.py:201](services/registry/__init__.py#L201) calls `initialize_faiss_store` which loads `entity_descriptions.json` at registry init. **Active at startup**, not orphan.

**`context_param_schemas.json` REMAINS drop candidate** — used only by [services/registry/swagger_parser.py:26](services/registry/swagger_parser.py#L26), which is build-time only (called by `scripts/sync_tools.py`). Drop reduces ability to re-run sync_tools without manual schema reconstruction. Lower priority.

**Hybrid V1+V2 architecture finding:** Production runs V1 modules (faiss_vector_store + api_capabilities) at INIT TIME alongside V2 routing layer. V2 doesn't call V1 directly at request time, but V1 provides initialization-time setup. This means **dropping V1 modules without understanding init-time data flow would break startup**. Pre-cleanup audit needed.

### F3 final drop list (revised post-Faza 7)

| File | Rationale | Risk |
|---|---|---|
| `tool_categories.json` (188KB) | Loaded but consumer OFF (recognition.py.enable_hierarchy=False default) | Smaller — feature was OFF anyway |
| `tool_domains.json` (11KB) | V3 dormant | If V3 activated, regen needed |
| `rich_tool_docs.json` (24KB) | V3 dormant | Same |
| `tool_knowledge_base.json` (1.5MB) | V3 + Unified dormant | Same + Unified locked |
| `context_param_schemas.json` (3.3KB) | Build-time only | sync_tools.py rebuild needs schemas |
| `services/v2/hallucination_guard.py` (154 LOC) | Tool-Use only consumer (dormant) | If Tool-Use activated, regen wiring |
| `services/v2/token_budget.py` (205 LOC) | Orphan, no consumer in production | None — fully unused |

**Revised total: ~1.55MB JSON + ~360 LOC Python** (down from original 1.74MB after entity_descriptions reclassified).

**Removed from drop list:**
- ~~`entity_descriptions.json`~~ (active at registry init via faiss_vector_store)

---

## F6 — Mental model gaps (Faza 8 verification)

**Found:** 2026-05-09 (Faza 8.1 prediction exercise)
**Status:** Documented; defines audit completeness boundary

### Claim

Faza 8.1 prediction exercise hit **8.5/10** on 10-query mental simulation. Below 9/10 target. The 1.5 deficit reveals **2 specific gaps** in agent's audit-derived understanding.

### Evidence

**Q4 ("moja vozila") and Q5 ("rezervacijejedne") — PARTIAL HIT:**

Predicted path correctly (Quick-Path miss → Recognition → Judge → score combine → L5 gate).
Could NOT predict exact score range without running the embedder live.

**Specific gap:** Audit produced complete code-level understanding but did NOT verify:
1. Actual ada-002 cosine values for specific Croatian queries vs registry anchors
2. Actual LLM Judge confidence outputs for ambiguous queries
3. Cross-turn behavior under real Redis load

**These gaps require live system execution**, not code reading. Audit was structured as **read-only static analysis** per its design.

### Failure mode coverage gaps (Faza 8.2 MEDIUM confidence)

2 of 8 failure modes had MEDIUM (not HIGH) prediction confidence:

- **FM4 Postgres downtime:** Architecture inference, not direct trace of every Postgres caller across services/. Out of audit scope (Faza 7 covered Postgres only at integration level, not exhaustive callers).
- **FM6 10000-char user message:** input_sanitizer length enforcement assumed but not verified. Module wasn't in Faza 5 scope (focused on guards: mutation_gate, confidence_gate, pending stores).

### Implication

F6 sets **honest boundary on audit deliverable**:

✅ **Audit produced:** Complete static mental model — paths, magic constants, decision trees, integrations, state lifecycle.

⚠️ **Audit did NOT produce:** Empirical confirmation that production behaves as static model predicts. Requires live execution validation.

### Recommendation

If empirical validation needed before redesign decisions:
- Run small benchmark suite (10-30 queries) against live Recognition path
- Capture actual anchor scores + Judge confidences for distribution
- Confirms or invalidates F2 unverifiable benchmark claims
- Effort: 2-4h (depends on Damir-export availability)

If Filip willing to act on static-only model:
- F1-F5 findings stand on code evidence alone
- F2 benchmark numbers remain unverifiable but framing is sound
- Production bugs (F5.1, F5.2) confirmed without execution
- Redesign decisions backed by code structure, not measured behavior

### Status

This finding is the **audit's honest self-boundary**. Static analysis approach was efficient for finding F1-F5, but cannot replace live measurement for performance/accuracy validation.

---

## Post-audit cleanup execution log

### 2026-05-09 — Phase 0 (F5 quick fixes)

- F5.1 turn_number bug — **RESOLVED**. process_message refactored into wrapper + _dispatch_message; redundant inline appends in dormant V3/Unified/Tool-Use paths removed. 722→714 tests (8 token_budget tests dropped per F5.2). All green.
- F5.2 token_budget orphan — **RESOLVED**. services/v2/token_budget.py + tests/v2/test_token_budget.py deleted. tests/v2/test_architecture.py KNOWN_MODULES + tenant_rate_limit invariant updated. 213 LOC + 8 tests removed.

### 2026-05-09 — Phase 2 (Role simplification, Filip Option A)

Per `docs/ROLE_SIMPLIFICATION_INVENTORY.md` Phase 1 inventory + Filip Option A directive:

**Manager-tier removal:**
- `_MANAGER_FRIENDLY_PREFIXES` constant dropped from registry.py (~33 LOC)
- Manager branch dropped from `tool_matches_audience` (~3 LOC)
- `test_audience_manager_includes_driver_plus_fleet_finance_org` test dropped
- `auto_enrich_tkb.py` manager import + persona dict dropped

**Token scope dead infrastructure:**
- `MOBILITY_SCOPE` field dropped from config.py
- `self.scope` + scope payload sending dropped from token_manager.py (~5 LOC)

**Comments/docstrings updated:**
- registry.py top docstring (removed "manager-surface" misleading claim)
- registry.py `tool_matches_audience` docstring (binary persona reality)
- executor.py confused-deputy comment (clarified defensive vs functional)
- domain_scoped_picker.py `_tools_in_domain` docstring (binary + V3 dormant note)

**Test verification:** 725 pass / 0 fail.

**Items NOT touched per Filip rules:**
- V3 modules persona logic (preserved per "leave V3 dormant alone")
- `_persona()` function itself (production-active)
- `_DRIVER_FRIENDLY_PREFIXES` (production-active driver filter)
- Pivot docs (READ_FIRST, DEPLOY_PLAYBOOK, FINAL_4_VERDICT, FILIP_HANDOFF, FILIP_LEGAL) — preserved per "DO NOT touch pivot docs"
- Decorative telemetry persona logging — out of Option A scope
