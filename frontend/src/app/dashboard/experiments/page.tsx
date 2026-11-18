"use client";

import { useCallback, useEffect, useState } from "react";
import {
  listExperiments, getExperiment, startExperimentShadow,
  startExperimentCanary, promoteExperiment, rollbackExperiment,
  ExperimentRecord,
} from "@/lib/api";

/**
 * Experiments (Phase 13: controlled self-improvement loop).
 *
 * Every candidate change (prompt, routing policy, connector) flows
 * PROPOSED → EVALUATED → SHADOW → CANARY → PROMOTED with separated
 * permissions and predeclared thresholds. Promotion evidence (dataset
 * hash, results, actor, deployment version) is shown; a promotion can
 * always be rolled back to the prior version.
 */

const STATE_TONE: Record<string, string> = {
  PROPOSED: "var(--text-secondary)",
  EVALUATING: "var(--info, #3b82f6)",
  EVALUATED: "var(--info, #3b82f6)",
  SHADOW: "var(--info, #3b82f6)",
  CANARY: "var(--warning, #eab308)",
  PROMOTED: "var(--success, #22c55e)",
  REJECTED: "var(--text-tertiary, #888)",
  ROLLED_BACK: "var(--error, #ef4444)",
  CANCELLED: "var(--text-tertiary, #888)",
};

function metricDelta(exp: ExperimentRecord, key: string): string | null {
  const b = exp.results?.baseline?.[key];
  const c = exp.results?.candidate?.[key];
  if (typeof b !== "number" || typeof c !== "number") return null;
  const delta = c - b;
  const sign = delta >= 0 ? "+" : "";
  return `${b} → ${c} (${sign}${Number(delta.toFixed(4))})`;
}

export default function ExperimentsPage() {
  const [experiments, setExperiments] = useState<ExperimentRecord[] | null>(null);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, string>>({});

  const load = useCallback(() => {
    setBusyId(null);
    listExperiments()
      .then((res) => { setExperiments(res.experiments || []); setError(""); })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "Could not load experiments"));
  }, []);
  useEffect(() => { void load(); }, [load]);

  // §14.K: promotion consumes the durable activation receipt.
  const promoteAct = async (id: string) => {
    setBusyId(id);
    try {
      const out = await promoteExperiment(
        id, `prompt:${experiments?.find((e) => e.experiment_id === id)?.candidate_ref ?? ""}`);
      const r = out.activation_receipt;
      if (!r) {
        setDetail((prev) => ({ ...prev, [id]: "Promotion done (no activation receipt returned)" }));
      } else if (r.state === "effective") {
        setDetail((prev) => ({
          ...prev,
          [id]: `Activation effective${r.active_version_id ? ` · version ${r.active_version_id}` : ""}`
            + (out.reconciled ? " · reconciled from an interrupted attempt" : ""),
        }));
      } else if (r.state === "failed") {
        setDetail((prev) => ({ ...prev, [id]: `Activation FAILED: ${r.error ?? "unknown error"}` }));
      } else {
        setDetail((prev) => ({ ...prev, [id]: "Activation pending — a worker will finish it; refresh to confirm" }));
      }
      load();
    } catch (err: unknown) {
      setDetail((prev) => ({
        ...prev,
        [id]: err instanceof Error ? `Promotion refused: ${err.message}` : "Promotion failed",
      }));
    } finally {
      setBusyId(null);
    }
  };

  const act = async (id: string, action: (eid: string) => Promise<unknown>, label: string) => {
    setBusyId(id);
    try {
      await action(id);
      await getExperiment(id).then((fresh) => {
        setDetail((prev) => ({ ...prev, [id]: `${label}: now ${fresh.state}` }));
      });
      load();
    } catch (err: unknown) {
      setDetail((prev) => ({
        ...prev,
        [id]: err instanceof Error ? `${label} refused: ${err.message}` : `${label} failed`,
      }));
    } finally {
      setBusyId(null);
    }
  };

  return <main style={{ maxWidth: 1000, margin: "0 auto" }}>
    <header style={{ display: "flex", justifyContent: "space-between", gap: 20, marginBottom: 24 }}>
      <div>
        <h1>Experiments</h1>
        <p>Controlled self-improvement: candidates are evaluated on the same
          held-out set, run in shadow, then a bounded canary. Agents can
          propose but never approve or promote.</p>
      </div>
      <button className="btn btn-secondary" onClick={() => void load()}>Refresh</button>
    </header>
    {error && <p role="alert">{error}</p>}
    {experiments && experiments.length === 0 && (
      <p>No experiments yet. Candidates are proposed from reviewed failure clusters.</p>
    )}
    {experiments && experiments.map((exp) => {
      const tone = STATE_TONE[exp.state] ?? "var(--text-secondary)";
      const successDelta = metricDelta(exp, "success");
      return (
        <section key={exp.experiment_id} className="glass-card" style={{ padding: 20, marginBottom: 16 }}
                 data-testid="experiment-card" data-state={exp.state}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
            <strong>{exp.target}</strong>
            <span style={{ color: tone, fontWeight: 600 }}>{exp.state}</span>
          </div>
          <p style={{ color: "var(--text-secondary)", margin: "8px 0 4px" }}>{exp.hypothesis}</p>
          <p style={{ color: "var(--text-tertiary)", fontSize: 13, margin: "0 0 8px" }}>
            Expected: {exp.expected_benefit} · {exp.kind} · {exp.baseline_ref} → {exp.candidate_ref}
          </p>
          <div style={{ color: "var(--text-tertiary)", fontSize: 12 }}>
            Held-out set: <code>{exp.dataset_ref}</code> ({exp.dataset_hash?.slice(0, 12)}) ·
            canary cap {exp.thresholds.max_cohort_size} ({Math.round(exp.thresholds.max_cohort_fraction * 100)}%)
            {exp.thresholds.critical_constraints.length > 0 &&
              <> · constraints: {exp.thresholds.critical_constraints.join(", ")}</>}
          </div>
          {successDelta && (
            <div style={{ marginTop: 6, fontSize: 13, color: "var(--text-secondary)" }}>
              Verified success: {successDelta}
            </div>
          )}
          {exp.state === "PROMOTED" && (
            <div style={{ marginTop: 6, fontSize: 12, color: "var(--text-tertiary)" }}>
              Promoted by {exp.promoted_actor} · deployment {exp.deployment_version} ·
              dataset {exp.promoted_dataset_hash?.slice(0, 12)}
            </div>
          )}
          {exp.state === "ROLLED_BACK" && exp.rollback_reason && (
            <div style={{ marginTop: 6, fontSize: 12, color: "var(--error)" }}>
              Rolled back by {exp.rolled_back_by}: {exp.rollback_reason}
            </div>
          )}
          <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
            {exp.state === "EVALUATED" && (
              <button className="btn btn-secondary btn-sm" disabled={busyId === exp.experiment_id}
                      onClick={() => void act(exp.experiment_id, startExperimentShadow, "Shadow")}>
                Start shadow
              </button>
            )}
            {exp.state === "SHADOW" && (
              <button className="btn btn-secondary btn-sm" disabled={busyId === exp.experiment_id}
                      onClick={() => {
                        // §13.25: the cohort size is the PREDECLARED cap —
                        // never a hardcoded number.
                        const size = exp.thresholds.max_cohort_size;
                        void act(exp.experiment_id,
                          (id) => startExperimentCanary(id, size), "Canary");
                      }}>
                Start canary (≤{exp.thresholds.max_cohort_size})
              </button>
            )}
            {exp.state === "CANARY" && (
              <button className="btn btn-primary btn-sm" disabled={busyId === exp.experiment_id}
                      onClick={() => {
                        // §13.25: no hardcoded population; the deployment
                        // identity names the ACTIVATED prompt version.
                        // §14.K: the ACTIVATION RECEIPT is consumed — the
                        // UI shows what actually happened to the runtime
                        // (effective/failed/reconciled), never just the
                        // experiment state label.
                        void promoteAct(exp.experiment_id);
                      }}>
                Promote
              </button>
            )}
            {exp.state === "PROMOTED" && (
              <button className="btn btn-ghost btn-sm" disabled={busyId === exp.experiment_id}
                      onClick={() => {
                        // §13.25: cancelling the prompt performs NO mutation.
                        const reason = window.prompt("Rollback reason?");
                        if (reason === null || !reason.trim()) return;
                        void act(exp.experiment_id,
                          (id) => rollbackExperiment(id, reason.trim()),
                          "Rollback");
                      }}>
                Roll back
              </button>
            )}
          </div>
          {detail[exp.experiment_id] && (
            <p style={{ marginTop: 8, fontSize: 12, color: "var(--text-tertiary)" }}>
              {detail[exp.experiment_id]}
            </p>
          )}
        </section>
      );
    })}
  </main>;
}
