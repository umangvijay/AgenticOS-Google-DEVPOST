/**
 * Explicit task/run states (audit §7 UX): one authoritative mapping from
 * engine states to human labels, badge classes, and colors — used by every
 * surface so "uncertain" and "waiting" are never rendered as generic.
 */
export type StateStyle = { label: string; badge: string; color: string; needsAttention: boolean };

const RUN_STATES: Record<string, StateStyle> = {
  QUEUED: { label: "Queued", badge: "badge-neutral", color: "var(--text-tertiary)", needsAttention: false },
  PENDING: { label: "Queued", badge: "badge-neutral", color: "var(--text-tertiary)", needsAttention: false },
  RUNNING: { label: "Running", badge: "badge-info", color: "var(--info)", needsAttention: false },
  WAITING_APPROVAL: { label: "Waiting for approval", badge: "badge-warn", color: "var(--warning)", needsAttention: true },
  WAITING_CREDENTIALS: { label: "Waiting for credentials", badge: "badge-warn", color: "var(--warning)", needsAttention: true },
  RECOVERING: { label: "Recovering", badge: "badge-info", color: "var(--info)", needsAttention: false },
  OUTCOME_UNCERTAIN: { label: "Outcome uncertain", badge: "badge-warn", color: "var(--warning)", needsAttention: true },
  COMPLETED: { label: "Completed", badge: "badge-success", color: "var(--success)", needsAttention: false },
  FAILED: { label: "Failed", badge: "badge-error", color: "var(--error)", needsAttention: true },
  CANCELLED: { label: "Cancelled", badge: "badge-neutral", color: "var(--text-tertiary)", needsAttention: false },
  // §12.2/§18.1: execution finished but the outcome is NOT verified
  // success — needs its own honest state, never rendered as Completed.
  NEEDS_REVIEW: { label: "Needs review", badge: "badge-warn", color: "var(--warning)", needsAttention: true },
  VERIFYING: { label: "Verifying", badge: "badge-info", color: "var(--info)", needsAttention: false },
  WAITING: { label: "Waiting", badge: "badge-neutral", color: "var(--text-tertiary)", needsAttention: false },
};

export function runStateStyle(status: string | undefined | null): StateStyle {
  return RUN_STATES[(status || "").toUpperCase()] || {
    label: status || "Unknown", badge: "badge-neutral", color: "var(--text-tertiary)", needsAttention: false,
  };
}

/** §18.1: the GOAL OUTCOME state — separate from the execution lifecycle.
  Derived from the durable evaluation (§12.3): a task can finish executing
  while the outcome is uncertain, partial, or unverified. */
export type OutcomeState =
  | "verified" | "partial" | "uncertain" | "needs_review"
  | "failed" | "pending_evaluation" | "unavailable";

const OUTCOME_STATES: Record<OutcomeState, StateStyle> = {
  verified: { label: "Verified", badge: "badge-success", color: "var(--success)", needsAttention: false },
  partial: { label: "Partially completed", badge: "badge-warn", color: "var(--warning)", needsAttention: true },
  uncertain: { label: "Outcome uncertain", badge: "badge-warn", color: "var(--warning)", needsAttention: true },
  needs_review: { label: "Needs review", badge: "badge-warn", color: "var(--warning)", needsAttention: true },
  failed: { label: "Failed", badge: "badge-error", color: "var(--error)", needsAttention: true },
  pending_evaluation: { label: "Checking result", badge: "badge-info", color: "var(--info)", needsAttention: false },
  unavailable: { label: "Verification unavailable", badge: "badge-neutral", color: "var(--text-tertiary)", needsAttention: true },
};

export function outcomeStateStyle(state: OutcomeState): StateStyle {
  return OUTCOME_STATES[state] ?? OUTCOME_STATES.unavailable;
}

/** §18.1: derive the outcome state from a durable evaluation record
  (server-derived — the UI never upgrades a claim the evidence lacks). */
export function outcomeFromEvaluation(ev: {
  success?: boolean; failed_tasks?: number | null;
  needs_review_tasks?: string[] | null; uncertain_effects?: string[] | null;
  partially_applied_effects?: string[] | null; unverified_effects?: string[] | null;
} | null | undefined): OutcomeState {
  if (!ev) return "pending_evaluation";
  if (ev.success) return "verified";
  if ((ev.needs_review_tasks?.length ?? 0) > 0) return "needs_review";
  if ((ev.partially_applied_effects?.length ?? 0) > 0) return "partial";
  if ((ev.uncertain_effects?.length ?? 0) > 0 ||
      (ev.unverified_effects?.length ?? 0) > 0) return "uncertain";
  if ((ev.failed_tasks ?? 0) > 0) return "failed";
  return "uncertain";
}

/** Connector lifecycle clarity (§7): separate generated / authenticated /
    tested / active instead of one broad badge. */
export function connectorStateBadges(integration: {
  trust_tier?: string; artifact_type?: string; is_enabled?: boolean;
  spec_provenance?: string; test_coverage?: { read_probe_passed?: number; total?: number };
}): Array<{ label: string; badge: string }> {
  const badges: Array<{ label: string; badge: string }> = [];
  badges.push({
    label: integration.artifact_type === "standalone_server" ? "standalone server" : "adapter",
    badge: "badge-neutral",
  });
  if ((integration.test_coverage?.read_probe_passed ?? 0) > 0) badges.push({ label: "tested", badge: "badge-info" });
  if (integration.trust_tier === "verified") badges.push({ label: "verified", badge: "badge-success" });
  else badges.push({ label: "needs review", badge: "badge-warn" });
  if (integration.is_enabled) badges.push({ label: "active", badge: "badge-success" });
  return badges;
}

/** §15.26: progress counters derive from AUTHORITATIVE task states —
  never incremented locally on received events. Duplicate or late events
  cannot inflate the counts because the source of truth is the task
  list the server reconciled. */
export function deriveProgress(tasks: Array<{
  status?: string | null;
}> | null | undefined): {
  total: number; completed: number; running: number;
  waiting: number; failed: number;
} {
  const t = tasks ?? [];
  const count = (fn: (s: string) => boolean) => t.filter(x => fn((x.status || "").toUpperCase())).length;
  return {
    total: t.length,
    completed: count(s => s === "COMPLETED"),
    running: count(s => s === "RUNNING" || s === "VERIFYING" || s === "RECOVERING"),
    waiting: count(s => s.startsWith("WAITING")),
    failed: count(s => s === "FAILED" || s === "NEEDS_REVIEW" ||
                       s === "OUTCOME_UNCERTAIN"),
  };
}

/** Phase 17 §17.21: the artifact VERDICT badge mapping — the single
  source the ArtifactEvidencePanel imports (no copied logic). */
const ARTIFACT_VERDICTS: Record<string, { label: string; badge: string }> = {
  pass: { label: "Verified", badge: "badge-success" },
  fail: { label: "Failed", badge: "badge-error" },
  needs_review: { label: "Needs review", badge: "badge-warn" },
  unavailable: { label: "Unverified", badge: "badge-neutral" },
};

export function artifactVerdictStyle(verdict: string | undefined | null): {
  label: string; badge: string;
} {
  return ARTIFACT_VERDICTS[(verdict || "").toLowerCase()] ?? {
    label: verdict || "Unverified", badge: "badge-neutral",
  };
}

/** Phase 16 §16.28: the MEMORY lifecycle states — "deleted" never means
  a card disappeared locally; each state names what actually happened. */
export type MemoryLifecycleState =
  | "saved" | "indexing" | "retrievable" | "superseded"
  | "deletion_requested" | "hidden" | "cleanup_complete" | "cleanup_failed";

const MEMORY_STATES: Record<MemoryLifecycleState, { label: string; badge: string }> = {
  saved: { label: "Saved", badge: "badge-neutral" },
  indexing: { label: "Indexing", badge: "badge-info" },
  retrievable: { label: "Available for retrieval", badge: "badge-success" },
  superseded: { label: "Superseded", badge: "badge-neutral" },
  deletion_requested: { label: "Deletion requested", badge: "badge-warn" },
  hidden: { label: "Hidden from retrieval", badge: "badge-warn" },
  cleanup_complete: { label: "Cleanup complete", badge: "badge-success" },
  cleanup_failed: { label: "Cleanup failed — retry", badge: "badge-error" },
};

export function memoryStateStyle(state: string): { label: string; badge: string } {
  return MEMORY_STATES[state as MemoryLifecycleState] ??
    { label: state || "Unknown", badge: "badge-neutral" };
}

/** The server-derived UI state for a memory record (§16.28): the row's
  durable fields decide — a client cannot invent a state. */
export function memoryUiState(record: {
  deletion_state?: string; superseded_by?: string | null;
  verification_status?: string;
}): { label: string; badge: string } {
  if (record.deletion_state === "hidden" ||
      record.deletion_state === "deletion_requested") {
    return memoryStateStyle(record.deletion_state);
  }
  if (record.superseded_by) return memoryStateStyle("superseded");
  if (record.verification_status === "confirmed") {
    return memoryStateStyle("retrievable");
  }
  return memoryStateStyle("saved");
}

export function isTerminalRun(status?: string) {
  return ["COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW", "EXPIRED"].includes(status || "");
}
