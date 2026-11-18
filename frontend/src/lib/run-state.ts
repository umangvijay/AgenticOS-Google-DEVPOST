"use client";

/**
 * Shared state contracts (Phase 18 §18.8) — the SIX dimensions are
 * tracked SEPARATELY. A connection failure never turns a running task
 * into a failed task; execution completion never implies verified
 * deliverables. Every page derives the same meaning from the same
 * backend state via these deterministic mappers.
 */

export type ExecutionState =
  | "queued" | "running" | "waiting" | "recovering" | "finished"
  | "unknown";
export type OutcomeState =
  | "verified" | "failed" | "partial" | "uncertain" | "needs_review"
  | "not_evaluated" | "unknown";
export type ConnectionState =
  | "connecting" | "connected" | "reconnecting" | "offline" | "unavailable";
export type FreshnessState = "current" | "stale" | "resynchronizing";
export type MutationState =
  | "idle" | "submitting" | "accepted" | "acceptance_unknown" | "conflicted";
export type ArtifactState =
  | "generated" | "stored" | "verifying" | "verified" | "stale"
  | "delivered" | "none";

const EXECUTION_MAP: Record<string, ExecutionState> = {
  QUEUED: "queued", PENDING: "queued",
  RUNNING: "running", VERIFYING: "running", PROCESSING: "running",
  WAITING_APPROVAL: "waiting", WAITING_CREDENTIALS: "waiting", WAITING: "waiting",
  RECOVERING: "recovering",
  COMPLETED: "finished", FAILED: "finished", CANCELLED: "finished",
  NEEDS_REVIEW: "finished", EXPIRED: "finished",
};

const OUTCOME_MAP: Record<string, OutcomeState> = {
  VERIFIED: "verified", COMPLETED: "not_evaluated",
  FAILED: "failed", PARTIAL: "partial",
  OUTCOME_UNCERTAIN: "uncertain", NEEDS_REVIEW: "needs_review",
};

export function executionState(status: string | undefined | null): ExecutionState {
  const mapped = EXECUTION_MAP[(status || "").toUpperCase()];
  // Unknown server values degrade to "unknown" — never guessed.
  return mapped ?? (status ? "unknown" : "unknown");
}

export function outcomeStateFor(status: string | undefined | null): OutcomeState {
  const mapped = OUTCOME_MAP[(status || "").toUpperCase()];
  if (mapped) return mapped;
  // Execution finished but no evaluation yet → not evaluated (never
  // silently verified).
  if (executionState(status) === "finished") return "not_evaluated";
  return "unknown";
}

/** The artifact lifecycle → the artifact dimension (§18.8). */
export function artifactStateFor(lifecycle: string | undefined | null,
                                 verification?: { stale?: boolean; verdict?: string } | null,
                                 ): ArtifactState {
  const stale = verification?.stale === true;
  if (lifecycle === "delivered") return stale ? "stale" : "delivered";
  if (lifecycle === "verification_passed") return stale ? "stale" : "verified";
  if (lifecycle === "verification_failed") return "stale";
  if (lifecycle === "verification_completed") return "verifying";
  if (lifecycle === "stored") return "stored";
  if (lifecycle === "generated") return "generated";
  return "none";
}

/** A connection failure does NOT change execution state. */
export function presentationFor(run: { status?: string | null },
                                connection: ConnectionState,
                                freshness: FreshnessState): {
  execution: ExecutionState; outcome: OutcomeState;
  degraded: boolean;
} {
  const execution = executionState(run.status);
  const outcome = outcomeStateFor(run.status);
  // Offline/reconnecting marks the VIEW degraded, never the TASK failed.
  const degraded = connection === "offline" || connection === "reconnecting" ||
    freshness === "stale";
  return { execution, outcome, degraded };
}

/** Deterministic handling for contradictory responses: the LAST
  terminal state wins; a terminal state never reverts to running. */
export function reconcileContradiction(previous: ExecutionState,
                                       next: ExecutionState): ExecutionState {
  const terminal: ExecutionState[] = ["finished"];
  if (terminal.includes(previous) && !terminal.includes(next) &&
      next === "running") {
    return previous;   // a late running event cannot un-finish a task
  }
  return next;
}
