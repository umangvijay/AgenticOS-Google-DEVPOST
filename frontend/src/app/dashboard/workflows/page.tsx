"use client";

import { useState, useEffect } from "react";
import { runStateStyle } from "@/lib/task-states";
import { listWorkflows, WorkflowRun } from "@/lib/api";
import RunElapsed from "@/components/RunElapsed";
import { isTerminalRun } from "@/lib/task-states";
import Link from "next/link";

export default function WorkflowsPage() {
  const [workflows, setWorkflows] = useState<WorkflowRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [filter, setFilter] = useState("all");
  const visible = workflows.filter(w => filter === "all" || (filter === "active" ? !isTerminalRun(w.status) : w.status === filter));
  useEffect(() => {
    let active = true;
    let refresh: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const { workflows } = await listWorkflows(50);
        if (!active) return;
        setWorkflows(workflows);
        setError("");
        if (workflows.some(w => !isTerminalRun(w.status))) refresh = setTimeout(load, 5000);
      } catch (err: unknown) {
        if (active) setError(err instanceof Error ? err.message : "Failed to load workflows");
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    return () => { active = false; clearTimeout(refresh); };
  }, []);

  return (
    <div className="animate-fade-in" data-scroll-reveal>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 32 }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 700 }}>Builds</h1>
          <p style={{ color: "var(--text-secondary)" }}>Inspect each run, its steps, and any failures.</p>
        </div>
        <Link href="/dashboard" className="btn btn-primary">New request</Link>
      </div>

      {error && (
        <div style={{
          padding: "12px 16px", marginBottom: 20,
          background: "var(--error-subtle)", borderRadius: "var(--radius-md)",
          color: "var(--error)", fontSize: 14,
        }}>
          {error}
        </div>
      )}

      <label>Show runs <select value={filter} onChange={e => setFilter(e.target.value)}>
        <option value="all">All runs</option><option value="active">Active</option>
        <option value="FAILED">Failed</option><option value="COMPLETED">Completed</option>
      </select></label>
      {loading ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="skeleton" style={{ height: 80 }} />
          <div className="skeleton" style={{ height: 80 }} />
          <div className="skeleton" style={{ height: 80 }} />
        </div>
      ) : workflows.length > 0 ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {visible.length === 0 && <p>No runs match this filter.</p>}
          {visible.map(wf => (
            <Link
              key={wf.run_id}
              href={`/dashboard/workflows/${wf.run_id}`}
              className="glass-card"
              style={{ padding: 20, textDecoration: "none", display: "block" }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
                <div>
                  <h3 style={{
                    fontSize: 16, fontWeight: 600, color: "var(--text-primary)", marginBottom: 4,
                    display: "-webkit-box", WebkitLineClamp: 2,
                    WebkitBoxOrient: "vertical" as const, overflow: "hidden",
                  }}>
                    {wf.goal}
                  </h3>
                  <div style={{ fontSize: 13, color: "var(--text-tertiary)", display: "flex", gap: 16 }}>
                    <span>ID: {wf.run_id.substring(0, 8)}</span>
                    <span>Started: {new Date(wf.created_at).toLocaleString()}<RunElapsed run={wf} /></span>
                  </div>
                </div>
                <span className={`badge ${runStateStyle(wf.status).badge}`}>
                  {runStateStyle(wf.status).label}
                </span>
              </div>
              
              {wf.tasks?.find(t => t.error) && <p style={{ color: "var(--error)" }}>{wf.tasks.find(t => t.error)?.error}</p>}
              <small>{wf.tasks?.filter(t => t.status === "COMPLETED").length ?? 0} of {wf.tasks?.length ?? 0} steps complete</small>
              <div style={{ display: "flex", gap: 4, marginTop: 16 }}>
                {wf.tasks.map(t => (
                  <div key={t.task_id} style={{
                    flex: 1, height: 4, borderRadius: 2,
                    background: runStateStyle(t.status).color
                  }} title={`${t.agent}: ${t.status}`} />
                ))}
              </div>
            </Link>
          ))}
        </div>
      ) : (
        <div className="glass-card empty-state">
          <svg width="48" height="48" fill="none" stroke="currentColor" strokeWidth="1" viewBox="0 0 24 24">
            <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
          </svg>
          <h3 style={{ fontSize: 16, fontWeight: 500, color: "var(--text-primary)", marginBottom: 8 }}>No workflows yet</h3>
          <p style={{ marginBottom: 24 }}>Start by giving Sandtable a goal.</p>
          <Link href="/dashboard" className="btn btn-primary">Create Workflow</Link>
        </div>
      )}
    </div>
  );
}
