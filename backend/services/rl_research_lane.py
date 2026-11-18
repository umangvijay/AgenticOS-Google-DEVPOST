"""RL research lane (Phase 14 §14.39) — the PRODUCTION/RESEARCH boundary.

What is IN PRODUCTION (the only learning that touches the runtime):

- `ContextualRoutingPolicy` (§14.2/§14.5): a context-bucketed UCB1 bandit
  with epsilon-mixture exploration, learning ONLY from delayed VERIFIED
  outcomes joined from durable evaluations, with logged selection
  probabilities (§14.4), an exploration budget (§14.15), drift
  monitoring (§14.5), and quarantine-isolated malformed rewards
  (§14.21). Every update flows through durable reward events consumed
  atomically (§14.18) — auditable, replayable, bounded.

What is RESEARCH-LANE ONLY (never wired into the runtime):

- Weight training / gradient updates over model parameters or embedding
  policies. These require an offline training pipeline, dataset
  governance, and evaluation gates far beyond the online bandit's, and
  NONE of it runs inside task execution.
- Off-policy policy-gradient or Q-learning over the decision log. The
  log supports DESCRIPTIVE analysis and propensity-supported IPS
  evaluation (§14.21/§14.22) — counterfactual policy OPTIMIZATION on
  production traffic is not enabled.

The boundary is enforced, not aspirational: the guard test
(`test_rl_research_lane.py`) asserts that the execution path
(engine → selection → learning) imports nothing from any
weight-training/research module, and that every learning write goes
through the reward-event consumption contract.

Adding to the research lane: register the module in RESEARCH_LANE_MODULES
below. Importing a lane module from the production path fails the guard
test — the lane stays a lane until a future phase graduates it with its
own governance.
"""
from __future__ import annotations

# Modules that belong to the research lane ONLY — the production
# execution path must never import them (guarded by test).
RESEARCH_LANE_MODULES: tuple[str, ...] = (
    "backend.research.rl_trainer",
    "backend.research.policy_gradient",
    "backend.research.offline_rl",
)

# The ONLY sanctioned learning write-path into production stats.
PRODUCTION_LEARNING_ENTRYPOINTS: tuple[str, ...] = (
    "backend.services.contextual_policy.ContextualRoutingPolicy"
    ".consume_reward_event",
    "backend.services.adaptive_policy.AdaptivePolicy.consume_reward_event",
    "backend.services.contextual_policy.ContextualRoutingPolicy.learn",
    "backend.services.adaptive_policy.AdaptivePolicy.record",
)


def is_research_module(module_name: str) -> bool:
    """True when the module is registered research-lane-only."""
    return str(module_name) in RESEARCH_LANE_MODULES
