"use client";

/**
 * Cancellation status (Phase 4 §4.15/§4.20).
 *
 * The frontend wording distinguishes the truthful lifecycle states — never
 * implying that cancellation reverses already-committed external effects:
 * - "Cancellation requested."  — intent recorded, worker not yet acked
 * - "Stopping."                — worker acknowledged, still finishing
 * - "Cancelled."               — handling finished
 * - "Completed before cancellation took effect." — honest race outcome
 * - "Stopped; an external outcome still needs review." — reconciliation
 */

export type CancellationPhase =
  | "requested"
  | "stopping"
  | "cancelled"
  | "completed_anyway"
  | "needs_review";

const PHASE_COPY: Record<CancellationPhase, { text: string; color: string }> = {
  requested: {
    text: "Cancellation requested. The worker acknowledges stopping separately — external effects that already happened are not undone.",
    color: "var(--warning, #eab308)",
  },
  stopping: {
    text: "Stopping. The worker is finishing cancellation handling.",
    color: "var(--warning, #eab308)",
  },
  cancelled: {
    text: "Cancelled.",
    color: "var(--text-tertiary, #888)",
  },
  completed_anyway: {
    text: "Completed before cancellation took effect.",
    color: "var(--success, #22c55e)",
  },
  needs_review: {
    text: "Stopped; an external outcome still needs review.",
    color: "var(--warning, #eab308)",
  },
};

export default function CancellationStatus({ phase }: { phase: CancellationPhase }) {
  const copy = PHASE_COPY[phase];
  return (
    <div
      role="status"
      data-testid="cancellation-status"
      data-phase={phase}
      style={{
        padding: "10px 14px", marginBottom: 12,
        background: "var(--warning-subtle, rgba(234,179,8,0.12))",
        borderRadius: 12, fontSize: 13, lineHeight: 1.45,
        border: `1px solid ${copy.color}`,
      }}
    >
      {copy.text}
    </div>
  );
}

/** Map a durable command state to the truthful cancellation phase. */
export function cancellationPhaseFrom(state: string): CancellationPhase | null {
  switch (state) {
    case "CANCEL_REQUESTED": return "requested";
    case "CANCELLED":
      return "cancelled";
    case "SUCCEEDED":
      return "completed_anyway";
    default:
      return null;
  }
}
