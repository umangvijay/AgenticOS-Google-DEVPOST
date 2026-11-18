"use client";

/**
 * Coordination graph (Phase 15 §15.27): an OPTIONAL dependency-graph
 * view over the delegation tree. The visual language is restrained:
 * nodes are specialists, edges are ACTUAL parent links, status comes
 * from the SERVER lifecycle, and decorative motion is gated by the
 * motion policy (§19.6) — movement never implies execution or learning
 * activity. The 2D specialist table (SpecialistPanel) is always
 * available; this view is additive, never load-bearing.
 */
import { useMemo, useState } from "react";
import { DelegationRow } from "@/lib/api";
import { decorativeMotionAllowed } from "@/lib/motion";
import { runStateStyle } from "@/lib/task-states";

export function CoordinationGraph({ delegations }: {
  delegations: DelegationRow[];
}) {
  const [graphView, setGraphView] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  // Motion is decided ONCE per render from the user's preference —
  // it can never communicate runtime state.
  const motionAllowed = useMemo(() => decorativeMotionAllowed(), []);

  const byParent = useMemo(() => {
    const m = new Map<string, DelegationRow[]>();
    for (const d of delegations) {
      const list = m.get(d.parent_id) ?? [];
      list.push(d);
      m.set(d.parent_id, list);
    }
    return m;
  }, [delegations]);

  const selectedRow = delegations.find(d => d.contract_id === selected) ?? null;

  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
        <button className={`btn btn-sm ${graphView ? "btn-secondary" : "btn-ghost"}`}
                onClick={() => setGraphView(false)} aria-pressed={!graphView}>
          Table view
        </button>
        <button className={`btn btn-sm ${graphView ? "btn-ghost" : "btn-secondary"}`}
                onClick={() => setGraphView(true)} aria-pressed={graphView}
                disabled={!delegations.length}>
          Dependency view
        </button>
      </div>
      {!graphView && (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {delegations.map(d => {
            const st = runStateStyle(d.state);
            return (
              <li key={d.contract_id}
                  style={{ padding: "4px 0", borderBottom: "1px solid var(--border-primary)" }}>
                {/* §19.11: a clickable element exposes BUTTON semantics —
                  visual interactivity alone is not accessible. */}
                <button type="button" className="btn btn-ghost btn-sm"
                        aria-pressed={selected === d.contract_id}
                        onClick={() => setSelected(d.contract_id)}
                        style={{ width: "100%", textAlign: "left", padding: "6px 8px" }}>
                <span className={`badge ${st.badge}`}>{st.label}</span>{" "}
                <strong>{d.title}</strong> — {d.goal || "(no goal recorded)"}
                {motionAllowed && d.state === "RUNNING" && (
                  // The ONLY animated element: a subtle pulse on the
                  // running row, gated by the motion policy.
                  <span aria-hidden="true" className="pulse-dot" />
                )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {graphView && (
        <div role="img"
             aria-label="Specialist dependency graph — status from the server lifecycle"
             style={{ padding: 12, border: "1px solid var(--border-primary)", borderRadius: 8 }}>
          {/* An indented tree is the dependency view: actual parent
              links, no decorative layout — readable without WebGL. */}
          <DependencyTree rows={byParent} parentId="root"
                          topRun={delegations[0]?.parent_id ?? ""}
                          onSelect={setSelected} depth={0}
                          motionAllowed={motionAllowed} />
          {!delegations.length && <p>No specialists to display.</p>}
        </div>
      )}
      {selectedRow && (
        <div style={{ marginTop: 12, padding: 12, border: "1px solid var(--border-primary)", borderRadius: 8, fontSize: 13 }}>
          <strong>{selectedRow.title}</strong> — {selectedRow.responsibility}
          <div style={{ color: "var(--text-secondary)", marginTop: 4 }}>
            Budget: {selectedRow.spent_units}/{selectedRow.budget_units} units
            {" · "}Evidence: {selectedRow.result_keys.join(", ") || "none yet"}
            {selectedRow.error ? ` · Error: ${selectedRow.error}` : ""}
          </div>
        </div>
      )}
    </div>
  );
}

function DependencyTree({ rows, parentId, topRun, onSelect, depth, motionAllowed }: {
  rows: Map<string, DelegationRow[]>;
  parentId: string;
  topRun: string;
  onSelect: (id: string) => void;
  depth: number;
  motionAllowed: boolean;
}) {
  const key = parentId === "root" ? topRun : parentId;
  const kids = rows.get(key) ?? [];
  if (depth > 8) return null;   // bounded display; the tree is bounded too
  return (
    <ul style={{ listStyle: "none", paddingInlineStart: depth ? 20 : 0, margin: 0 }}>
      {kids.map(d => {
        const st = runStateStyle(d.state);
        return (
          <li key={d.contract_id}>
            <button className="btn btn-ghost btn-sm" onClick={() => onSelect(d.contract_id)}
                    style={{ padding: "2px 6px" }}>
              <span className={`badge ${st.badge}`}>{st.label}</span> {d.title}
            </button>
            {motionAllowed && d.state === "RUNNING" && (
              <span aria-hidden="true" className="pulse-dot" />
            )}
            <DependencyTree rows={rows} parentId={d.contract_id}
                            topRun={topRun} onSelect={onSelect}
                            depth={depth + 1} motionAllowed={motionAllowed} />
          </li>
        );
      })}
    </ul>
  );
}
