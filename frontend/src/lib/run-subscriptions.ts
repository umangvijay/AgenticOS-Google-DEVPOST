"use client";

/** One stream and one snapshot reconciler per run, shared by every consumer. */
import { subscribeWorkflowEvents, getWorkflow } from "./api";
import type { WorkflowEvent, WorkflowRun } from "./api";

type Listener = (payload: { event: WorkflowEvent | null; run: WorkflowRun | null }) => void;
interface Subscription {
  runId: string;
  listeners: Set<Listener>;
  unsubscribeStream: (() => void) | null;
  pollTimer: ReturnType<typeof setInterval> | null;
  lastEventAt: number;
  fetching: boolean;
  dirty: boolean;
  terminalEvent: boolean;
  stopped: boolean;
  snapshot: WorkflowRun | null;
}
const subscriptions = new Map<string, Subscription>();
const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW", "EXPIRED"]);
const STREAM_SILENCE_MS = 10_000;
const POLL_MS = 2_500;

function stopTransport(sub: Subscription) {
  sub.unsubscribeStream?.();
  sub.unsubscribeStream = null;
  if (sub.pollTimer) clearInterval(sub.pollTimer);
  sub.pollTimer = null;
}

async function reconcile(sub: Subscription): Promise<void> {
  if (sub.stopped) return;
  if (sub.fetching) { sub.dirty = true; return; }
  sub.fetching = true;
  try {
    const run = await getWorkflow(sub.runId);
    if (sub.stopped) return;
    // An event arriving during this read may refer to a newer persisted
    // version. Read again before publishing, rather than reverting the UI.
    if (sub.dirty) return;
    sub.snapshot = run;
    for (const listener of sub.listeners) listener({ event: null, run });
    // Wait for the terminal event too, so reopening a finished run can
    // replay its timeline before closing the stream.
    if (TERMINAL.has(run.status) && (sub.terminalEvent || Date.now() - sub.lastEventAt >= STREAM_SILENCE_MS)) stopTransport(sub);
  } catch {
    // The watchdog retries failed reads, even if the final event closed SSE.
    sub.lastEventAt = 0;
  } finally {
    sub.fetching = false;
    if (sub.dirty && !sub.stopped) {
      sub.dirty = false;
      void reconcile(sub);
    }
  }
}

function start(sub: Subscription) {
  sub.lastEventAt = Date.now();
  // Always watch for silent disconnects, not only explicit stream errors.
  sub.pollTimer = setInterval(() => {
    if (!sub.fetching && (sub.terminalEvent || Date.now() - sub.lastEventAt >= STREAM_SILENCE_MS)) void reconcile(sub);
  }, POLL_MS);
  try {
    sub.unsubscribeStream = subscribeWorkflowEvents(sub.runId, event => {
      if (sub.stopped) return;
      sub.lastEventAt = Date.now();
      if (event.type.startsWith("WORKFLOW_") && TERMINAL.has(event.type.slice(9))) sub.terminalEvent = true;
      for (const listener of sub.listeners) listener({ event, run: null });
      // Event metadata can omit or redact output. Fetch the saved tasks;
      // never infer the assistant's answer from a completion notification.
      if (/^(TASK_|WORKFLOW_|APPROVAL_)/.test(event.type)) void reconcile(sub);
    }, () => { void reconcile(sub); }, err => {
      if (sub.stopped) return;
      const message = err instanceof Error ? err.message : String(err);
      if (/expired|sign in|401|403/i.test(message)) {
        sub.stopped = true;
        stopTransport(sub);
        return;
      }
      sub.lastEventAt = 0;
      void reconcile(sub);
    });
  } catch {
    sub.lastEventAt = 0;
  }
  void reconcile(sub);
}

export function subscribeRun(runId: string, listener: Listener): () => void {
  let sub = subscriptions.get(runId);
  if (!sub) {
    sub = { runId, listeners: new Set([listener]), unsubscribeStream: null,
      pollTimer: null, lastEventAt: 0, fetching: false, dirty: false,
      terminalEvent: false, stopped: false, snapshot: null };
    subscriptions.set(runId, sub);
    start(sub);
  } else {
    sub.listeners.add(listener);
    if (sub.snapshot) listener({ event: null, run: sub.snapshot });
  }
  const owned = sub;
  return () => {
    owned.listeners.delete(listener);
    if (owned.listeners.size === 0) {
      owned.stopped = true;
      stopTransport(owned);
      subscriptions.delete(runId);
    }
  };
}

export function subscriptionCount(runId: string): number {
  return subscriptions.get(runId)?.listeners.size ?? 0;
}
