"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getWorkflow, WorkflowEvent, WorkflowRun,
} from "@/lib/api";
import { subscribeRun } from "@/lib/run-subscriptions";
import { useNetworkStatus } from "@/hooks/useNetworkStatus";

/**
 * Centralized run-event handling (Phase 18 §18.2):
 *
 * - SSE events deduplicated by event_id (lib layer).
 * - On every (re)connect, state is RECONCILED from the persisted
 *   snapshot — missed events cannot leave stale UI.
 * - Stale-run rejection: events arriving for a superseded runId after a
 *   navigation are ignored (§18.7 race).
 * - Offline-aware: the subscription pauses when the network drops and
 *   re-syncs from the snapshot when it returns.
 * - Terminal runs stop streaming and settle on the snapshot.
 */

const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW", "EXPIRED"]);

export function useRunEvents(runId: string | null | undefined) {
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [connected, setConnected] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  const runIdRef = useRef(runId);
  const { online } = useNetworkStatus();
  const isOnlineRef = useRef(true);
  // §18.17 (finding F7): the authoritative online hint from the hook —
  // connectivity is the reconciliation trigger (§27.D6).
  // (react-hooks v6: refs are written in effects, never during render.)
  useEffect(() => {
    isOnlineRef.current = online;
  }, [online]);

  // Snapshot reconciliation — the source of truth after any gap.
  const reconcile = useCallback(async (id: string) => {
    try {
      const fresh = await getWorkflow(id);
      if (!aliveRef.current || runIdRef.current !== id) return;
      setRun(fresh);
      setLoadError(null);
      return fresh;
    } catch (err) {
      if (!aliveRef.current || runIdRef.current !== id) return;
      setLoadError(err instanceof Error ? err.message : "Could not load the run");
      return undefined;
    }
  }, []);

  // (set-state-in-effect) the runId reset adjusts DURING RENDER — the
  // official adjust-on-prop-change pattern.
  const [resetForRunId, setResetForRunId] = useState(runId);
  if (resetForRunId !== runId) {
    setResetForRunId(runId);
    setRun(null);
    setConnected(false);
    setLoadError(null);
  }
  useEffect(() => {
    aliveRef.current = true;
    runIdRef.current = runId;
    if (!runId) return;

    let unsubscribe: (() => void) | undefined;
    let stopped = false;

    const handleEvent = (event: WorkflowEvent) => {
      // Stale-run rejection: ignore events that arrive after navigation.
      if (!aliveRef.current || runIdRef.current !== runId) return;
      // Apply the event's summary to the in-memory run (the next
      // reconciliation or snapshot refetch is authoritative).
      setRun((prev) => {
        if (!prev) return prev;
        // §18.8 (finding F5): run_status is NOT part of the
        // WorkflowEvent interface — execution state derives from the
        // event TYPE through the shared contracts, then the snapshot
        // reconciliation confirms it from the authoritative record.
        // §19 (C8): an explicit, SCOPED mapping — only run-level
        // WORKFLOW_* events touch run status. A task-completed event
        // NEVER completes the run (substring matching is gone).
        const RUN_LEVEL_EVENTS: Record<string, string> = {
          WORKFLOW_COMPLETED: "COMPLETED",
          WORKFLOW_FAILED: "FAILED",
          WORKFLOW_CANCELLED: "CANCELLED",
          WORKFLOW_EXPIRED: "EXPIRED",
        };
        const isRunScoped = typeof event.type === "string" &&
          event.type.startsWith("WORKFLOW_");
        const status = isRunScoped
          ? RUN_LEVEL_EVENTS[event.type as string]
          : ((event as { status?: string }).status ?? undefined);
        if (status && status !== prev.status) {
          return { ...prev, status };
        }
        return prev;
      });
    };

    const start = async () => {
      // 1) Snapshot first (§18.2: reconnect reconciles from persisted state).
      const fresh = await reconcile(runId);
      if (stopped) return;
      if (fresh && TERMINAL.has(String(fresh.status).toUpperCase())) {
        setConnected(true);   // terminal: no stream needed
        return;
      }
      // 2) Stream deltas — through the SHARED reference-counted
      // subscription manager (§18.14, finding F4): the workspace,
      // timeline, and evidence panels share ONE stream per run instead
      // of each opening its own. Every (re)connect re-reconciles.
      unsubscribe = subscribeRun(
        runId,
        ({ event, run: snapshotRun }) => {
          if (snapshotRun) {
            // The coordinated fallback poller delivered a reconciled
            // snapshot — same contract as our own reconcile.
            if (!aliveRef.current || runIdRef.current !== runId) return;
            setRun(snapshotRun);
            setConnected(true);
            return;
          }
          handleEvent(event as WorkflowEvent);
        },
      );
    };

    void start();

    return () => {
      stopped = true;
      aliveRef.current = false;
      if (unsubscribe) unsubscribe();
    };
  }, [runId, reconcile]);

  // Back online: re-reconcile from the server (§18.7 disconnect).
  // §27.D6: the trigger is actual CONNECTIVITY (`online`), not render
  // quality (`isLowEnd`) — the previous dependency skipped
  // re-reconciliation on real network recovery.
  useEffect(() => {
    if (online && runId) {
      // (set-state-in-effect) reconciliation lands after the fetch's
      // await; the call itself is deferred off the effect's sync window.
      void queueMicrotask(() => { void reconcile(runId); });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [online]);

  return { run, connected, loadError, reconcile };
}

