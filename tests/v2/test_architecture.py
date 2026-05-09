"""Architecture invariants for services/v2.

Encodes the layered design:

  - L-1 rate_limiter, L0 identity, L0.5 pii_scrubber, L1 special_intents,
    L2a intent_type, L2b driver_basics, L3 recognition, L4 flow_engine,
    L5 confidence_gate, L6 mutation_gate, L7 executor, L8 formatter
  - Only the orchestrator (engine.py) may pull siblings together.
  - No cycles.
  - No layer reaches into another's private surface (`._private`).

If any rule fails the build, that is a structural drift signal —
either the design changed (update this file deliberately) or someone
crossed a boundary that should stay clean.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

V2_DIR = Path(__file__).resolve().parents[2] / "services" / "v2"

LEAF_MODULES = {
    "__init__",
    # Core V2Engine layers (L-1 through L8)
    "rate_limiter",          # L-1 per-phone QPM cap
    "pii_scrubber",          # L0.3 redact PII before logs
    "input_sanitizer",       # L0.5 prompt-injection guard (user input)
    "identity",              # L0 phone → personId
    "intent_type",           # L2a 4-way intent classifier
    "driver_basics",         # L2b anchor index for driver self-questions
    "driver_quick_path",     # L2 deterministic regex routing
    "recognition",           # L3 anchor + judge fallback
    "domain_picker",         # L3a Stage 1 V3 hierarchical
    "domain_scoped_picker",  # L3b Stage 2 V3 hierarchical
    "unified_responder",     # L3 alt — one-call routing + params + response
    "unified_retriever",     # FAISS adapter for UnifiedResponder + ToolUse
    "tool_use_responder",    # L3 alt — 2-pass OpenAI function calling
    "flow_engine",           # L4 multi-step state machine
    "confidence_gate",       # L5 execute / clarify / fallback
    "clarify_ui",            # L5.5 Top-3 cards
    "pending_clarify",       # L5.5 state for "1"/"2"/"3" replies
    "mutation_gate",         # L6 confirm dialog (POST/PUT/DELETE)
    "pending_mutation",      # L6 confirm state
    "executor",              # L7 API call + circuit breaker + idempotency
    "output_sanitizer",      # L7.5 indirect-prompt-injection guard
    "hallucination_guard",   # L7.5 response vs api_data sanity
    "formatter",             # L8 Croatian template
    # Cross-cutting helpers
    "registry",              # v2 tool registry
    "telemetry",             # structured event logging
    "conversation_history",  # multi-turn context
    "query_normalizer",      # L1.5 query rewrite
    "crisis_detector",       # L0.7 suicidal-signal redirect
    "negation_handler",      # standalone "nemoj/ne" recognizer
    "multi_intent_detector", # split detection
    "reference_resolver",    # anaphoric follow-ups
    "meta_intents",          # self-reference / handover / OOS
    "special_intents",       # welcome / GDPR / help
    # L3 sub-helpers (used only by recognition.py)
    "hierarchical_entity_retrieval",
    "confusion_disambiguator",
    # Infra wired outside engine.py
    "azure_rate_guard",      # used by openai_client wrapper
    "cache_invalidation",    # used by /admin/cache-invalidate route
    "latency_ux",            # used by V2Engine.process_message_chunked
}

ORCHESTRATOR = "engine"


def _v2_files() -> list[Path]:
    return sorted(V2_DIR.glob("*.py"))


def _v2_imports(path: Path) -> set[str]:
    """Return v2-sibling module names imported by `path`."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=path.name)
    siblings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "services.v2":
                for n in node.names:
                    siblings.add(n.name)
            elif mod.startswith("services.v2."):
                siblings.add(mod.split(".")[2])
    return siblings


SIBLING_IMPORTS_ALLOWED = {
    # recognition (L3) may pull its internal sub-components for entity-level
    # hierarchical retrieval and per-cluster confusion disambiguation.
    # Single-direction composition (recognition uses, not the reverse).
    "recognition": {
        "hierarchical_entity_retrieval",
        "confusion_disambiguator",
    },
    # confidence_gate (L5) renders Top-3 fallback via clarify_ui (L5.5).
    # Lazy-imported inside _context_fallback to avoid import cycles —
    # this is a single-direction render-helper composition.
    "confidence_gate": {"clarify_ui"},
    # domain_scoped_picker (L3b Stage 2) needs Stage 1 metadata.
    # Single-direction: scoped picker reads domain definitions from picker.
    "domain_scoped_picker": {"domain_picker"},
    # SECURITY composition (audit fix 2026-05-08): tool_use_responder
    # and unified_responder embed identity fields (vehicle_name, etc.)
    # into the LLM system prompt at Pass 1. Those fields come from the
    # backend API and are NOT user-input (so input_sanitizer doesn't
    # cover them); without sanitization, a corrupted/maliciously-set
    # vehicle_name like "[INST: ...]" could shift LLM behavior.
    # Both responders now reuse `output_sanitizer._sanitize_string`
    # via a single-direction import. The architecture test invariant
    # treats this the same as recognition→clarify_ui composition.
    "tool_use_responder": {"output_sanitizer"},
    "unified_responder": {"output_sanitizer"},
}


def test_only_orchestrator_imports_siblings():
    """All cross-module wiring belongs to engine.py.

    Documented exceptions: SIBLING_IMPORTS_ALLOWED — for sub-components
    that exist only to support a single layer.
    """
    offenders = []
    for path in _v2_files():
        stem = path.stem
        if stem in (ORCHESTRATOR, "__init__"):
            continue
        if stem not in LEAF_MODULES:
            offenders.append(f"unknown module: {stem}")
            continue
        siblings = _v2_imports(path)
        allowed = SIBLING_IMPORTS_ALLOWED.get(stem, set())
        unauthorized = siblings - allowed
        if unauthorized:
            offenders.append(f"{stem}.py imports siblings: {sorted(unauthorized)}")
    assert not offenders, "Layer boundary violations:\n  " + "\n  ".join(offenders)


def test_orchestrator_imports_only_known_modules():
    """engine.py must reference only the registered v2 layers."""
    engine_path = V2_DIR / f"{ORCHESTRATOR}.py"
    siblings = _v2_imports(engine_path)
    unknown = siblings - LEAF_MODULES
    assert not unknown, f"engine.py imports unknown v2 modules: {unknown}"


def test_no_layer_imports_engine():
    """Cycle guard — no leaf may import the orchestrator."""
    bad = []
    for path in _v2_files():
        if path.stem in (ORCHESTRATOR, "__init__"):
            continue
        if ORCHESTRATOR in _v2_imports(path):
            bad.append(path.stem)
    assert not bad, f"Modules importing engine (cycle risk): {bad}"


def test_no_private_attribute_access_across_modules():
    """No file outside a module reaches into its `._private` surface.

    Allowed: `self._foo` within the same class (intra-module). Banned:
    `other_object._foo` where `other_object` came from another v2 module.
    """
    offenders = []
    for path in _v2_files():
        if path.stem == "__init__":
            continue
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith("_") \
                    and not node.attr.startswith("__"):
                # Skip self.* / cls.*
                if isinstance(node.value, ast.Name) and node.value.id in ("self", "cls"):
                    continue
                # Skip dataclass-style fallbacks (e.g. obj._asdict)
                if node.attr in ("_asdict", "_replace", "_fields"):
                    continue
                offenders.append(
                    f"{path.name}:{node.lineno} private access "
                    f"{ast.unparse(node) if hasattr(ast, 'unparse') else node.attr}"
                )
    assert not offenders, "Private cross-module access:\n  " + "\n  ".join(offenders)


def test_every_leaf_module_has_unit_tests():
    """Each layer ships with its own test file (HTTP-coupled modules
    are exempt because their tests live in integration suites)."""
    integration_only = {"executor"}
    tests_dir = Path(__file__).resolve().parent
    have_tests = {
        p.stem.replace("test_", "")
        for p in tests_dir.glob("test_*.py")
    }
    missing = []
    for mod in LEAF_MODULES - {"__init__"} - integration_only:
        if mod not in have_tests:
            missing.append(mod)
    assert not missing, f"Modules without unit tests: {missing}"


def test_v2_module_count_is_known():
    """Sanity check — if a new module appears, the architecture test
    must be updated explicitly so its boundaries are deliberate."""
    actual = {p.stem for p in _v2_files()}
    expected = LEAF_MODULES | {ORCHESTRATOR}
    new = actual - expected
    deleted = expected - actual
    assert not new, f"New v2 modules need entry in test_architecture.py: {new}"
    assert not deleted, f"Removed v2 modules still listed in test: {deleted}"
