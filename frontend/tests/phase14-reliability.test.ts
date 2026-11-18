/**
 * Phase 14 §14.35: React reliability — the pure, reliability-critical
 * UI contracts hold as unit tests (vitest):
 *  - the outcome state is SERVER-DERIVED and never upgrades an
 *    unverified execution to "verified" (§18.1/§12.2);
 *  - every engine state maps to an explicit label (unknown values
 *    degrade honestly);
 *  - the motion policy honors reduced-motion at every tier (§19.6).
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import {
  runStateStyle, outcomeFromEvaluation, outcomeStateStyle,
  connectorStateBadges,
} from "@/lib/task-states";
import {
  motionPreference, decorativeMotionAllowed, essentialTransitionsAllowed,
} from "@/lib/motion";

describe("§14.35 outcome derivation is server-derived truth", () => {
  it("never upgrades an unverified execution to verified", () => {
    expect(outcomeFromEvaluation({
      success: false, uncertain_effects: ["fx-1"],
    })).toBe("uncertain");
    expect(outcomeFromEvaluation(null)).toBe("pending_evaluation");
    expect(outcomeFromEvaluation(undefined)).toBe("pending_evaluation");
  });

  it("orders the honest precedence: needs_review > partial > uncertain > failed", () => {
    expect(outcomeFromEvaluation({
      success: false, needs_review_tasks: ["t1"],
      partially_applied_effects: ["fx"], uncertain_effects: ["fx2"],
      failed_tasks: 1,
    })).toBe("needs_review");
    expect(outcomeFromEvaluation({
      success: false, partially_applied_effects: ["fx"],
      uncertain_effects: ["fx2"], failed_tasks: 1,
    })).toBe("partial");
    expect(outcomeFromEvaluation({
      success: false, uncertain_effects: ["fx"], failed_tasks: 1,
    })).toBe("uncertain");
    expect(outcomeFromEvaluation({ success: false, failed_tasks: 1 }))
      .toBe("failed");
  });

  it("verified requires the evaluation to actually say success", () => {
    expect(outcomeFromEvaluation({ success: true })).toBe("verified");
    expect(outcomeFromEvaluation({})).toBe("uncertain");
  });

  it("every outcome state renders explicit and attention-aware", () => {
    for (const state of ["verified", "partial", "uncertain", "needs_review",
      "failed", "pending_evaluation", "unavailable"] as const) {
      const style = outcomeStateStyle(state);
      expect(style.label.length).toBeGreaterThan(0);
      if (state !== "verified" && state !== "pending_evaluation") {
        expect(style.needsAttention).toBe(true);
      }
    }
  });
});

describe("§14.35 run states are explicit", () => {
  it("maps known engine states to honest labels", () => {
    expect(runStateStyle("NEEDS_REVIEW").label).toBe("Needs review");
    expect(runStateStyle("OUTCOME_UNCERTAIN").label)
      .toBe("Outcome uncertain");
    expect(runStateStyle("WAITING_APPROVAL").needsAttention).toBe(true);
    expect(runStateStyle("COMPLETED").needsAttention).toBe(false);
  });

  it("degrades unknown values honestly instead of pretending", () => {
    expect(runStateStyle("SOMETHING_NEW").label).toBe("SOMETHING_NEW");
    expect(runStateStyle(null).label).toBe("Unknown");
  });
});

describe("§14.35 connector lifecycle badges separate the rungs", () => {
  it("generated-but-untested is not 'tested' and needs review", () => {
    const labels = connectorStateBadges({
      trust_tier: "unverified", artifact_type: "adapter",
      is_enabled: false,
    }).map((b) => b.label);
    expect(labels).not.toContain("tested");
    expect(labels).toContain("needs review");
    expect(labels).not.toContain("active");
  });

  it("a verified, tested, active connector shows all three", () => {
    const labels = connectorStateBadges({
      trust_tier: "verified", artifact_type: "adapter",
      is_enabled: true,
      test_coverage: { read_probe_passed: 3, total: 3 },
    }).map((b) => b.label);
    expect(labels).toContain("tested");
    expect(labels).toContain("verified");
    expect(labels).toContain("active");
  });
});

describe("§14.36 motion policy tiers", () => {
  const originalMatchMedia = window.matchMedia;

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
    document.documentElement.classList.remove("reduce-data");
    vi.restoreAllMocks();
  });

  function mockReducedMotion(reduced: boolean) {
    window.matchMedia = ((query: string) => ({
      matches: reduced && query.includes("prefers-reduced-motion"),
      media: query, onchange: null, addListener: vi.fn(),
      removeListener: vi.fn(), addEventListener: vi.fn(),
      removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia;
  }

  it("full motion by default", () => {
    mockReducedMotion(false);
    expect(motionPreference()).toBe("full");
    expect(decorativeMotionAllowed()).toBe(true);
    expect(essentialTransitionsAllowed()).toBe(true);
  });

  it("prefers-reduced-motion: reduce is the STATIC tier — everything off", () => {
    mockReducedMotion(true);
    expect(motionPreference()).toBe("static");
    expect(decorativeMotionAllowed()).toBe(false);
    expect(essentialTransitionsAllowed()).toBe(false);
  });

  it("data-saver is the REDUCED tier — essential transitions stay on", () => {
    mockReducedMotion(false);
    document.documentElement.classList.add("reduce-data");
    expect(motionPreference()).toBe("reduced");
    expect(decorativeMotionAllowed()).toBe(false);
    expect(essentialTransitionsAllowed()).toBe(true);
  });
});

import { clampZoom, SCENE_ZOOM_MIN, SCENE_ZOOM_MAX } from "@/lib/scene";

describe("§14.36 the 3D scene's zoom contract", () => {
  it("clamps wheel zoom into the bounded range", () => {
    expect(clampZoom(1, -0.0015 * 5000)).toBe(SCENE_ZOOM_MAX);
    expect(clampZoom(1, 0.0015 * 5000)).toBe(SCENE_ZOOM_MIN);
    expect(clampZoom(1.2, -0.2)).toBe(1.4);   // delta is subtracted
  });

  it("clamps pinch zoom multiplicatively", () => {
    // Pinch out ×4 from 1.0 → clamped to the max.
    expect(clampZoom(1.0, -(4 - 1) * 1.0)).toBe(SCENE_ZOOM_MAX);
    // Pinch in ×0.1 from 1.0 → clamped to the min.
    expect(clampZoom(1.0, -(0.1 - 1) * 1.0)).toBe(SCENE_ZOOM_MIN);
    // A mild ×1.2 pinch from 1.0 lands at 1.2.
    expect(clampZoom(1.0, -0.2 * 1.0)).toBe(1.2);
  });

  it("quantizes to 2 decimals so state stays stable", () => {
    expect(clampZoom(1, 0.0003)).toBe(1);
    expect(String(clampZoom(1, 0.0157)).length <= 4).toBe(true);
  });
});
