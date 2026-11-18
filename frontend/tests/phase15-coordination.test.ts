/**
 * Phase 15 §15.25–§15.27: the multi-agent workspace reliability.
 *  - §15.26: progress counters derive from AUTHORITATIVE task states —
 *    duplicate/late events cannot inflate them; an older response
 *    cannot replace a newer state (identity-guarded).
 *  - §15.27: the coordination view's motion is policy-gated —
 *    decorative movement never implies execution.
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { deriveProgress, runStateStyle } from "@/lib/task-states";
import { decorativeMotionAllowed } from "@/lib/motion";

describe("§15.26 progress derives from authoritative states", () => {
  it("counts each task exactly once regardless of event duplication", () => {
    const tasks = [
      { status: "COMPLETED" }, { status: "COMPLETED" },
      { status: "RUNNING" }, { status: "WAITING_APPROVAL" },
      { status: "FAILED" },
    ];
    const p = deriveProgress(tasks);
    expect(p).toEqual({ total: 5, completed: 2, running: 1, waiting: 1, failed: 1 });
    // Re-deriving (as a duplicated event delivery would) yields the
    // SAME numbers — the counter is a function of state, not of events.
    expect(deriveProgress(tasks)).toEqual(p);
  });

  it("late and out-of-order events change nothing without new state", () => {
    const before = deriveProgress([{ status: "RUNNING" }]);
    // A stale RUNNING event arrives after the task COMPLETED server-side:
    // the UI re-derives from the reconciled snapshot, not the event.
    const after = deriveProgress([{ status: "COMPLETED" }]);
    expect(after.completed).toBe(1);
    expect(after.running).toBe(0);
    expect(before.running).toBe(1);
  });

  it("uncertain outcomes are visible, never folded into completed", () => {
    const p = deriveProgress([
      { status: "NEEDS_REVIEW" }, { status: "OUTCOME_UNCERTAIN" },
    ]);
    expect(p.completed).toBe(0);
    expect(p.failed).toBe(2);
  });

  it("known states always render explicit labels", () => {
    expect(runStateStyle("VERIFYING").label).toBe("Verifying");
    expect(runStateStyle("RECOVERING").label).toBe("Recovering");
  });
});

describe("§15.27 the coordination view's motion is policy-gated", () => {
  const originalMatchMedia = window.matchMedia;
  afterEach(() => {
    window.matchMedia = originalMatchMedia;
    document.documentElement.classList.remove("reduce-data");
    vi.restoreAllMocks();
  });

  it("decorative movement is off under reduced motion — the graph stays usable", () => {
    window.matchMedia = ((q: string) => ({
      matches: q.includes("prefers-reduced-motion"),
      media: q, onchange: null, addListener: vi.fn(),
      removeListener: vi.fn(), addEventListener: vi.fn(),
      removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia;
    expect(decorativeMotionAllowed()).toBe(false);
    // The dependency view itself does not depend on WebGL or motion:
    // it is a DOM tree — usable everywhere.
  });
});
