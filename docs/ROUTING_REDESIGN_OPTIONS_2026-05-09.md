# Routing Redesign — Decision Framework (post-Phase 2)

**Date:** 2026-05-09
**Context:** After Phase 2 aggressive role removal + verification pass.
**Decision needed from:** Filip (with Damir consult where flagged).

This doc is **decision input**, not a plan. Filip picks one direction, then we plan execution.

---

## Current state (verified, post-Phase 2)

| Metric | Value | Source |
|---|---|---|
| Production-active path | Quick-Path → Recognition (single LLM Judge) | Faza 2.1 audit + Phase 2 cleanup |
| L2 Quick-Path match rate on real Filip corpus | **96.7% (29/30)** | bench_real_corpus_l2.py executed 2026-05-09 |
| L2 Quick-Path correct routing when matched | **23/24 (95.8%)** | same |
| Wrong routing | **0** | same |
| Tests passing | 709 / 709 | pytest tests/v2/ |
| Docker build | ✅ mobilityone-bot:phase2-rolecleanup | docker build verified 2026-05-09 |
| Dormant code | V3 + Unified + Tool-Use (env flags off) | F1 finding |
| V3 numbers (81% canonical / 62% Slice B) | ⚠️ unverifiable | F2 finding (bench JSONs pruned) |
| Recognition production accuracy | TBD | benchmark in progress |
| Production deployment | ❌ months-old image, code on disk only | Filip stated |

---

## Five possible directions

Each option is self-contained — pick one and plan execution.

### Option A: Status Quo + Production Deploy
**Action:** Deploy current disk state (Phase 2 cleanup applied) to production. Gather real telemetry for 2-4 weeks. Iterate based on data, not speculation.

**Effort:** 1-3h (build → push → k8s rollout)
**Cost:** 0 LLM API (already in budget)
**Risk:** LOW — proven Quick-Path covers 96.7% of real driver corpus. Recognition handles the 3.3% via existing path.
**Reversibility:** HIGH — old image still in registry, k8s rollback available.

**Why this might be right:** Production isn't broken (Damir hasn't complained). New disk state has Phase 2 cleanup that reduces complexity. Deploying gets the benefit shipped, then real telemetry replaces speculation about routing accuracy.

**Why this might be wrong:** Doesn't address F2 (V3 questions still unanswered) or F4 (drift surfaces still present). Punts redesign to "after we have data" which may never come.

### Option B: V3 Hierarchical Activation + Re-bench
**Action:** Set `V2_USE_V3_ROUTER=1` in `.env`, re-run benches against current registry/anchors/TKB. If V3 ≥ Recognition by ≥5pp, switch primary. If not, drop V3 code per F3.

**Effort:** 1-2h re-bench + decision
**Cost:** ~$0.50-1.00 LLM (multiple bench runs)
**Risk:** MEDIUM — V3 prompt construction works (smoke tests pass), but real accuracy under current config is unknown. F2 means historical 81% number is unreliable.
**Reversibility:** HIGH — env flag toggle.

**Why this might be right:** Resolves F2 finally. Either V3 wins (deploy it, drop Recognition fallback) or V3 loses (drop V3 code per F3, save 1500+ LOC).

**Why this might be wrong:** Spends LLM API budget on a question Filip might not need answered. If V3 wins by < 5pp, still ambiguous. Pivot docs reference scripts (v3_production_bootstrap.py) that don't exist.

### Option C: Drop All Dormant Code (Aggressive Cleanup #2)
**Action:** Remove V3 (domain_picker, domain_scoped_picker, ~50 LOC), Unified (unified_responder, ~200 LOC), Tool-Use (tool_use_responder, ~280 LOC). Drop config files: tool_domains, rich_tool_docs, tool_knowledge_base. Keep Recognition + Quick-Path only.

**Effort:** 3-5h (cleanup + tests + docs update)
**Cost:** 0 LLM API
**Risk:** LOW behavior-wise (dropping unreachable code), HIGH option-wise (commits to Recognition-only future).
**Reversibility:** GIT REVERT only (~1 commit). No re-implementing 1500 LOC easily.

**Why this might be right:** F1 + F3 finding shows ~50% of audit complexity is dormant. Dropping it makes bot honestly reflect what it does. Aligns with CLAUDE.md "minimum koda".

**Why this might be wrong:** Loses option to activate V3/Unified later if Damir's needs grow. F2 unverified means we don't know if V3 was actually better — we'd never find out.

### Option D: Targeted Recognition Improvements
**Action:** Keep architecture as-is. Improve Recognition specifically:
- Better anchor enrichments (more Croatian phrases per tool)
- Tune confidence_gate thresholds (currently 0.78/0.7/0.65/0.5)
- Self-consistency (judge_self_consistency=3, currently 1)
- Add LLM-as-retriever fallback for vocab-stripped queries

**Effort:** 5-10h (design + tune + bench)
**Cost:** $1-3 LLM API (multiple bench runs for tuning)
**Risk:** MEDIUM — tuning thresholds without real production data is guessing.
**Reversibility:** HIGH (tuning changes).

**Why this might be right:** Recognition is production-active path. Improving it has direct user impact. F4 drift surfaces partly addressed (single source of truth for thresholds, etc.).

**Why this might be wrong:** Optimizing without production telemetry. Damir's actual queries (96.7% Quick-Path) suggest Recognition handles only edge cases — improving edge case accuracy may not move the needle.

### Option E: New Architecture (Rebuild from scratch)
**Action:** Design new routing layer informed by audit findings + Damir feedback. Probably involves dropping current 5-implementation system + replacing with cleaner 1-2 layer approach.

**Effort:** 20-50h+ (design + impl + bench + deploy)
**Cost:** $5-20 LLM API
**Risk:** HIGH — months of work with no guaranteed outcome.
**Reversibility:** Effectively NONE — by the time it's done, current state is too stale to revert to.

**Why this might be right:** Clean slate. No dormant code debt. No legacy assumptions.

**Why this might be wrong:** No evidence current state is broken at user level (96.7% Quick-Path on real corpus). "Redesign" without identified problem = scope creep. CLAUDE.md says "ne over-engineerizam za scale koji nemam".

---

## My honest read (per CLAUDE.md format)

### (1) Konkretna preporuka

**Option A (Status Quo Deploy) → Option C (Drop Dormant) sequence.**

Rationale:
- Real corpus shows Quick-Path covers 96.7% of Filip's actual queries
- Recognition handles the rest (Phase 2 cleanup verified intact)
- Damir's bot is months out of sync with disk — deploying current state IS the biggest win available right now
- After deploy, gather 2-4 weeks production telemetry, then drop dormant code (Option C) backed by data

Time estimate: 1-3h deploy this week + 3-5h cleanup in 4-6 weeks.

### (2) Glavna slabost preporuke

You'd be committing to Recognition-only future without measuring V3 first. F2 question stays unanswered forever (V3 might have been better, we'd never know). If Damir's needs grow (multi-role, manager workflows), Recognition-only might hit ceiling.

### (3) Alternativa za drugog korisnika

Engineer with appetite for measurement: **Option B (V3 re-bench)**. Spend $1 + 2h to definitively answer F2, then decide based on data. Risk-tolerant but evidence-driven.

Engineer with redesign appetite: **Option E**. Months of work but ends with clean architecture. Only makes sense if current state is empirically broken (no evidence yet).

### (4) Trošak ako pogriješim

- Option A wrong → deploys Phase 2 changes that fail in production. Cost: emergency rollback (10 min), maybe 1-2 hours debugging. Recoverable.
- Option A wrong + Option C wrong → cleaned up code that V3 future would need. Cost: 3-5h to re-implement IF Damir ever needs it. Probably never.
- Worst case: 6-8 hours of total wasted work over 2-3 months. Bounded.

---

## Damir consultation needed before any option

These questions don't have technical answers, only business answers:

1. **Is Damir actively using the bot?** If yes (real users, real usage): production deploy is urgent (current disk state has Phase 2 + F5.1 telemetry fix). If no (testing only): timeline is relaxed.

2. **What's the role situation?** Phase 2 removed manager/admin tier. If Damir intended bot to handle manager workflows (fleet stats, person management), we degraded that. If driver-only is fine, Phase 2 is correct.

3. **Latency tolerance?** Recognition path is ~1-2s. Quick-Path is ~50ms. If Damir's users tolerate 1-2s, current architecture works. If they want faster, expand Quick-Path coverage from 96.7% upward.

4. **Cost sensitivity?** Recognition costs ~$0.0007/query. Quick-Path costs $0. Driver might have 100-1000 queries/day = $0.07-$0.70/day. Acceptable?

---

## Filip's choice

Pick one of A/B/C/D/E (or hybrid). Then we plan execution.

If you choose A (deploy + measure), next step is figuring out your deploy mechanism (which you said you don't remember). I can help reconstruct from Dockerfile + k8s manifests, but actual `docker push` to registry needs your credentials.

If you choose B (V3 re-bench), I can run benches autonomously (~$1 cost). Then we decide based on numbers.

If you choose C (drop dormant), I can execute autonomously over 3-5h. Tests will show if anything breaks.

If you choose D or E, you need to specify scope before I can do anything.

**Default if no choice made:** Option A (deploy current state). Lowest-risk highest-value action.
