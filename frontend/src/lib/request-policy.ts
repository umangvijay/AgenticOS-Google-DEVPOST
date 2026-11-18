"use client";

/**
 * Request policy (Phase 18 §18.9, finding F2/probe 4) — the ONE place
 * request cancellation, deadlines, and correlation live.
 *
 * - Caller signals are COMBINED with the timeout, never overwritten
 *   (AbortSignal.any with a manual fallback for older browsers).
 * - An already-aborted caller signal means the request NEVER reaches
 *   the transport.
 * - The operation deadline persists across CSRF prep, refresh, and
 *   authorized retry (one total deadline).
 */

export interface RequestOptions extends Omit<RequestInit, "signal"> {
  /** Caller cancellation. */
  signal?: AbortSignal;
  /** Per-attempt timeout; the TOTAL deadline spans retries. */
  timeoutMs?: number;
  /** Total operation deadline across CSRF/refresh/retry. */
  deadlineMs?: number;
  /** Correlation ID attached as a header. */
  requestId?: string;
  /** Idempotency identity for writes. */
  idempotencyKey?: string;
}

/** Compose N signals into one — fires when ANY aborts. Uses
  AbortSignal.any when available; a manual fallback otherwise. */
export function combineSignals(...signals: (AbortSignal | undefined)[]): AbortSignal {
  const present = signals.filter((s): s is AbortSignal => !!s);
  if (present.length === 0) {
    return AbortSignal.timeout(60_000);
  }
  if (present.length === 1) return present[0];
  const anyFn = (AbortSignal as unknown as {
    any?: (signals: AbortSignal[]) => AbortSignal;
  }).any;
  if (typeof anyFn === "function") {
    return anyFn.call(AbortSignal, present);
  }
  // Manual fallback: one controller aborted by any source.
  const controller = new AbortController();
  for (const s of present) {
    if (s.aborted) {
      controller.abort(s.reason);
      break;
    }
    s.addEventListener("abort", () => controller.abort(s.reason), {
      once: true,
    });
  }
  return controller.signal;
}

/** An already-aborted signal short-circuits BEFORE the transport. */
export function throwIfAborted(signal: AbortSignal | undefined): void {
  if (signal?.aborted) {
    throw signal.reason ?? new DOMException("Aborted", "AbortError");
  }
}

/** A deadline that SURVIVES retries: one absolute end time. */
export class OperationDeadline {
  private readonly startedAt: number;
  private readonly totalMs: number;

  constructor(totalMs?: number) {
    this.totalMs = totalMs ?? 30_000;
    this.startedAt = Date.now();
  }

  /** Remaining per-attempt budget, bounded by the caller timeout. */
  remainingMs(attemptTimeoutMs?: number): number {
    const left = this.totalMs - (Date.now() - this.startedAt);
    if (left <= 0) return 0;
    if (attemptTimeoutMs !== undefined) {
      return Math.min(left, attemptTimeoutMs);
    }
    return left;
  }

  get expired(): boolean {
    return this.remainingMs() <= 0;
  }
}
