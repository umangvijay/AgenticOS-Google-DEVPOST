"use client";

import type { Task, WorkflowEvent } from "@/lib/api";
import { Check, ChevronRight, Circle, LoaderCircle, ShieldAlert, SquareTerminal, X } from "lucide-react";

export function executionLabel(status?: string) {
  const labels: Record<string, string> = { COMPLETED: "Completed", RUNNING: "Working", PENDING: "Queued",
    WAITING: "Waiting", WAITING_APPROVAL: "Needs your input", FAILED: "Failed", CANCELLED: "Stopped",
    RETRYING: "Retrying", RECOVERING: "Recovering", NEEDS_REVIEW: "Needs review", EXPIRED: "Expired", BLOCKED: "Blocked", SKIPPED: "Skipped" };
  return labels[status || ""] || "Waiting";
}

function StateIcon({ status }: { status?: string }) {
  if (status === "COMPLETED") return <Check size={15} className="execution-success" />;
  if (["RUNNING", "RETRYING", "RECOVERING"].includes(status || "")) return <LoaderCircle size={15} className="execution-working" />;
  if (status === "FAILED") return <X size={15} className="execution-failed" />;
  if (["NEEDS_REVIEW", "WAITING_APPROVAL", "BLOCKED"].includes(status || "")) return <ShieldAlert size={15} />;
  return <Circle size={13} />;
}

function EventRows({ events }: { events: WorkflowEvent[] }) {
  return <ol className="execution-events">
    {events.map((event, index) => <li key={event.event_id || `${event.type}-${index}`}>
      <SquareTerminal size={14} aria-hidden />
      <div><span>{event.summary || event.type.replaceAll("_", " ").toLowerCase()}</span>
        {event.timestamp && !Number.isNaN(Date.parse(event.timestamp)) &&
          <time dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</time>}
      </div>
    </li>)}
  </ol>;
}

/** Saved task states and event history stay separate: a notification is not an output. */
export default function ExecutionTimeline({ events, tasks = [], compact = false }: {
  events: WorkflowEvent[]; tasks?: Task[]; compact?: boolean;
}) {
  const groups = new Map<string, { task?: Task; events: WorkflowEvent[] }>();
  for (const task of tasks) groups.set(`${task.run_id}:${task.task_id}`, { task, events: [] });
  const runEvents: WorkflowEvent[] = [];
  for (const event of events) {
    if (!event.task_id) { runEvents.push(event); continue; }
    const key = `${event.run_id}:${event.task_id}`;
    if (!groups.has(key)) groups.set(key, { events: [] });
    groups.get(key)!.events.push(event);
  }
  const latestFrame = [...events].reverse().map(e => e.sanitized_metadata?.browser_frame).find(value => {
    if (!value || typeof value !== "object") return false;
    const frame = value as Record<string, unknown>;
    return typeof frame.image === "string" && /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(frame.image)
      && typeof frame.url === "string" && typeof frame.step === "number";
  }) as { image: string; url: string; step: number } | undefined;

  return <div className={`execution-timeline ${compact ? "execution-timeline-compact" : "execution-timeline-full"}`}>
    {!compact && <h3>Execution timeline</h3>}
    {groups.size === 0 && runEvents.length === 0 && <p className="execution-empty">Activity will appear when the run starts.</p>}
    {[...groups].map(([key, group], index) => <details className="execution-step" key={key}>
      <summary>
        <StateIcon status={group.task?.status || group.events.at(-1)?.status} />
        <span className="execution-step-title">{group.task?.title || (group.task?.agent === "core.chat" ? "Respond to your message" : group.task?.tool || `Step ${index + 1}`)}</span>
        {group.task && <span className="execution-step-state">{executionLabel(group.task.status)}</span>}
        <ChevronRight size={14} className="disclosure-chevron" />
      </summary>
      {group.events.length > 0 ? <EventRows events={group.events} /> : <p className="execution-empty">Saved state: {executionLabel(group.task?.status)}. No event history loaded for this step.</p>}
      {group.task?.error && <p className="execution-step-error">{group.task.error}</p>}
    </details>)}
    {runEvents.length > 0 && <details className="execution-step">
      <summary><SquareTerminal size={15} /><span className="execution-step-title">Run activity</span><span className="execution-count">{runEvents.length}</span><ChevronRight size={14} className="disclosure-chevron" /></summary>
      <EventRows events={runEvents} />
    </details>}
    {latestFrame && <details className="execution-step">
      <summary><SquareTerminal size={15} /><span className="execution-step-title">Browser preview</span><ChevronRight size={14} className="disclosure-chevron" /></summary>
      <figure className="execution-browser-frame">
        {/* Authenticated frame from the event stream, never a remote image. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={latestFrame.image} alt={`Browser at step ${latestFrame.step}`} />
        <figcaption>Step {latestFrame.step} · {latestFrame.url}</figcaption>
      </figure>
    </details>}
  </div>;
}
