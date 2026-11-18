"use client";

import { useCallback, useEffect, useState } from "react";
import { getEvaluationsForRun, type EvaluationRecord } from "@/lib/api";

/**
 * Evaluation panel (Phase 12 §12.31, corrected per review finding F).
 *
 * Shows the SERVER-derived verification state — never a stronger claim
 * than the evidence supports:
 * - per-task verifier verdicts (verified / failed / needs review),
 * - effect uncertainty and failures,
 * - pending evaluation (run finished but the record has not landed yet),
 * - fetch errors kept VISIBLE and retryable (never silently hidden),
 * - refresh without a page reload, so a late-arriving evaluation appears.
 */

const VERDICT_COPY: Record<string, { label: string; tone: string }> = {
  pass: { label: "verified", tone: "var(--success, #22c55e)" },
  fail: { label: "failed verification", tone: "var(--error, #ef4444)" },
  needs_review: { label: "needs review", tone: "var(--warning, #eab308)" },
};

type FetchState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; evaluations: EvaluationRecord[] };

export default function EvaluationPanel({ runId, runStatus }: {
  runId: string;
  runStatus?: string;
}) {
  const [state, setState] = useState<FetchState>({ kind: "loading" });
  const [retryTick, setRetryTick] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  // react-hooks v6 (set-state-in-effect): a runId change resets to
  // loading DURING RENDER (adjust-on-prop-change); retry ticks reset in
  // the event handlers that cause them.
  const [loadedRunId, setLoadedRunId] = useState(runId);
  const [loadedTick, setLoadedTick] = useState(0);
  if (loadedRunId !== runId || loadedTick !== retryTick) {
    setLoadedRunId(runId);
    setLoadedTick(retryTick);
    setState({ kind: "loading" });
  }

  const load = useCallback((mode: "initial" | "refresh") => {
    if (mode === "refresh") setRefreshing(true);
    getEvaluationsForRun(runId)
      .then((res) => setState({ kind: "ready", evaluations: res.evaluations || [] }))
      .catch((err: unknown) => setState({
        kind: "error",
        message: err instanceof Error ? err.message : "Could not load the evaluation",
      }))
      .finally(() => setRefreshing(false));
  }, [runId]);

  useEffect(() => {
    let alive = true;
    // react-hooks v6 (set-state-in-effect): the "loading" reset for a
    // runId/retry change is issued by the EVENT that caused it (retry
    // click) or by the render-time adjust below — never synchronously
    // inside the effect.
    getEvaluationsForRun(runId)
      .then((res) => { if (alive) setState({ kind: "ready", evaluations: res.evaluations || [] }); })
      .catch((err: unknown) => {
        if (alive) setState({
          kind: "error",
          message: err instanceof Error ? err.message : "Could not load the evaluation",
        });
      });
    return () => { alive = false; };
  }, [runId, retryTick]);

  // §12.31: an evaluation arriving after task completion appears without a
  // full page reload — poll while the run is terminal but no record exists.
  useEffect(() => {
    const ready = state.kind === "ready" ? state.evaluations : null;
    if (!ready || ready.length > 0) return;
    if (runStatus && !["COMPLETED", "FAILED", "NEEDS_REVIEW"].includes(runStatus)) return;
    const timer = setTimeout(() => {
      setState({ kind: "loading" });
      setRetryTick((t) => t + 1);
    }, 4000);
    return () => clearTimeout(timer);
  }, [state, runStatus]);

  if (state.kind === "error") {
    // §12.31: fetch errors stay visible and actionable — never hidden.
    return (
      <div role="alert" data-testid="evaluation-panel" data-state="error"
           className="glass-card" style={{ padding: "12px 16px", marginBottom: 12, fontSize: 13 }}>
        <strong style={{ color: "var(--warning, #eab308)" }}>Evaluation unavailable</strong>
        <div style={{ color: "var(--text-secondary)", marginTop: 2 }}>{state.message}</div>
        <button className="btn btn-secondary btn-sm" style={{ marginTop: 8 }}
                onClick={() => { setState({ kind: "loading" }); setRetryTick((t) => t + 1); }}>
          Retry
        </button>
      </div>
    );
  }

  // Explicit narrowing: after the error branch, only loading | ready remain.
  const readyState = state.kind === "ready" ? state : null;
  if (!readyState) {
    return (
      <div role="status" data-testid="evaluation-panel" data-state="loading"
           className="glass-card" style={{ padding: "12px 16px", marginBottom: 12, fontSize: 13 }}>
        Checking verification evidence…
      </div>
    );
  }
  const evaluations = readyState.evaluations;
  if (evaluations.length === 0) {
    // §12.30/§12.31: distinguish "pending evaluation" from "no evaluation
    // exists" — while the worker scores the run this stays explicit.
    return (
      <div role="status" data-testid="evaluation-panel" data-state="pending"
           className="glass-card" style={{ padding: "12px 16px", marginBottom: 12, fontSize: 13 }}>
        <strong style={{ color: "var(--text-secondary)" }}>Evaluation pending</strong>
        <div style={{ color: "var(--text-tertiary)", marginTop: 2 }}>
          The run finished; its verification record is being written.
        </div>
      </div>
    );
  }

  const latest = evaluations[evaluations.length - 1];
  // §12.2 finding F: the label comes from the server-derived verdicts —
  // success requires every task verdict to pass, so the summary cannot
  // contradict the criterion-level results below it.
  const anyNeedsReview = (latest.needs_review_tasks?.length ?? 0) > 0 ||
    latest.task_verifications.some((tv) => tv.verifier_verdict === "needs_review");
  const verdict = latest.success
    ? { label: "Run verified", tone: "var(--success, #22c55e)" }
    : anyNeedsReview
      ? { label: "Needs review — not verified", tone: "var(--warning, #eab308)" }
      : { label: "Not fully verified", tone: "var(--warning, #eab308)" };

  return (
    <div
      role="status"
      data-testid="evaluation-panel"
      data-success={String(latest.success)}
      className="glass-card"
      style={{ padding: "12px 16px", marginBottom: 12, fontSize: 13, lineHeight: 1.45 }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
        <strong style={{ color: verdict.tone }}>{verdict.label}</strong>
        <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <span style={{ color: "var(--text-tertiary)", fontSize: 12 }}>
            scorer v{latest.evaluator_version}
          </span>
          <button className="btn btn-ghost btn-sm" disabled={refreshing}
                  onClick={() => load("refresh")}>
            {refreshing ? "…" : "Refresh"}
          </button>
        </span>
      </div>

      {(latest.total_tasks !== null && latest.total_tasks !== undefined) && (
        <div style={{ color: "var(--text-secondary)", marginTop: 4 }}>
          {latest.completed_tasks}/{latest.total_tasks} tasks completed
          {(latest.failed_tasks ?? 0) > 0 ? ` · ${latest.failed_tasks} failed` : ""}
          {(latest.needs_review_tasks?.length ?? 0) > 0
            ? ` · ${latest.needs_review_tasks.length} awaiting review` : ""}
          {(latest.recovery_attempts ?? 0) > 0
            ? ` · ${latest.recovery_attempts} recovery attempt${latest.recovery_attempts === 1 ? "" : "s"}`
              + ((latest.successful_recoveries ?? 0) > 0 ? ` (${latest.successful_recoveries} recovered)` : "")
            : ""}
        </div>
      )}

      {latest.task_verifications.map((tv) => {
        const copy = VERDICT_COPY[tv.verifier_verdict] ??
          { label: tv.verifier_verdict, tone: "var(--text-tertiary)" };
        const unmet: string[] = (tv as { unmet_criteria?: string[] }).unmet_criteria ?? [];
        return (
          <div key={tv.task_id} style={{ marginTop: 4, color: "var(--text-secondary)" }}>
            <code>{tv.task_id}</code>:{" "}
            <span style={{ color: copy.tone }}>{copy.label}</span>
            {tv.criterion_count > 0 ? ` · ${tv.criterion_count} acceptance criteria checked` : " · no acceptance criteria on this task"}
            {tv.effect_count > 0 ? ` · ${tv.effect_count} external effect${tv.effect_count === 1 ? "" : "s"}` : ""}
            {unmet.length > 0 && (
              <ul data-testid="unmet-criteria" style={{ margin: "2px 0 0", paddingLeft: 18, color: "var(--warning, #eab308)", fontSize: 12 }}>
                {unmet.slice(0, 5).map((reason, i) => <li key={i}>{reason}</li>)}
                {unmet.length > 5 && <li>… {unmet.length - 5} more</li>}
              </ul>
            )}
          </div>
        );
      })}

      {latest.uncertain_effects.length > 0 && (
        <div style={{ color: "var(--warning, #eab308)", marginTop: 4 }}>
          {latest.uncertain_effects.length} external action{latest.uncertain_effects.length === 1 ? "" : "s"} with unknown outcome — the run cannot be counted as verified until they resolve.
        </div>
      )}
      {latest.failed_effects.length > 0 && (
        <div style={{ color: "var(--error, #ef4444)", marginTop: 4 }}>
          {latest.failed_effects.length} external action{latest.failed_effects.length === 1 ? "" : "s"} confirmed NOT applied.
        </div>
      )}
      {latest.unverified_effects?.length > 0 && (
        <div style={{ color: "var(--text-tertiary)", marginTop: 4 }}>
          {latest.unverified_effects.length} external action{latest.unverified_effects.length === 1 ? "" : "s"} without verifiable outcome evidence.
        </div>
      )}

      <div style={{ color: "var(--text-tertiary)", fontSize: 12, marginTop: 6 }}>
        {latest.latency_ms ? `${(latest.latency_ms / 1000).toFixed(1)}s observed · ` : ""}
        {latest.token_usage_total ? `${latest.token_usage_total.toLocaleString()} tokens · ` : "token usage unavailable · "}
        fixture {latest.fixture_hash?.slice(0, 12) ?? "n/a"}
      </div>
    </div>
  );
}
