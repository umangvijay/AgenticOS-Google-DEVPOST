"use client";

/**
 * One owner for run subscriptions (Phase 18 §18.14, finding F4).
 * Reference-counted per run: the timeline, graph, and evidence panels
 * all consume the SAME subscription instead of each opening one. A
 * fallback poller runs only while the stream is down, coordinated —
 * never competing.
 */
import { subscribeWorkflowEvents, getWorkflow } from "./api";
import type { WorkflowEvent, WorkflowRun } from "./api";

type Listener = (payload: { event: WorkflowEvent | null; run: WorkflowRun | null }) => void;

interface Subscription {
  runId: string;
  listeners: Set<Listener>;
  unsubscribeStream: (() => void) | null;
  pollTimer: ReturnType<typeof setInterval> | null;
  lastEventAt: number;
}

const subscriptions = new Map<string, Subscription>();
const STREAM_SILENCE_MS = 20_000;   // heartbeat policy: poll after silence
const POLL_MS = 5_000;

function startStream(sub: Subscription): void {
  if (sub.unsubscribeStream) return;
  sub.lastEventAt = Date.now();
  try {
    const stop = subscribeWorkflowEvents(
      sub.runId,
      (event: WorkflowEvent) => {
        sub.lastEventAt = Date.now();
        for (const listener of sub.listeners) {
          listener({ event, run: null });
        }
      },
      () => { /* open */ },
      (err: Error) => {
        // §18.16: transient errors start the coordinated fallback
        // poller; definitive access loss stops the subscription.
        if (!/expired|sign in|401|403/i.test(err.message)) {
          startPoller(sub);
        }
      },
    );
    sub.unsubscribeStream = stop;
  } catch {
    startPoller(sub);
  }
}

function startPoller(sub: Subscription): void {
  if (sub.pollTimer) return;
  sub.pollTimer = setInterval(async () => {
    // §18.14: the poller is COORDINATED — it yields while the stream
    // is delivering (recent event) and reconciles after silence.
    if (Date.now() - sub.lastEventAt < STREAM_SILENCE_MS) return;
    try {
      const run = await getWorkflow(sub.runId);
      for (const listener of sub.listeners) {
        listener({ event: null, run });
      }
    } catch {
      /* the next tick retries; errors do not kill the poller */
    }
  }, POLL_MS);
}

function stopIfUnused(sub: Subscription): void {
  if (sub.listeners.size === 0) {
    if (sub.unsubscribeStream) {
      sub.unsubscribeStream();
      sub.unsubscribeStream = null;
    }
    if (sub.pollTimer) {
      clearInterval(sub.pollTimer);
      sub.pollTimer = null;
    }
    subscriptions.delete(sub.runId);
  }
}

/** Subscribe to a run. Returns an unsubscribe that cleans up THIS
  consumer's reference (Strict Mode's double setup/cleanup cycle is
  safe: the second setup re-subscribes the same refcount). */
export function subscribeRun(runId: string,
                             listener: Listener): () => void {
  let sub = subscriptions.get(runId);
  if (!sub) {
    sub = { runId, listeners: new Set(), unsubscribeStream: null,
            pollTimer: null, lastEventAt: 0 };
    subscriptions.set(runId, sub);
    startStream(sub);
  }
  sub.listeners.add(listener);
  return () => {
    sub.listeners.delete(listener);
    stopIfUnused(sub);
  };
}

/** Test/diagnostic helper: the live subscription count per run. */
export function subscriptionCount(runId: string): number {
  return subscriptions.get(runId)?.listeners.size ?? 0;
}
