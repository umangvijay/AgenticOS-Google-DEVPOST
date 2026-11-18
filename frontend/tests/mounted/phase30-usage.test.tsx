/**
 * Phase 30 §30.32/§30.37 — MOUNTED tests for the usage/authority panels.
 * The budget panel surfaces the provider rate limit as an explicit state
 * (reported vs not reported), and the evaluation panel names WHICH
 * criterion remains unmet rather than only a count.
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

import BudgetUsagePanel from "@/components/BudgetUsagePanel";
import EvaluationPanel from "@/components/EvaluationPanel";

const { getBudgetSnapshot, getEvaluationsForRun } = vi.hoisted(() => ({
  getBudgetSnapshot: vi.fn(),
  getEvaluationsForRun: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getBudgetSnapshot: (...args: unknown[]) => getBudgetSnapshot(...args),
  getEvaluationsForRun: (...args: unknown[]) => getEvaluationsForRun(...args),
}));

describe("§30.32 BudgetUsagePanel provider rate limit", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders an explicit not-reported state when the provider omits it", async () => {
    getBudgetSnapshot.mockResolvedValue({
      scope: "user", subject_id: "u1", unit: "tokens", period: "2026-11-14",
      limit: 10000, consumed: 3200, reserved: 0, uncertain_reserved: 0,
      available: 6800, accounting_status: "healthy",
      policy_version: "budget-policy-2",
      provider_rate_limit: null,
    });
    render(<BudgetUsagePanel />);
    await waitFor(() => {
      expect(screen.getByTestId("provider-rate-limit").textContent)
        .toContain("not reported by the provider");
    });
  });

  it("surfaces the provider-supplied limit, remaining and reset", async () => {
    getBudgetSnapshot.mockResolvedValue({
      scope: "user", subject_id: "u1", unit: "tokens", period: "2026-11-14",
      limit: 10000, consumed: 3200, reserved: 0, uncertain_reserved: 0,
      available: 6800, accounting_status: "healthy",
      provider_rate_limit: {
        provider: "gemini", source: "provider_header", scope: "project",
        limit: 1000, remaining: 742, reset_at: "2026-11-14T18:00:00Z",
        window: "1m",
      },
    });
    render(<BudgetUsagePanel />);
    await waitFor(() => {
      const text = screen.getByTestId("provider-rate-limit").textContent ?? "";
      expect(text).toContain("742/1000");
      expect(text).toContain("from header");
      expect(text).toContain("resets");
    });
  });
});

describe("§30.37 EvaluationPanel unmet criteria", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("lists the unmet criteria for a failed task", async () => {
    getEvaluationsForRun.mockResolvedValue({
      evaluations: [{
        success: false,
        evaluator_version: 3,
        total_tasks: 1, completed_tasks: 1, failed_tasks: 1,
        needs_review_tasks: [],
        recovery_attempts: 0, successful_recoveries: 0,
        task_verifications: [{
          task_id: "task-1", task_status: "FAILED",
          verifier_verdict: "fail", criterion_count: 2,
          unmet_criteria: ["output does not mention 'case 4711'"],
          effect_count: 0, effect_interpretations: {}, blocking_effects: [],
        }],
        uncertain_effects: [], failed_effects: [], unverified_effects: [],
        latency_ms: 1200, token_usage_total: 512, fixture_hash: "abc",
      }],
    });
    render(<EvaluationPanel runId="run-1" runStatus="FAILED" />);
    await waitFor(() => {
      const list = screen.getByTestId("unmet-criteria");
      expect(list.textContent).toContain("case 4711");
    });
  });
});
