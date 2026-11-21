"use client";

import { useId, useState, type ReactNode } from "react";
import RunElapsed from "@/components/RunElapsed";
import Link from "next/link";
import { Activity, ChevronRight, ChevronsUpDown, FileText, Folder, Link2, PanelRightClose, Plus } from "lucide-react";
import type { WorkflowEvent, WorkflowRun } from "@/lib/api";
import ExecutionTimeline, { executionLabel } from "@/components/ExecutionTimeline";

function linkedSources(run?: WorkflowRun) {
  const urls = new Set<string>();
  const add = (value: unknown) => {
    if (typeof value !== "string") return;
    try {
      const url = new URL(value);
      if (["https:", "http:"].includes(url.protocol) && !url.username && !url.password) urls.add(url.href);
    } catch { /* A source must be a valid, explicitly supplied web URL. */ }
  };
  for (const task of run?.tasks || []) {
    add(task.input_data?.url);
    add(task.output_data?.url);
    const sources = task.output_data?.sources;
    if (Array.isArray(sources)) for (const source of sources) add(typeof source === "string" ? source : source?.url);
  }
  return [...urls];
}

export default function WorkspaceExecutionCard({ turns, events, onClose, onInspectRun, children }: {
  turns: WorkflowRun[]; events: WorkflowEvent[]; onClose: () => void; onInspectRun: (runId: string) => void; children?: (run: WorkflowRun) => ReactNode;
}) {
  const bodyId = useId();
  const [collapsed, setCollapsed] = useState(false);
  const [selectedId, setSelectedId] = useState("");
  const run = turns.find(t => t.run_id === selectedId) || turns.at(-1);
  const runEvents = events.filter(event => event.run_id === run?.run_id);
  const tasks = run?.tasks || [];
  const done = tasks.filter(task => task.status === "COMPLETED").length;
  const outputs = tasks.filter(task => task.status === "COMPLETED" && task.output_data && Object.keys(task.output_data).length > 0);
  const sources = linkedSources(run);

  return <section className="execution-card" aria-label="Run summary">
    <div className="execution-card-header">
      <button className="execution-card-toggle" aria-label={collapsed ? "Expand execution card" : "Collapse execution card"}
        aria-expanded={!collapsed} aria-controls={bodyId} onClick={() => setCollapsed(value => !value)}>
        <Folder size={17} /><span>Sandtable</span><ChevronRight size={15} className={collapsed ? "" : "is-expanded"} />
      </button>
      <button className="execution-icon-button" aria-label="Close execution" title="Hide execution card" onClick={onClose}><PanelRightClose size={17} /></button>
    </div>
    <div id={bodyId} hidden={collapsed} className="execution-card-body">
      {turns.length > 1 && <label className="execution-run-picker">Message
        <select aria-label="Execution for message" value={selectedId} onChange={event => { setSelectedId(event.target.value); onInspectRun(event.target.value || turns.at(-1)?.run_id || ""); }}>
          <option value="">Latest message</option>
          {turns.map((turn, index) => <option key={turn.run_id} value={turn.run_id}>{index + 1}. {turn.goal.slice(0, 70)}</option>)}
        </select>
      </label>}
      <div className="execution-overview"><Activity size={17} /><span role="status">{executionLabel(run?.status)}</span>
        <span className="execution-progress">{tasks.length ? `${done}/${tasks.length} steps` : ["COMPLETED", "FAILED", "CANCELLED", "EXPIRED", "NEEDS_REVIEW"].includes(run?.status || "") ? "No steps recorded" : "Waiting for steps"}</span>
      </div>
      {run && <RunElapsed run={run} events={runEvents} />}
      <details className="execution-section" key={`activity-${run?.run_id}`}>
        <summary><span>Activity</span><span className="execution-count">{tasks.length || runEvents.length}</span><ChevronRight size={15} className="disclosure-chevron" /></summary>
        <ExecutionTimeline events={runEvents} tasks={tasks} compact />
      </details>
      <details className="execution-section" open key={`outputs-${run?.run_id}`}>
        <summary><span>Outputs</span><span className="execution-count">{outputs.length}</span><ChevronRight size={15} className="disclosure-chevron" /></summary>
        {outputs.length ? <ul className="execution-resource-list">{outputs.map(task => <li key={task.task_id}>
          {typeof task.output_data?.mcp_id === "string" && <Link href={`/dashboard/integrations/${encodeURIComponent(task.output_data.mcp_id)}`}>Open stamp</Link>}
          <a href={`#result-${run!.run_id}-${task.task_id}`} onClick={() => { if (window.matchMedia("(max-width: 1099px)").matches) onClose(); }} title="View result in conversation"><FileText size={16} /><span>{task.title || (task.agent === "core.chat" ? "Assistant response" : task.tool || "Step result")}</span><ChevronRight size={14} /></a>
        </li>)}</ul> : <p className="execution-empty">No saved outputs yet.</p>}
        <Link href="/dashboard/studio" className="execution-text-link"><Plus size={15} />Create an output</Link>
      </details>
      <details className="execution-section" open key={`sources-${run?.run_id}`}>
        <summary><span>Sources</span><span className="execution-count">{sources.length}</span><ChevronRight size={15} className="disclosure-chevron" /></summary>
        {sources.length ? <ul className="execution-resource-list">{sources.map(url => <li key={url}>
          <a href={url} target="_blank" rel="noreferrer"><Link2 size={16} /><span>{new URL(url).hostname}{new URL(url).pathname === "/" ? "" : new URL(url).pathname}</span></a>
        </li>)}</ul> : <p className="execution-empty">No linked sources.</p>}
      </details>
      {run && <InspectionDetails key={run.run_id} run={run}>{children}</InspectionDetails>}
    </div>
  </section>;
}

function InspectionDetails({ run, children }: { run: WorkflowRun; children?: (run: WorkflowRun) => ReactNode }) {
  const [open, setOpen] = useState(false);
  return <details className="execution-section execution-inspect" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><ChevronsUpDown size={15} /><span>Run details</span><ChevronRight size={15} className="disclosure-chevron" /></summary>
    {open && children?.(run)}
    <Link className="execution-text-link" href={`/dashboard/workflows/${run.run_id}`}>Open full run details<ChevronRight size={14} /></Link>
  </details>;
}
