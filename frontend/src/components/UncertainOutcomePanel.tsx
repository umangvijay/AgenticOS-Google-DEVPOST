"use client";

import { useState } from "react";
import { apiFetch } from "@/lib/api";

/**
 * Uncertain outcome panel (Phase 5 §5.19).
 *
 * Explains what AgentOS attempted, what is confirmed, what remains unknown,
 * and which actions are permitted — never a generic Retry button for an
 * unknown outcome (§5.16: an unknown outcome without a replay guarantee
 * must not be automatically re-dispatched).
 */

export interface EffectSummary {
  effect_id: string;
  operation: string;
  state: string;
  state_version: number;
  target: string;
  terminal: boolean;
  permitted_actions: {
    reconcile: boolean;
    retry: boolean;
    compensate: boolean;
    close_unresolved: boolean;
  };
  receipts?: {
    provider_status: string;
    provider_operation_id?: string;
    detail?: { accepted?: string[]; refused?: string[]; note?: string };
  }[];
}

const STATE_COPY: Record<string, { title: string; body: string; tone: string }> = {
  DISPATCHING: {
    title: "Submitting…",
    body: "The action is being dispatched to the external service.",
    tone: "var(--info, #3b82f6)",
  },
  PROVIDER_PENDING: {
    title: "Accepted by the provider",
    body: "The provider accepted the work; the outcome is being verified.",
    tone: "var(--info, #3b82f6)",
  },
  OUTCOME_UNKNOWN: {
    title: "Outcome unknown",
    body: "The connection ended before Sandtable could confirm the result. The action may have been submitted. Automatic resend is paused while the outcome is checked.",
    tone: "var(--warning, #eab308)",
  },
  PARTIALLY_APPLIED: {
    title: "Partially applied",
    body: "Some recipients/items were accepted; others were refused. The accepted ones are preserved and will not be repeated.",
    tone: "var(--warning, #eab308)",
  },
  CONFIRMED_NOT_APPLIED: {
    title: "Not applied",
    body: "Evidence confirms the action did not go through. You can retry it.",
    tone: "var(--error, #ef4444)",
  },
  CONFIRMED_APPLIED: {
    title: "Applied",
    body: "Evidence confirms the action went through.",
    tone: "var(--success, #22c55e)",
  },
  CLOSED_UNRESOLVED: {
    title: "Closed without a confirmed outcome",
    body: "Investigation stopped without determining the result. The historical uncertainty is preserved.",
    tone: "var(--text-tertiary, #888)",
  },
  CANCELLED_BEFORE_DISPATCH: {
    title: "Cancelled before submission",
    body: "Nothing was sent.",
    tone: "var(--text-tertiary, #888)",
  },
};

export default function UncertainOutcomePanel({ effect }: { effect: EffectSummary }) {
  const [checking, setChecking] = useState(false);
  const [notice, setNotice] = useState("");
  const copy = STATE_COPY[effect.state] ?? {
    title: effect.state, body: "", tone: "var(--text-tertiary)",
  };

  const checkOutcome = async () => {
    setChecking(true);
    setNotice("");
    try {
      const updated = await apiFetch<{ resolved?: boolean }>(
        `/effects/${encodeURIComponent(effect.effect_id)}/reconcile`,
        { method: "POST" });
      setNotice(updated.resolved
        ? "The outcome was confirmed from evidence."
        : "No sufficient evidence yet — the outcome remains unknown.");
    } catch {
      setNotice("The check could not complete; try again shortly.");
    } finally {
      setChecking(false);
    }
  };

  return (
    <div
      role="status"
      data-testid="uncertain-outcome-panel"
      data-state={effect.state}
      style={{
        padding: "10px 14px", marginBottom: 10, borderRadius: 12, fontSize: 13,
        lineHeight: 1.45, border: `1px solid ${copy.tone}`,
        background: "var(--warning-subtle, rgba(234,179,8,0.08))",
      }}
    >
      <strong style={{ color: copy.tone }}>{copy.title}</strong>
      {copy.body && (
        <div style={{ color: "var(--text-secondary)", marginTop: 2 }}>{copy.body}</div>
      )}
      {effect.target && (
        <div style={{ color: "var(--text-tertiary)", marginTop: 2, fontSize: 12 }}>
          Target: <code>{effect.target}</code>
        </div>
      )}
      {effect.receipts?.map((r, i) => (
        <div key={i} style={{ color: "var(--text-tertiary)", fontSize: 12, marginTop: 2 }}>
          Evidence: <code>{r.provider_status}</code>
          {r.detail?.refused?.length ? ` · refused: ${r.detail.refused.join(", ")}` : ""}
        </div>
      ))}
      {notice && (
        <div style={{ color: "var(--text-tertiary)", marginTop: 4, fontSize: 12 }}>{notice}</div>
      )}
      {effect.permitted_actions.reconcile && (
        <button
          type="button"
          onClick={checkOutcome}
          disabled={checking}
          style={{
            marginTop: 6, padding: "4px 12px", borderRadius: 8,
            border: `1px solid ${copy.tone}`, background: "transparent",
            color: copy.tone, fontSize: 12, cursor: "pointer",
          }}
        >
          {checking ? "Checking…" : "Check outcome"}
        </button>
      )}
    </div>
  );
}
