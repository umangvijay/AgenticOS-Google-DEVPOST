"use client";

/**
 * Specialist panel (Phase 15 §15.25): the multi-agent workspace view —
 * the run's goal, each specialist's responsibility and state, budget,
 * and evidence. Three DISTINCT interventions (§15.25 acceptance):
 * "Stop observing" (pause the local event stream), "Cancel task" (one
 * delegated contract), and "Cancel workflow" — never conflated.
 */
import { useCallback, useEffect, useState } from "react";
import {
  cancelDelegation, cancelWorkflow, getDelegations,
  DelegationRow,
} from "@/lib/api";
import { runStateStyle } from "@/lib/task-states";

export function SpecialistPanel({ runId, onObserveChange, revision = 0 }: {
  runId: string;
  onObserveChange?: (observing: boolean) => void;
  revision?: number;   // §15.26 (finding G): bump on run events → reload
}) {
  const [rows, setRows] = useState<DelegationRow[] | null>(null);
  const [available, setAvailable] = useState(true);
  const [observing, setObserving] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    getDelegations(runId)
      .then((res) => { setRows(res.delegations || []); setAvailable(res.available); })
      .catch(() => setAvailable(false));
  }, [runId]);
  useEffect(() => { void load(); }, [load, revision]);

  const cancelOne = async (contractId: string) => {
    if (!window.confirm("Cancel this delegated task? Its subtasks are cancelled too.")) return;
    setBusyId(contractId);
    try {
      await cancelDelegation(runId, contractId);
      load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not cancel the task");
    } finally {
      setBusyId(null);
    }
  };

  const stopObserving = () => {
    setObserving(false);
    onObserveChange?.(false);
  };
  const resumeObserving = () => {
    setObserving(true);
    onObserveChange?.(true);
  };

  if (!available) return null;   // no delegation store — the panel is absent, not broken

  return (
    <section className="glass-card" style={{ padding: 24, marginTop: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 16 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>Specialists</h2>
        {observing ? (
          <button className="btn btn-ghost btn-sm" onClick={stopObserving}>
            Stop observing
          </button>
        ) : (
          <button className="btn btn-secondary btn-sm" onClick={resumeObserving}>
            Resume observing
          </button>
        )}
      </div>
      <p style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 6 }}>
        Each specialist&apos;s responsibility and state — action summaries, not
        internal reasoning.
      </p>
      {error && <p style={{ color: "var(--error)", fontSize: 13 }}>{error}</p>}
      {!rows?.length && <p style={{ fontSize: 13, color: "var(--text-tertiary)" }}>
        No delegated specialists on this run.</p>}
      {rows && rows.length > 0 && (
        <div style={{ overflowX: "auto", marginTop: 8 }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead><tr>
              {["Specialist", "Goal", "State", "Budget", "Evidence", ""].map(h =>
                <th key={h} style={{ textAlign: "left", padding: "6px 12px", borderBottom: "1px solid var(--border-primary)" }}>{h}</th>)}
            </tr></thead>
            <tbody>
              {rows.map((d) => {
                const st = runStateStyle(d.state);
                return (
                  <tr key={d.contract_id} style={{ borderTop: "1px solid var(--border-primary)" }}>
                    <td style={{ padding: "9px 12px" }}>
                      <div style={{ fontWeight: 600 }}>{d.title}</div>
                      <div style={{ fontSize: 12, color: "var(--text-tertiary)" }}>{d.responsibility}</div>
                    </td>
                    <td style={{ padding: "9px 12px", maxWidth: 260 }}>
                      <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{d.goal || "—"}</div>
                    </td>
                    <td style={{ padding: "9px 12px" }}>
                      <span className={`badge ${st.badge}`}>{st.label}</span>
                      {d.error && <div style={{ fontSize: 12, color: "var(--error)", marginTop: 4 }}>{d.error}</div>}
                    </td>
                    <td style={{ padding: "9px 12px", whiteSpace: "nowrap" }}>
                      {d.spent_units}/{d.budget_units} units
                    </td>
                    <td style={{ padding: "9px 12px", fontSize: 12, fontFamily: "var(--font-mono)" }}>
                      {d.result_keys.length ? d.result_keys.join(", ") : "—"}
                    </td>
                    <td style={{ padding: "9px 12px", textAlign: "right" }}>
                      {!["COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "SUPERSEDED"].includes(d.state) && (
                        <button className="btn btn-ghost btn-sm" disabled={busyId === d.contract_id}
                                onClick={() => void cancelOne(d.contract_id)}>
                          Cancel task
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function CancelWorkflowButton({ runId, onDone }: {
  runId: string; onDone?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const cancelAll = async () => {
    if (!window.confirm("Cancel the ENTIRE workflow? All specialists stop; committed effects are preserved.")) return;
    setBusy(true);
    try {
      await cancelWorkflow(runId);
      onDone?.();
    } finally {
      setBusy(false);
    }
  };
  return (
    <button className="btn btn-danger btn-sm" disabled={busy} onClick={() => void cancelAll()}>
      Cancel workflow
    </button>
  );
}
