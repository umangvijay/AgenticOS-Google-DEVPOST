"use client";

/**
 * Queue status panel (Phase 3, §3.19).
 *
 * Shows the state of ACCEPTED work only — backed by a real command/run
 * identifier and the backend's persisted status. It NEVER invents a queue
 * position or a start time: the backend does not compute those, and a fake
 * "position 3 of 7" would be a lie the user plans around.
 */

export type QueueState =
  | "queued"          // accepted, waiting for capacity
  | "dispatched"      // accepted, handed to a worker
  | "running"         // actively executing
  | "completed"
  | "expired";        // exceeded the maximum-wait policy

const STATE_COPY: Record<QueueState, { label: string; detail: string; color: string }> = {
  queued: { label: "Queued", detail: "Accepted and waiting for capacity.", color: "var(--info, #3b82f6)" },
  dispatched: { label: "Starting", detail: "Handed to a worker.", color: "var(--info, #3b82f6)" },
  running: { label: "Running", detail: "Executing now.", color: "var(--success, #22c55e)" },
  completed: { label: "Completed", detail: "Finished.", color: "var(--text-tertiary, #888)" },
  expired: {
    label: "Expired",
    detail: "Waited longer than the queue policy allows and was not started. Its accounting entry shows whether any usage was incurred — see the budget panel.",
    color: "var(--warning, #eab308)",
  },
};

export default function QueueStatusPanel({ state, runId, commandId }: {
  state: QueueState;
  runId?: string;
  commandId?: string;
}) {
  const copy = STATE_COPY[state];
  return (
    <div
      role="status"
      data-state={state}
      data-testid="queue-status-panel"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "8px 12px",
        borderRadius: 10,
        border: "1px solid var(--border, rgba(128,128,128,0.25))",
        fontSize: 13,
      }}
    >
      <span
        aria-hidden
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: copy.color,
          flexShrink: 0,
          ...(state === "running" ? { animation: "pulse 1.2s ease-in-out infinite" } : {}),
        }}
      />
      <div>
        <strong>{copy.label}</strong>
        <span style={{ color: "var(--text-secondary)", marginLeft: 6 }}>{copy.detail}</span>
        {(runId || commandId) && (
          <div style={{ color: "var(--text-tertiary)", fontSize: 11, marginTop: 2 }}>
            {runId ? <>Run <code>{runId}</code></> : null}
            {runId && commandId ? " · " : null}
            {commandId ? <>Command <code>{commandId}</code></> : null}
          </div>
        )}
      </div>
    </div>
  );
}
