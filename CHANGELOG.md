# Changelog

## 11.0.2 — current

- Consolidated on single-pod Kubernetes deployment (1 CPU / 1 GiB). New Kustomize manifests under [k8s/](k8s/).
- Project documentation rewritten from scratch: README, ARCHITECTURE, DEPLOYMENT, SECURITY, HANDOFF.
- Removed reverted retrieval experiments (V7 Sandwich, LLM-as-TFI-veto, English op_id anchors) and their stale benchmark artifacts.
- Removed mock-only tests that were testing stubs rather than real behaviour (`test_ai_orchestrator_fixes.py`, `test_ai_orchestrator_simple.py`, `test_schema_integration.py`, `test_redis_stress.py`).
- Removed one-shot helper scripts that had completed their purpose (`swagger_watcher.py`, `kpi_metrics.py`, `generate_intent_training_data.py`, `auto_improve_documentation.py`, a handful of others — see `git log`).
- Updated [scripts/verify_production_readiness.py](scripts/verify_production_readiness.py) header to match the real 1 CPU / 1 GiB target.
- Fixed stale docstring in [tests/test_masterdata_alias.py](tests/test_masterdata_alias.py) pointing to the current `scripts/sync_tools.py`.

## 11.0.x — prior

- Dual-seed retrieval benchmark protocol (seed=42 primary + seed=1337 held-out). Dual-seed gate is now the merge criterion for any retrieval change.
- Graph Discovery V6 (alias-aware) proved end-to-end by [tests/test_masterdata_alias.py](tests/test_masterdata_alias.py) — full phone → person → vehicle chain.
- Synonym injection targeted at worst-50 misses was the only retrieval change that held up on both seeds. Heuristic boosters (LLM TFI veto, English-token sandwich) regressed and were reverted.
- `HALF_OPEN_PROBING` state added to the circuit breaker to avoid thundering herds on recovery.
- Hallucination capture path: write-only via `bot_user`, read/review restricted to `admin_user`.

## 10.x and earlier

Pre-consolidation history lives in git. Use `git log -- path/to/file` for module-level archaeology.
