"use client";

import { Task } from "@/lib/api";

function formatTime(value?: string): string | null {
  if (!value) return null;
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

/**
 * Present the result fields users actually care about as readable rows
 * (the audit's "Report step is thin" finding), with the full raw payload
 * kept available in a collapsible block instead of the default view.
 */
function ResultSummary({ output }: { output: Record<string, unknown> }) {
  const rows: Array<[string, string]> = [];

  const push = (label: string, value: unknown) => {
    if (value === undefined || value === null || value === "") return;
    const text = typeof value === "string" ? value : JSON.stringify(value);
    rows.push([label, text.length > 4000 ? text.slice(0, 4000) + "…" : text]);
  };

  // Friendly headline fields first, in reporting order.
  for (const key of ["reply", "message", "summary", "answer", "url", "status_code", "http_status", "method", "ok", "score", "count"]) {
    if (key in output) push(key === "ok" ? "Success" : key.replace(/_/g, " "), output[key]);
  }

  // Anything else beyond the headline fields and known bookkeeping keys.
  const seen = new Set(rows.map(([label]) => label.replace(/ /g, "_")));
  for (const [key, value] of Object.entries(output)) {
    if (seen.has(key) || ["tool_result", "fallback", "extracted_text", "browser_frame"].includes(key)) continue;
    push(key.replace(/_/g, " "), value);
  }

  if (rows.length === 0) return null;

  return (
    <div className="space-y-2">
      {rows.map(([label, value]) => (
        <div key={label} className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 items-start">
          <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider pt-0.5 whitespace-nowrap">{label}</span>
          <span className="text-sm break-words whitespace-pre-wrap">{value}</span>
        </div>
      ))}
    </div>
  );
}

export default function TaskDetailsPanel({ task }: { task: Task | null }) {
  if (!task) {
    return (
      <div className="bg-card border border-border rounded-xl p-4 h-full flex flex-col items-center justify-center text-center space-y-2">
        <p className="text-muted-foreground italic text-sm">Select a node to view details</p>
      </div>
    );
  }

  const started = formatTime(task.started_at);
  const completed = formatTime(task.completed_at);
  const hasOutput = task.output_data && Object.keys(task.output_data).length > 0;

  return (
    <div className="bg-card border border-border rounded-xl p-4 h-full flex flex-col space-y-4 overflow-y-auto">
      <div>
        <h3 className="font-semibold text-lg">{task.title || task.task_id}</h3>
        <p className="text-xs text-muted-foreground font-mono">{task.task_id}</p>
        <p className="text-sm text-muted-foreground">Agent: {task.agent}</p>
        {task.tool && <p className="text-sm text-muted-foreground">Tool: {task.tool}</p>}
      </div>

      <div className="flex items-center space-x-2 text-sm">
        <span className="font-semibold">Status:</span>
        <span className="px-2 py-0.5 rounded bg-muted text-foreground">{task.status}</span>
      </div>

      {(started || completed) && (
        <div className="space-y-1 text-sm">
          {started && <p><span className="font-semibold text-foreground">Started:</span> <span className="text-muted-foreground">{started}</span></p>}
          {completed && <p><span className="font-semibold text-foreground">Completed:</span> <span className="text-muted-foreground">{completed}</span></p>}
        </div>
      )}

      <div className="space-y-1">
        <span className="text-sm font-semibold text-foreground">Attempts:</span>
        <span className="text-sm text-muted-foreground ml-2">{task.attempt}</span>
      </div>

      {task.error && (
        <div className="p-3 bg-destructive/10 border border-destructive/20 rounded-lg">
          <p className="text-xs font-semibold text-destructive uppercase tracking-wider mb-1">{task.error_type || 'Error'}</p>
          <p className="text-sm text-destructive font-mono break-words">{task.error}</p>
        </div>
      )}

      {hasOutput && (
        <div className="space-y-2">
          <p className="text-sm font-semibold">Result</p>
          <ResultSummary output={task.output_data!} />
          <details className="mt-1">
            <summary className="text-xs text-muted-foreground cursor-pointer select-none hover:text-foreground">
              Raw output (JSON)
            </summary>
            <pre className="mt-2 p-2 bg-black border border-border rounded text-xs font-mono text-gray-300 overflow-x-auto break-words whitespace-pre-wrap max-h-64 overflow-y-auto">
              {JSON.stringify(task.output_data, null, 2)}
            </pre>
          </details>
        </div>
      )}
    </div>
  );
}
