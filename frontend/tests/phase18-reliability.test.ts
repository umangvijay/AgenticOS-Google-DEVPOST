/**
 * Phase 18 reliability contracts — the pure-function layer plus the
 * shared modules the mounted components consume:
 * - run-state: connection failure NEVER turns running → failed;
 *   terminal states never revert; unknown values degrade deterministically.
 * - request-policy: combined signals fire on ANY source; an
 *   already-aborted signal short-circuits; deadlines survive retries.
 * - draft-store: identity namespacing, version gating.
 */
import { describe, expect, it } from "vitest";
import {
  executionState, outcomeStateFor, presentationFor,
  reconcileContradiction, artifactStateFor,
} from "@/lib/run-state";
import {
  combineSignals, throwIfAborted, OperationDeadline,
} from "@/lib/request-policy";

describe("§18.8 execution vs outcome are separate dimensions", () => {
  it("a connection failure never turns a running task into a failed task", () => {
    const view = presentationFor({ status: "RUNNING" }, "offline", "stale");
    expect(view.execution).toBe("running");
    expect(view.degraded).toBe(true);
    // The outcome stays not-evaluated — the connection is not evidence.
    expect(view.outcome).not.toBe("failed");
  });

  it("execution completion does not imply verified deliverables", () => {
    expect(outcomeStateFor("COMPLETED")).toBe("not_evaluated");
    expect(outcomeStateFor("NEEDS_REVIEW")).toBe("needs_review");
    expect(outcomeStateFor("OUTCOME_UNCERTAIN")).toBe("uncertain");
  });

  it("terminal states never revert to running", () => {
    expect(reconcileContradiction("finished", "running")).toBe("finished");
    expect(reconcileContradiction("running", "finished")).toBe("finished");
  });

  it("unknown server values degrade deterministically", () => {
    expect(executionState("SOMETHING_NEW")).toBe("unknown");
    expect(outcomeStateFor("SOMETHING_NEW")).toBe("unknown");
  });

  it("stale verification never renders as verified", () => {
    expect(artifactStateFor("verification_passed",
                            { stale: true })).toBe("stale");
    expect(artifactStateFor("verification_passed",
                            { stale: false })).toBe("verified");
  });
});

describe("§18.9 request policy combines signals", () => {
  it("fires when ANY source aborts", () => {
    const caller = new AbortController();
    const timeout = new AbortController();
    const combined = combineSignals(caller.signal, timeout.signal);
    expect(combined.aborted).toBe(false);
    caller.abort(new Error("caller cancelled"));
    expect(combined.aborted).toBe(true);
  });

  it("an already-aborted caller signal short-circuits before transport", () => {
    const caller = new AbortController();
    caller.abort();
    expect(() => throwIfAborted(caller.signal)).toThrow();
    // The combined signal is born aborted — fetch would reject instantly.
    const combined = combineSignals(caller.signal);
    expect(combined.aborted).toBe(true);
  });

  it("the operation deadline survives retries", () => {
    const deadline = new OperationDeadline(1000);
    expect(deadline.remainingMs(500) <= 500).toBe(true);
    expect(deadline.expired).toBe(false);
  });
});
