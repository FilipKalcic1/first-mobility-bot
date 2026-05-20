# Round Protocol — Pre/In/Post-flight discipline

This document codifies the iteration discipline for rolling-architecture
work on the bot. Each "round" (one merged version bump, e.g. 11.0.4) MUST
go through these checklists. The point: catch overconfidence and stale
plans before they cost a day of misdirected work.

The protocol is non-negotiable for solo work. Skipping pre-round because
"I already know" is the exact failure mode it exists to prevent.

---

## PRE-ROUND CHECKLIST (5 minutes, before writing any code)

Open a fresh empty section in CHANGELOG.md draft titled `## <next version>
— <round name>`. Then answer the seven questions below in writing. If
any answer is "I'm not sure", STOP and design more before coding.

### 1. Is this round still the right priority?

What did the previous round teach us that might re-order what's next?
Are any of the next 3 rounds suddenly redundant or higher-priority?

If the previous round had a measurable result (Top-1 score, latency,
cost), is the round about to start the highest-leverage change given
that result? If not, swap.

### 2. Are dependencies satisfied?

List each input the round needs:
- Code that must already exist
- Data that must be enriched / mined
- Decisions that must be made (e.g. role detection signal source)

If any are missing, either:
- (a) Schedule the dependency as a sub-round before this one
- (b) Mark the round as blocked and switch to one whose deps are met

### 3. Success metric — concrete and measurable

NOT: "improve retrieval"
YES: "Top-1 recall on `eval_dataset_holdout.jsonl` increases by ≥ 5pp"
YES: "62% empty parameter descriptions → 0% (validated by registry scan)"
YES: "p95 end-to-end latency drops from current X to ≤ Y seconds"

The metric must be:
- Quantitative (a number)
- Pre-existing or trivially-computable baseline
- Verifiable post-round in <5 min

If you can't write the metric, you don't know what success looks like —
finish design first.

### 4. Cost — days, dollars, risk

- Days: realistic estimate, including testing + integration + rollback prep
- Dollars: API calls (LLM), infra (Redis storage, Postgres rows), embedding
  regeneration if relevant
- Risk: 1-5 (1 = test-covered refactor, 5 = touches hot path with no canary)

If days > 5 or risk ≥ 4, split the round into smaller commits with
explicit rollback points.

### 5. Rollback plan

What's the recovery procedure if the round breaks production?
- Code: `git revert <range>` — must be bisectable
- Data: must include backup-before-write (see existing patterns in
  `scripts/repair_registry_classifications.py` for atomic file writes)
- Config: previous version of config file kept alongside new one for 1
  release cycle minimum

If you can't write a rollback in 30 seconds, the round is too coupled.

### 6. Battle-readiness rating (1-10)

Self-assess:
- 9-10: every algorithm has pseudocode, every edge case has a handler,
  every test is enumerable
- 7-8: main path is clear, edge cases identified but not all handled
- 5-6: direction is clear but algorithms hand-waved
- 1-4: this is a sketch, not a plan

If rating < 6, add a "design" sub-round of 1-3 hours **before** coding.
The design output is: this checklist filled out, and a `docs/design_<roundname>.md`.

### 7. Cascade — what changes for rounds N+1 through N+12?

Walk forward through the remaining plan. For each subsequent round:
- Does this round's outcome change its priority?
- Does it satisfy a dependency for any future round?
- Does it make any future round redundant?

Update the plan in `docs/plan_rounds.md` (kept current).

---

## IN-FLIGHT — micro-checkpoints

While coding, every ~30 minutes ask yourself:

- "Am I still solving what I started?" (scope creep check)
- "Did I just add complexity I haven't justified?" (bloat check)
- "Did I just hardcode something?" (doctrine check)
- "What would the devil-advocate of this design say?"

If any answer is suspicious, stop, write it down, decide. Do NOT
keep coding through doubt — that's how technical debt accrues.

---

## POST-ROUND CHECKLIST (5 minutes, after PR merge / final commit)

Write a 5-line section in `docs/round_log.md` per round:

### 1. Did it hit the success metric?

Pass / Fail / Partial. With number.

If Fail or Partial: WHY? (root cause, not symptom)

### 2. What surprised you?

Anything you didn't predict. This is the most valuable signal — surprises
mean your model of the system was wrong somewhere.

### 3. What's now possible?

What change is unlocked? Which future rounds become easier or doable?

### 4. What's now unnecessary?

Which planned rounds are now redundant or no longer top priority?

### 5. Update the plan

Edit `docs/plan_rounds.md`:
- Strike through completed round
- Move re-prioritized rounds
- Adjust day estimates if last round taught us something about velocity

### 6. Critical audit pass (≥ 1 round per major slice)

After 3+ versions in a slice (e.g. P4 → 12.4.0 / 12.5.0 / 12.6.0), do a
**dedicated audit round** before declaring the slice done:

- **Cross-check the contract**: read the LLM prompt, the dispatch
  code, and the test expectations *as separate documents*. Each must
  describe the same behavior. (Found 12.7.1 START_FLOW + plan bug.)
- **Adversarial inputs**: pretend you're attacking your own code —
  corrupt JSON in conv state, missing fields, malicious user input,
  guest contexts. Add a regression test for each. (Found 12.7.1
  parked-plan deserialize raise.)
- **Real-world drift**: deterministic stubs return the same value
  every call. Production data drifts between message-1 and message-2.
  For any path that involves a confirm + resume, write a test that
  asserts call-count or value-pinning to catch dialog/action races.
  (Found 12.7.2 resume re-runs GET correctness bug.)
- **Single-chokepoint security gates**: any safety check
  (guest gate, mutation gate, GDPR consent) must live at the
  innermost callable, not at every entry path. Multiple entry-path
  copies drift. (Found 12.7.2 guest-mutation gate duplication.)

Audit rounds get their own patch version (`12.7.1`, `12.7.2`...) so
they are bisectable and visibly billed. They are not optional.

---

## SELF-CRITICISM PROMPTS

Copy these into your head before each pre-round answer:

- "Where am I optimistic right now?"
- "What number am I citing that I haven't actually measured?"
- "What complexity am I rationalizing as 'pragmatic'?"
- "What hardcoding am I about to wave through?"
- "If a critical reviewer read this, what would they call out?"
- "Am I planning more because I'm avoiding the doing?"

Council voices to consult:
- ARCHITECT (no hardcoding, registry-driven, agnostic)
- PRAGMATIST (ship it, 80/20)
- DEVIL (challenges every premise, especially confidence)

---

## ROUNDS WHEN PROTOCOL FAILED

When this protocol fails to catch a problem, log it here so we improve it:

- (none yet — first formal use is round 12.0)

---

## VERSIONING

Round versions follow semver-with-roll: each round is a patch bump
(11.0.X). Major bumps (12.0.0) reserved for breaking architectural shifts.
The CHANGELOG.md entry IS the post-round log; keep them concise.
