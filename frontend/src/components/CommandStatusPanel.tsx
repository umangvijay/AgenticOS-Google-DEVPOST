"use client";

import { CommandStatus } from "@/hooks/useCommandStatus";

/**
 * Command status panel (Phase 4 §4.20).
 *
 * Shows the BACKEND's durable lifecycle states — including the distinct
 * "acceptance unknown", "cancellation requested" vs "stopped", and "outcome
 * under reconciliation" states. No invented progress percentages: the
 * backend does not compute them.
 */

const STATE_COPY: Record<string, { label: string; detail: string; color: string }> = {
  ACCEPTED: {
    label: "Accepted",
    detail: "Recorded durably; dispatch is starting.",
    color: "var(--info, #3b82f6)",
  },
  QUEUED: {
    label: "Queued",
    detail: "Accepted and waiting for capacity.",
    color: "var(--info, #3b82f6)",
  },
  RUNNING: {
    label: "Running",
    detail: "Executing now.",
    color: "var(--success, #22c55e)",
  },
  WAITING: {
    label: "Waiting",
    detail: "Progress depends on a dependency, approval, or retry time.",
    color: "var(--warning, #eab308)",
  },
  CANCEL_REQUESTED: {
    label: "Cancellation requested",
    detail: "Stopping was requested; the worker acknowledges separately.",
    color: "var(--warning, #eab308)",
  },
  SUCCEEDED: {
    label: "Completed",
    detail: "Finished successfully.",
    color: "var(--success, #22c55e)",
  },
  FAILED: {
    label: "Failed",
    detail: "No further automatic progress. You can request a retry.",
    color: "var(--error, #ef4444)",
  },
  CANCELLED: {
    label: "Cancelled",
    detail: "Cancellation handling finished.",
    color: "var(--text-tertiary, #888)",
  },
  EXPIRED: {
    label: "Expired",
    detail: "Its allowed lifetime ended before it could run.",
    color: "var(--warning, #eab308)",
  },
};

const WAITING_DETAIL: Record<string, string> = {
  capacity: "Waiting for execution capacity.",
  retry_backoff: "Will retry automatically at the next scheduled time.",
  child_build: "Waiting for an integration build to finish.",
  human_approval: "Waiting for your approval.",
  browser_handoff: "Waiting for you to finish in the browser window.",
  budget: "Waiting for budget allowance.",
  authorization_review: "Waiting for an authorization check.",
  outcome_reconciliation: "An external outcome is being verified before continuing.",
};

export default function CommandStatusPanel({ status }: {
  status: CommandStatus | null;
}) {
  if (!status) return null;
  const copy = STATE_COPY[status.state] ?? {
    label: status.state, detail: "", color: "var(--text-tertiary)",
  };
  const waiting = status.waiting_reason ? WAITING_DETAIL[status.waiting_reason] : null;
  return (
    <div
      role="status"
      data-testid="command-status-panel"
      data-state={status.state}
      style={{
        display: "flex", alignItems: "center", gap: 10,
        padding: "8px 12px", borderRadius: 10,
        border: "1px solid var(--border, rgba(128,128,128,0.25))",
        fontSize: 13,
      }}
    >
      <span aria-hidden style={{
        width: 8, height: 8, borderRadius: "50%", background: copy.color,
        flexShrink: 0,
        ...(status.state === "RUNNING" ? { animation: "pulse 1.2s ease-in-out infinite" } : {}),
      }} />
      <div>
        <strong>{copy.label}</strong>
        <span style={{ color: "var(--text-secondary)", marginLeft: 6 }}>
          {waiting ?? copy.detail}
        </span>
        <div style={{ color: "var(--text-tertiary)", fontSize: 11, marginTop: 2 }}>
          {status.run_id ? <>Run <code>{status.run_id}</code></> : null}
          {status.run_id && status.build_id ? " · " : null}
          {status.build_id ? <>Build <code>{status.build_id}</code></> : null}
          {status.terminal_reason ? ` · ${status.terminal_reason}` : ""}
        </div>
      </div>
    </div>
  );
}
