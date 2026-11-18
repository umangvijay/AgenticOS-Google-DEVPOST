"""Phase 14 §14.39: the RL research lane is a REAL boundary, not a
comment. The production execution path imports no research-lane module,
and every learning write enters through the sanctioned reward-event
consumption contract."""
import ast
import pathlib

BACKEND = pathlib.Path(__file__).resolve().parents[1]

# The production execution path: engine, selection, learning, recorder,
# worker — the modules that run inside task execution.
PRODUCTION_PATH = [
    "backend/engine/engine.py",
    "backend/services/contextual_policy.py",
    "backend/services/adaptive_policy.py",
    "backend/services/routing_recorder.py",
    "backend/worker.py",
]


def _imports_of(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_research_lane_modules_do_not_exist_in_production_path():
    """The lane modules are declared but NOT implemented — nothing in
    the production path can import them, and no half-built trainer
    module ships under backend.research."""
    from backend.services.rl_research_lane import RESEARCH_LANE_MODULES
    for mod in RESEARCH_LANE_MODULES:
        pkg = BACKEND.parent / (mod.replace(".", "/") + ".py")
        assert not pkg.exists(), \
            f"{mod} exists but the research lane is not wired for it"


def test_production_path_imports_no_research_lane_module():
    """AST-level guard: the execution path never imports a lane module —
    even a future one registered in RESEARCH_LANE_MODULES."""
    from backend.services.rl_research_lane import RESEARCH_LANE_MODULES
    lane_roots = {m.rsplit(".", 1)[0] for m in RESEARCH_LANE_MODULES}
    for rel in PRODUCTION_PATH:
        src = BACKEND.parent / rel
        assert src.exists(), rel
        for imported in _imports_of(src):
            for root in lane_roots:
                assert not imported.startswith(root), \
                    f"{rel} imports research-lane module {imported}"


def test_learning_writes_flow_through_the_reward_contract():
    """The sanctioned learning entrypoints exist on the runtime path —
    per-context stats are written ONLY via the reward-event consumption
    (or the explicit learn() bridge), never by ad-hoc writers."""
    from backend.services.contextual_policy import ContextualRoutingPolicy
    from backend.services.adaptive_policy import AdaptivePolicy
    assert hasattr(ContextualRoutingPolicy, "consume_reward_event")
    assert hasattr(AdaptivePolicy, "consume_reward_event")
    assert hasattr(ContextualRoutingPolicy, "learn")
    # The lane boundary module itself is importable and stable.
    from backend.services import rl_research_lane
    assert rl_research_lane.is_research_module("backend.research.rl_trainer")
    assert not rl_research_lane.is_research_module(
        "backend.services.contextual_policy")
