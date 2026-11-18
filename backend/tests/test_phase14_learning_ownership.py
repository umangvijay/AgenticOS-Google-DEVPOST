"""Phase 14 §14.8: LEARNING OWNERSHIP — the runtime writes learned
stats ONLY through the sanctioned entrypoints (the reward-event
consumption contract and the explicit learn/record bridges). Nothing in
the execution path writes contextual or global stats directly.

This is an AST-level structural guard, so a future edit that opens a
side door (e.g., the engine updating stats from its own outcome guess)
fails here, not in production.
"""
import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[1]

# Modules that RUN inside task execution.
RUNTIME_MODULES = [
    "backend/engine/engine.py",
    "backend/services/web_agent.py",
    "backend/worker.py",
    "backend/services/effect_service.py",
]

# The ONLY learning-write calls the runtime may make.
ALLOWED_WRITE_CALLS = {
    "consume_reward_event",   # §14.18 atomic consumption
    "learn",                  # §14.1 the policy's explicit bridge
    "record",                 # the legacy global learner's bridge
    "meter_improvement_call", # governance metering (not learning)
}


def _call_names(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def test_runtime_never_writes_stats_directly():
    """The execution path never calls the raw stat writers."""
    forbidden = {"record_outcome", "record_context_outcome"}
    for rel in RUNTIME_MODULES:
        src = BACKEND.parent / rel
        assert src.exists(), rel
        used = _call_names(src)
        for bad in forbidden:
            assert bad not in used, \
                f"{rel} writes learned stats directly via {bad} — learning " \
                "must flow through the sanctioned reward contract (§14.8)"


def test_the_sanctioned_entrypoints_exist_and_are_the_contract():
    from backend.services.contextual_policy import ContextualRoutingPolicy
    from backend.services.adaptive_policy import AdaptivePolicy
    # consume_reward_event IS the atomic write path (§14.18).
    assert hasattr(ContextualRoutingPolicy, "consume_reward_event")
    assert hasattr(AdaptivePolicy, "consume_reward_event")
    # learn() routes through the repo's context outcome (the ONE bridge).
    import inspect
    learn_src = inspect.getsource(ContextualRoutingPolicy.learn)
    assert "record_context_outcome" in learn_src, \
        "learn() is the sanctioned bridge into the context stats"


def test_reward_classification_stays_in_one_module():
    """The reward mapping is specified ONCE (§14.17) — the recorder and
    the evaluators import it; a second mapping would let the two
    learners disagree about what a verified outcome is worth."""
    from backend.services.routing_recorder import POLICY_VERSION
    from backend.services.contextual_policy import reward_from_class
    from backend.services.offline_policy_evaluation import REWARD_WEIGHTS
    for cls in ("verified", "partial", "uncertain", "rejected", "missing"):
        assert reward_from_class(cls) == REWARD_WEIGHTS.get(cls, 0.0), \
            f"reward disagreement for class {cls}"
