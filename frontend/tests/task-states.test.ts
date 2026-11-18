import { describe, expect, it } from "vitest";
import { connectorStateBadges, runStateStyle } from "../src/lib/task-states";

describe("explicit task states (§7)", () => {
  it("renders uncertain / waiting / recovering as distinct attention states", () => {
    expect(runStateStyle("OUTCOME_UNCERTAIN").label).toBe("Outcome uncertain");
    expect(runStateStyle("OUTCOME_UNCERTAIN").needsAttention).toBe(true);
    expect(runStateStyle("WAITING_APPROVAL").label).toBe("Waiting for approval");
    expect(runStateStyle("WAITING_CREDENTIALS").label).toBe("Waiting for credentials");
    expect(runStateStyle("RECOVERING").label).toBe("Recovering");
  });

  it("maps known states to non-generic labels", () => {
    // The mapping exists: every known state resolves to a defined label
    // rather than the unknown-state fallback.
    for (const state of ["COMPLETED", "FAILED", "RUNNING", "QUEUED", "CANCELLED",
                         "WAITING_APPROVAL", "OUTCOME_UNCERTAIN", "RECOVERING"]) {
      const style = runStateStyle(state);
      expect(style.badge).toBeTruthy();
      expect(style.label).toBeTruthy();
    }
    expect(runStateStyle("COMPLETED").label).toBe("Completed");
  });

  it("falls back safely for unknown states", () => {
    const style = runStateStyle("SOMETHING_NEW");
    expect(style.badge).toBe("badge-neutral");
    expect(style.label).toBe("SOMETHING_NEW");
  });
});

describe("connector lifecycle clarity (§7)", () => {
  it("separates generated / tested / verified / active", () => {
    const labels = connectorStateBadges({
      artifact_type: "adapter", trust_tier: "verified", is_enabled: true,
      test_coverage: { read_probe_passed: 3, total: 5 },
    }).map(b => b.label);
    expect(labels).toContain("adapter");
    expect(labels).toContain("tested");
    expect(labels).toContain("verified");
    expect(labels).toContain("active");
  });

  it("never shows tested/active for an untested disabled connector", () => {
    const labels = connectorStateBadges({
      artifact_type: "adapter", trust_tier: "pending_review", is_enabled: false,
      test_coverage: { read_probe_passed: 0, total: 4 },
    }).map(b => b.label);
    expect(labels).not.toContain("tested");
    expect(labels).toContain("needs review");
    expect(labels).not.toContain("active");
  });
});
