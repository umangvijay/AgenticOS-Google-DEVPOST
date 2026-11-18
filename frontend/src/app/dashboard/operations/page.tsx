"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { getOperationsSummary, OperationsSummary, getModelLeaderboard, ModelLeaderboardRow, getRoutingDrift, RoutingDriftEntry, getRoutingHealth, RoutingHealth } from "@/lib/api";
import BudgetUsagePanel from "@/components/BudgetUsagePanel";

export default function OperationsPage() {
  const [data, setData] = useState<OperationsSummary | null>(null);
  const [leaderboard, setLeaderboard] = useState<ModelLeaderboardRow[] | null>(null);
  const [analytics, setAnalytics] = useState<{ baselines: { agent: string; expected_s: number; samples: number; std_s: number }[]; anomalies: { agent: string; duration_s: number; expected_s: number; z: number; run_id?: string; title?: string }[] } | null>(null);
  const [drift, setDrift] = useState<RoutingDriftEntry[] | null>(null);
  const [routingHealth, setRoutingHealth] = useState<RoutingHealth | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(true);
  const load = useCallback(() => Promise.all([getOperationsSummary(), getModelLeaderboard(), getRoutingDrift().catch(() => null), getRoutingHealth().catch(() => null)]).then(([result, board, driftData, healthData]) => {
    setRoutingHealth(healthData);
    setData(result); setLeaderboard(board.models || []);
    setAnalytics((result as { analytics?: { baselines: never[]; anomalies: never[] } }).analytics ?? { baselines: [], anomalies: [] });
    setDrift(driftData?.contexts ?? null);
    setError("");
  }).catch((err: unknown) => {
    setError(err instanceof Error ? err.message : "Could not load run metrics");
  }).finally(() => setBusy(false)), []);
  useEffect(() => { void load(); }, [load]);
  return <main style={{ maxWidth: 1000, margin: "0 auto" }}>
    <header style={{ display: "flex", justifyContent: "space-between", gap: 20, marginBottom: 24 }}>
      <div><h1>Operations</h1><p>Observed results from your recent workflows.</p></div>
      <button className="btn btn-secondary" disabled={busy} onClick={() => { setBusy(true); void load(); }}>{busy ? "Loading…" : "Refresh"}</button>
    </header>
    {error && <p role="alert">{error}</p>}
    <BudgetUsagePanel />
    {data && <>
      <p style={{ color: "var(--text-secondary)", marginBottom: 20 }}>Latest {data.sample_size} runs, up to {data.sample_limit}. Updated {new Date(data.generated_at).toLocaleString()}. Refresh to update.</p>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 16 }}>
        {[["Completion rate", data.completion_rate === null ? "No finished runs" : `${data.completion_rate}%`],
          ["Median completion", data.median_completion_seconds === null ? "No timing data" : `${data.median_completion_seconds}s`],
          ["Est. spend (sample)", `$${(data.cost_tracking?.estimated_cost_usd ?? 0).toFixed(4)} · ${data.cost_tracking?.estimated_tokens?.toLocaleString() ?? 0} tok`],
          ["Needs attention", String(data.attention.length)]].map(([label, value]) =>
          <div className="glass-card" style={{ padding: 24 }} key={label}><p>{label}</p><strong style={{ fontSize: 24 }}>{value}</strong></div>)}
      </div>
      <p style={{ margin: "16px 0", color: "var(--text-secondary)" }}>Completion rate includes completed and failed runs. Long-running means more than 15 minutes since creation; it does not necessarily mean stuck. Spending is not measured here.</p>
      <section className="glass-card" style={{ padding: 24, marginBottom: 20 }}>
        <h2>Needs attention</h2>
        {!data.attention.length && <p>No flagged runs in this sample.</p>}
        {data.attention.map(run => <div key={run.run_id} style={{ display: "flex", justifyContent: "space-between", padding: "12px 0", gap: 16 }}>
          <span>{run.reason === "approval_required" ? "Awaiting your approval" : run.reason === "failed" ? "Run failed" : "Long-running workflow"}</span>
          <Link href={`/dashboard/workflows/${encodeURIComponent(run.run_id)}`} className="btn btn-secondary">Inspect run</Link>
        </div>)}
      </section>
      <section className="glass-card" style={{ padding: 24 }}><h2>Failed tasks by agent</h2>
        {!Object.keys(data.failed_tasks_by_agent).length && <p>No failed tasks in this sample.</p>}
        {Object.entries(data.failed_tasks_by_agent).map(([agent, count]) => <p key={agent}>{agent}: {count}</p>)}
      </section>
    </>}
  {routingHealth && routingHealth.total_decisions > 0 && (
      <section style={{ marginTop: 28 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>Routing log health</h2>
        <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 12 }}>
          Coverage of verified feedback over the logged model selections
          (Phase 14 §14.4). Missing feedback is explicit — those decisions
          are visible here, never silently dropped from learning.
        </p>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap", fontSize: 13 }}>
          <div><span style={{ color: "var(--text-tertiary)" }}>Decisions</span> <strong>{routingHealth.total_decisions}</strong></div>
          <div><span style={{ color: "var(--text-tertiary)" }}>Verified feedback</span> <strong>{routingHealth.with_feedback}</strong></div>
          <div><span style={{ color: routingHealth.missing_feedback > 0 ? "var(--warning)" : "var(--text-tertiary)" }}>Missing feedback</span> <strong>{routingHealth.missing_feedback}</strong></div>
          <div><span style={{ color: "var(--text-tertiary)" }}>Distinct actions</span> <strong>{routingHealth.distinct_actions}</strong></div>
        </div>
      </section>
  )}
  {drift && drift.length > 0 && (
      <section style={{ marginTop: 28 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>Routing drift</h2>
        <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 12 }}>
          Recent vs long-run mean reward per context (Phase 14 §14.5). A shift is a signal for
          operators — the policy does not auto-tune on it. Security constraints stay outside the
          scalar reward.
        </p>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead><tr>
              {["Context", "Recent mean reward", "Long-run mean", "Recent attempts", "Drift"].map(h =>
                <th key={h} style={{ textAlign: "left", padding: "6px 12px", borderBottom: "1px solid var(--border-primary)" }}>{h}</th>)}
            </tr></thead>
            <tbody>
              {drift.map(d => <tr key={d.context_key}>
                <td style={{ padding: "6px 12px", fontFamily: "monospace", fontSize: 12 }}>{d.context_key}</td>
                <td style={{ padding: "6px 12px" }}>{d.recent_mean_reward}</td>
                <td style={{ padding: "6px 12px" }}>{d.long_run_mean_reward}</td>
                <td style={{ padding: "6px 12px" }}>{d.recent_attempts}</td>
                <td style={{ padding: "6px 12px", color: d.drift_detected ? "var(--warning)" : "var(--text-tertiary)" }}>
                  {d.drift_detected ? "DRIFT DETECTED" : "stable"}
                </td>
              </tr>)}
            </tbody>
          </table>
        </div>
      </section>
    )}
  {leaderboard && leaderboard.length > 0 && (
      <section style={{ marginTop: 28 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>Model leaderboard</h2>
        <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 12 }}>
          Learned by the adaptive routing policy from real task outcomes (UCB1 + 10% exploration).
        </p>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-tertiary)", fontSize: 12 }}>
                <th style={{ padding: "8px 12px 8px 0" }}>Model</th>
                <th style={{ padding: "8px 12px" }}>Attempts</th>
                <th style={{ padding: "8px 12px" }}>Success rate</th>
                <th style={{ padding: "8px 12px" }}>Avg latency</th>
                <th style={{ padding: "8px 12px" }}>Policy score</th>
              </tr>
            </thead>
            <tbody>
              {leaderboard.map(row => (
                <tr key={row.model} style={{ borderTop: "1px solid var(--border-primary)" }}>
                  <td style={{ padding: "9px 12px 9px 0", fontFamily: "var(--font-mono)", fontSize: 12.5 }}>{row.model}</td>
                  <td style={{ padding: 12 }}>{row.attempts}</td>
                  <td style={{ padding: 12 }}>{row.success_rate === null ? "—" : `${Math.round(row.success_rate * 100)}%`}</td>
                  <td style={{ padding: 12 }}>{Math.round(row.ewma_latency_ms)} ms</td>
                  <td style={{ padding: 12 }}>{row.score.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    )}
  {analytics && analytics.baselines.length > 0 && (
      <section style={{ marginTop: 28 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>Expected durations (learned)</h2>
        <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 12 }}>
          Regression baselines per agent from your real run history (EWMA mean ± std).
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 12 }}>
          {analytics.baselines.map(b => (
            <div key={b.agent} style={{ padding: 14, borderRadius: 12, border: "1px solid var(--border-primary)", background: "var(--bg-secondary)" }}>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-tertiary)" }}>{b.agent}</div>
              <div style={{ fontSize: 20, fontWeight: 700, marginTop: 4 }}>{b.expected_s}s</div>
              <div style={{ fontSize: 12, color: "var(--text-tertiary)" }}>± {b.std_s}s · {b.samples} samples</div>
            </div>
          ))}
        </div>
      </section>
    )}
    {analytics && analytics.anomalies.length > 0 && (
      <section style={{ marginTop: 28 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>Slow-run anomalies</h2>
        <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 12 }}>
          Statistically slow for their agent (z-score &gt; 2.5) — worth a look.
        </p>
        {analytics.anomalies.slice(0, 8).map((a, i) => (
          <div key={i} style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "10px 0", borderBottom: "1px solid var(--border-primary)", flexWrap: "wrap" }}>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{a.agent}</span>
            <span style={{ fontSize: 12.5, color: "var(--text-secondary)" }}>
              {a.duration_s}s vs expected {a.expected_s}s · z {a.z}
            </span>
          </div>
        ))}
      </section>
    )}
  </main>
}
