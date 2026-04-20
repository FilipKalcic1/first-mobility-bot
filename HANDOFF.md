# Handoff

Orientation for a new maintainer. Pair with [ARCHITECTURE.md](ARCHITECTURE.md) and [DEPLOYMENT.md](DEPLOYMENT.md).

## First hour

1. Read [ARCHITECTURE.md](ARCHITECTURE.md). The whole routing pipeline fits on one page.
2. Skim [config.py](config.py). Every env var that matters is there with defaults.
3. Run the suite: `pytest`. Baseline should be green.
4. Run the retrieval benchmark: `pytest tests/benchmarks/test_tool_recognition.py`. Current plateau is Recalibrated Top-5 ≈ 69–78% depending on synonym state; see [CHANGELOG.md](CHANGELOG.md).

## First day

- Trace a message end-to-end using [tests/test_full_bootstrap_e2e.py](tests/test_full_bootstrap_e2e.py) and [tests/test_masterdata_alias.py](tests/test_masterdata_alias.py). Those two cover webhook → routing → Graph Discovery → payload.
- Read [services/unified_router.py](services/unified_router.py) and [services/unified_search.py](services/unified_search.py). All routing decisions flow through these.
- Read [services/dependency_resolver/resolver.py](services/dependency_resolver/resolver.py). Graph Discovery is the project's differentiator — understanding it is non-optional.

## Working on retrieval quality

The benchmark is the authority. Dual-seed protocol:

- `tests/benchmarks/tool_recognition_paraphrases.json` — seed=42 (primary)
- `tests/benchmarks/tool_recognition_paraphrases.seed1337.json` — held-out

Any change that moves recall must pass the gate on **both** seeds. Revert fast if one regresses.

Known dead ends (do not retry without new evidence):
- LLM classifier as a TFI veto — regressed Recalib −17pp.
- English `op_id` anchors inside Croatian embeddings — dilutes semantics in ada-002.
- Unbounded synonym expansion — cross-contamination between tools.

## Working on the tool registry

1. Edit upstream Swagger or `config/tool_documentation.json`.
2. Run `python scripts/sync_tools.py` — regenerates `config/processed_tool_registry.json`.
3. Run `python scripts/generate_tool_embeddings.py` to regenerate `.cache/tool_embeddings.json`.
4. Run benchmark on both seeds before merging.

## Deploying

See [DEPLOYMENT.md](DEPLOYMENT.md). Single-pod, `Recreate` strategy, ~30–60 s downtime per deploy. Pre-flight with `scripts/verify_production_readiness.py`.

## What lives where (cheat sheet)

| Question | Look here |
| --- | --- |
| "Why is the bot routing this query wrong?" | [services/unified_router.py](services/unified_router.py) logs + benchmark replay |
| "Why is a required field missing?" | [services/dependency_resolver/resolver.py](services/dependency_resolver/resolver.py) |
| "Why did the API call 400?" | [services/api_gateway.py](services/api_gateway.py) + [services/error_learning.py](services/error_learning.py) |
| "Why did we answer in English?" | [services/response_formatter.py](services/response_formatter.py), [services/message_engine.py](services/message_engine.py) |
| "Why did we log a phone number?" | We didn't — `scripts/verify_production_readiness.py` would have failed CI. If it did, that's a bug — fix and add a test |
| "Where is consent stored?" | `user_mappings.consent_*` columns, migration `002` |

## People and process

- Architecture directives and standards: **Damir (Principal Architect)**.
- Implementation and maintenance: **Filip**.
- All architectural changes require sign-off before merging.

## Non-negotiables

1. PII never leaves a masking boundary (see [SECURITY.md](SECURITY.md)).
2. Regex `detected_entity` is the **only** TFI veto trigger. LLM signals are advisory.
3. Never commit raw `.env` or unsealed secrets.
4. Benchmarks on **both** seeds before merging retrieval changes.
5. Single-pod resource limits (1 CPU / 1 GiB) are a product constraint, not a bug — design within them.
