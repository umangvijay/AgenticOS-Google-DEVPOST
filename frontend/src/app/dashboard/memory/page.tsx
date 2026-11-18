"use client";

/**
 * Memory workspace (Phase 16 §16.27): what AgentOS remembers — with
 * source, scope, freshness, and status DIRECTLY visible, and the user
 * controls (correct, forget, export) one click away. §16.29: the
 * optional relationship view uses only REAL edges (supersedes /
 * derived-from via subject keys) — decorative connections never imply
 * evidence that does not exist; the searchable list stays primary.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  correctMemory, exportMemories, forgetMemory, listMemories,
  MemoryRecord,
} from "@/lib/api";
import { memoryUiState } from "@/lib/task-states";
import { decorativeMotionAllowed } from "@/lib/motion";

export default function MemoryPage() {
  const [memories, setMemories] = useState<MemoryRecord[] | null>(null);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [graphView, setGraphView] = useState(false);

  const load = useCallback(() => {
    listMemories()
      .then((res) => { setMemories(res.memories || []); setError(""); })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : "Could not load memories"));
  }, []);
  useEffect(() => { void load(); }, [load]);

  const forget = async (id: string) => {
    if (!window.confirm("Forget this memory? It disappears from retrieval immediately; cleanup follows.")) return;
    setBusyId(id);
    try {
      await forgetMemory(id);
      load();
    } finally {
      setBusyId(null);
    }
  };

  const correct = async (id: string) => {
    if (!draft.trim()) return;
    setBusyId(id);
    try {
      await correctMemory(id, draft.trim());
      setEditing(null);
      setDraft("");
      load();
    } finally {
      setBusyId(null);
    }
  };

  const doExport = async () => {
    const data = await exportMemories();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "agentos-memory-export.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  // §16.29: REAL edges only — supersession chains and subject groups
  // from the durable records.
  const relationships = useMemo(() => {
    const bySubject = new Map<string, MemoryRecord[]>();
    for (const m of memories ?? []) {
      if (!m.subject_key) continue;
      const list = bySubject.get(m.subject_key) ?? [];
      list.push(m);
      bySubject.set(m.subject_key, list);
    }
    return bySubject;
  }, [memories]);
  const motionAllowed = useMemo(() => decorativeMotionAllowed(), []);

  return (
    <div style={{ padding: 24, maxWidth: 1100, margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, marginBottom: 4 }}>Memory</h1>
        <button className="btn btn-secondary btn-sm" onClick={() => void doExport()}>
          Export my memories
        </button>
      </div>
      <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 20 }}>
        Source, scope, and status shown directly — you can tell whether a
        record is confirmed, inferred, or disputed, and act on it (§16.27).
      </p>
      {error && <p style={{ color: "var(--error)" }}>{error}</p>}
      {!memories && !error && <div className="spinner" />}
      {memories && memories.length === 0 && (
        <div className="empty-state">Nothing stored yet — verified outcomes and your explicit instructions appear here.</div>
      )}
      {memories && memories.length > 0 && (
        <>
          <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
            <button className={`btn btn-sm ${graphView ? "btn-secondary" : "btn-ghost"}`}
                    onClick={() => setGraphView(false)} aria-pressed={!graphView}>
              List
            </button>
            <button className={`btn btn-sm ${graphView ? "btn-ghost" : "btn-secondary"}`}
                    onClick={() => setGraphView(true)} aria-pressed={graphView}>
              Relationships
            </button>
          </div>
          {!graphView && (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead><tr>
                  {["Memory", "Class", "Status", "Scope", "Source", ""].map(h =>
                    <th key={h} style={{ textAlign: "left", padding: "6px 12px", borderBottom: "1px solid var(--border-primary)" }}>{h}</th>)}
                </tr></thead>
                <tbody>
                  {memories.map((m) => {
                    const st = memoryUiState(m);
                    const isHistorical = Boolean(m.superseded_by);
                    return (
                      <tr key={m.id} style={{ borderTop: "1px solid var(--border-primary)", opacity: isHistorical ? 0.55 : 1 }}>
                        <td style={{ padding: "9px 12px", maxWidth: 360 }}>
                          {editing === m.id ? (
                            <div style={{ display: "flex", gap: 8 }}>
                              <input className="input" value={draft}
                                     onChange={(e) => setDraft(e.target.value)}
                                     aria-label="Corrected memory content" />
                              <button className="btn btn-primary btn-sm" disabled={busyId === m.id}
                                      onClick={() => void correct(m.id)}>Save</button>
                              <button className="btn btn-ghost btn-sm" onClick={() => setEditing(null)}>Cancel</button>
                            </div>
                          ) : m.content}
                        </td>
                        <td style={{ padding: "9px 12px" }}>{m.memory_class}</td>
                        <td style={{ padding: "9px 12px" }}>
                          <span className={`badge ${st.badge}`}>{st.label}</span>
                          <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginTop: 2 }}>
                            {m.verification_status}
                          </div>
                        </td>
                        <td style={{ padding: "9px 12px" }}>{m.scope}</td>
                        <td style={{ padding: "9px 12px", fontSize: 12, color: "var(--text-tertiary)", maxWidth: 160 }}>
                          <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                            {m.source || "—"}
                          </div>
                        </td>
                        <td style={{ padding: "9px 12px", textAlign: "right", whiteSpace: "nowrap" }}>
                          {!isHistorical && (
                            <>
                              <button className="btn btn-ghost btn-sm"
                                      onClick={() => { setEditing(m.id); setDraft(m.content); }}>
                                Correct
                              </button>{" "}
                              <button className="btn btn-ghost btn-sm" disabled={busyId === m.id}
                                      onClick={() => void forget(m.id)}>
                                Forget
                              </button>
                            </>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          {graphView && (
            <div role="img" aria-label="Memory relationship view — supersession chains and subject groups"
                 style={{ padding: 16, border: "1px solid var(--border-primary)", borderRadius: 8 }}>
              {relationships.size === 0 && (
                <p style={{ fontSize: 13, color: "var(--text-tertiary)" }}>
                  No related memory groups yet — records that supersede or extend one another appear here.
                </p>
              )}
              {[...relationships.entries()].map(([subject, group]) => (
                <div key={subject} style={{ marginBottom: 16 }}>
                  <div style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-tertiary)", marginBottom: 6 }}>
                    {subject}
                  </div>
                  <ol style={{ margin: 0, paddingInlineStart: 20 }}>
                    {group.map((m, i) => (
                      <li key={m.id} style={{ fontSize: 13, padding: "2px 0" }}>
                        {m.content}{" "}
                        {i < group.length - 1 && (
                          <span style={{ color: "var(--text-tertiary)", fontSize: 11 }}>
                            ← superseded by the next version
                          </span>
                        )}
                        {motionAllowed && m.superseded_by && (
                          <span aria-hidden="true" className="pulse-dot" />
                        )}
                      </li>
                    ))}
                  </ol>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
