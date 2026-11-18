"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, jitteredDelay } from "@/lib/api";

/**
 * Command status hook (Phase 4 §4.20).
 *
 * Resolves UNKNOWN acceptance (transport timeout after submit) by polling
 * the stable status URL with the SAME idempotency identity — a timeout is
 * never treated as rejection. Polling is bounded (§3.19: observation
 * retries stay within a policy) and jittered so many clients never
 * synchronize into a wave.
 */

export type CommandState =
  | "ACCEPTED" | "QUEUED" | "RUNNING" | "WAITING"
  | "CANCEL_REQUESTED" | "SUCCEEDED" | "FAILED" | "CANCELLED" | "EXPIRED";

export interface CommandStatus {
  command_id: string;
  run_id?: string;
  build_id?: string;
  state: CommandState;
  state_version?: number;   // optional: receipts may omit it pre-hydration
  waiting_reason?: string;
  terminal: boolean;
  terminal_reason?: string;
  cancel_requested_at?: number;
  cancel_acknowledged_at?: number;
  result_reference?: string;
  status_url?: string;
}

const TERMINAL_STATES = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"]);

export function useCommandStatus(commandId: string | null, opts?: {
  pollMs?: number;          // base poll interval (jittered); default 2s
  maxPolls?: number;        // bounded observation (default 30)
}) {
  const pollMs = opts?.pollMs ?? 2000;
  const maxPolls = opts?.maxPolls ?? 30;
  const [status, setStatus] = useState<CommandStatus | null>(null);
  const [error, setError] = useState("");
  const polls = useRef(0);
  const cancelled = useRef(false);

  // §18.18 (finding F8): the loop decides from the CURRENT request
  // result — never a status captured by an older closure.
  const refresh = useCallback(async (): Promise<CommandStatus | null> => {
    if (!commandId) return null;
    try {
      const data = await apiFetch<CommandStatus>(
        `/commands/${encodeURIComponent(commandId)}`);
      if (!cancelled.current) {
        setStatus(data);
        setError("");
        return data;
      }
      return null;
    } catch (err) {
      if (!cancelled.current) {
        setError(err instanceof Error ? err.message : "Could not load command status");
      }
      return null;
    }
  }, [commandId]);

  useEffect(() => {
    cancelled.current = false;
    polls.current = 0;
    if (!commandId) return;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const current = await refresh();
      polls.current += 1;
      const terminalNow = TERMINAL_STATES.has(current?.state || "");
      if (cancelled.current || terminalNow || polls.current >= maxPolls) {
        // §18.18: "polling stopped" is distinct from "server task
        // stopped" — a non-terminal stop is policy exhaustion, and the
        // UI surfaces it via error/state, never as a terminal state.
        return;
      }
      timer = setTimeout(tick, jitteredDelay(pollMs));
    };
    void tick();
    return () => { cancelled.current = true; clearTimeout(timer); };
  }, [commandId, refresh, pollMs, maxPolls]);

  return { status, error, refresh };
}
