"use client";

import { WorkflowEvent } from "@/lib/api";
import { format, isValid } from "date-fns";
import { CheckCircle2, PlayCircle, AlertCircle, RefreshCw, XCircle, ShieldAlert } from "lucide-react";

export default function ExecutionTimeline({ events }: { events: WorkflowEvent[] }) {
  const latestFrame = [...events].reverse().map((event) => event.sanitized_metadata?.browser_frame)
    .find((value): value is { image: string; url: string; step: number } => {
      if (!value || typeof value !== "object") return false;
      const frame = value as Record<string, unknown>;
      return typeof frame.image === "string" && /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(frame.image)
        && typeof frame.url === "string" && typeof frame.step === "number";
    });
  const getIcon = (type: string) => {
    switch (type) {
      case "WORKFLOW_STARTED":
      case "TASK_STARTED":
      case "TOOL_INVOKED":
        return <PlayCircle className="w-5 h-5 text-blue-400" />;
      case "WORKFLOW_COMPLETED":
      case "TASK_COMPLETED":
      case "TOOL_COMPLETED":
        return <CheckCircle2 className="w-5 h-5 text-green-400" />;
      case "WORKFLOW_FAILED":
      case "TASK_FAILED":
        return <XCircle className="w-5 h-5 text-destructive" />;
      case "APPROVAL_REQUIRED":
        return <ShieldAlert className="w-5 h-5 text-amber-500" />;
      case "TASK_RECOVERING":
      case "TASK_RETRYING":
        return <RefreshCw className="w-5 h-5 text-purple-400" />;
      default:
        return <AlertCircle className="w-5 h-5 text-muted-foreground" />;
    }
  };

  return (
    <div className="bg-card border border-border rounded-xl p-4 h-full flex flex-col">
      <h3 className="font-semibold text-lg mb-4">Execution Timeline</h3>
      {latestFrame && (
        <figure className="mb-4 overflow-hidden rounded-lg border border-border">
          {/* Browser frames are authenticated event data, not remote image URLs. */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={latestFrame.image} alt={`Browser page at step ${latestFrame.step}`} className="w-full" />
          <figcaption className="p-2 text-xs text-muted-foreground break-all">
            Browser · Step {latestFrame.step} · {latestFrame.url}
            <span className="block">Latest captured view. Form fields are hidden.</span>
          </figcaption>
        </figure>
      )}
      <div className="flex-1 overflow-y-auto pr-2 space-y-4">
        {events.length === 0 ? (
          <p className="text-sm text-muted-foreground italic">No events yet.</p>
        ) : (
          events.map((event, idx) => (
            <div key={event.event_id || idx} className="flex space-x-3 items-start animate-in slide-in-from-left-2 duration-300">
              <div className="mt-0.5">{getIcon(event.type)}</div>
              <div className="flex-1 space-y-1">
                <p className="text-sm font-medium leading-none text-foreground">{event.summary}</p>
                <div className="flex items-center text-xs text-muted-foreground space-x-2">
                  <span>{event.timestamp && isValid(new Date(event.timestamp)) ? format(new Date(event.timestamp), "HH:mm:ss") : ""}</span>
                  {event.task_id && <span className="px-1.5 py-0.5 bg-muted rounded font-mono text-[10px]">{event.task_id}</span>}
                </div>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
