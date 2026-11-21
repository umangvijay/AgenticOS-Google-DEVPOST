"use client";
import { useEffect, useState } from "react";
import type { WorkflowRun, WorkflowEvent } from "@/lib/api";
import { isTerminalRun } from "@/lib/task-states";

export default function RunElapsed({ run, events = [] }: { run: WorkflowRun; events?: WorkflowEvent[] }) {
  const terminal = isTerminalRun(run.status);
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    if (terminal) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [terminal]);
  const finish = Math.max(...events.filter(e => /^WORKFLOW_(COMPLETED|FAILED|CANCELLED|EXPIRED|NEEDS_REVIEW)$/.test(e.type))
    .map(e => Date.parse(e.timestamp)).filter(Number.isFinite),
    ...(run.tasks ?? []).map(t => Date.parse(t.completed_at ?? "")).filter(Number.isFinite));
  const end = terminal ? finish : now;
  const seconds = end === null ? NaN : Math.floor((end - Date.parse(run.created_at)) / 1000);
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  return <span className="run-elapsed"> · {seconds >= 60 ? Math.floor(seconds / 60) + "m " : ""}{seconds % 60}s elapsed</span>;
}
