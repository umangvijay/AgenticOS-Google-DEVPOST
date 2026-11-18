"use client";

import { useEffect, useState } from "react";
import { AdmissionErrorBody } from "@/lib/api";

/**
 * Admission & overload messaging (Phase 3, §3.19).
 *
 * Distinguishes the seven overload states instead of showing one generic
 * error: rate limited / capacity unavailable / queue full / provider
 * throttled / admission unavailable / policy invalid / budget blocked
 * (budget has its own BudgetBlockedNotice). Retry timing is shown ONLY when
 * the backend supplied it — no invented queue positions or start times.
 */

const STATE_COPY: Record<string, { title: string; body: string; tone: Tone }> = {
  RATE_LIMITED: {
    title: "Too many requests",
    body: "You've sent a lot of requests in a short window. Give it a moment and try again.",
    tone: "warning",
  },
  TENANT_CAPACITY_EXHAUSTED: {
    title: "Workspace at capacity",
    body: "Your workspace already has the maximum amount of work running. Retry after something finishes.",
    tone: "warning",
  },
  QUEUE_FULL: {
    title: "Too much queued work",
    body: "Your workspace has a lot of work waiting. Retry after some of it completes.",
    tone: "warning",
  },
  PROVIDER_THROTTLED: {
    title: "Provider is limiting execution",
    body: "The AI provider is temporarily limiting requests. This usually clears on its own shortly.",
    tone: "warning",
  },
  ADMISSION_UNAVAILABLE: {
    title: "Can't safely start new work",
    body: "The system can't currently decide whether to accept new work, so it paused instead of guessing. Please retry shortly.",
    tone: "paused",
  },
  POLICY_INVALID: {
    title: "Request refused by policy",
    body: "This request was refused by a server-side policy. If this keeps happening, contact support.",
    tone: "error",
  },
};

type Tone = "warning" | "paused" | "error";

const TONE_STYLE: Record<Tone, { bg: string; color: string }> = {
  warning: { bg: "var(--warning-subtle, rgba(234,179,8,0.12))", color: "var(--warning, #eab308)" },
  paused: { bg: "var(--info-subtle, rgba(59,130,246,0.12))", color: "var(--info, #3b82f6)" },
  error: { bg: "var(--error-subtle, rgba(239,68,68,0.12))", color: "var(--error, #ef4444)" },
};

function formatWait(seconds: number): string {
  if (seconds <= 0) return "now";
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.ceil(seconds / 60);
  return `${minutes} min`;
}

export default function AdmissionNotice({ error, onRetry }: {
  error: AdmissionErrorBody | null;
  onRetry?: () => void;
}) {
  const retryAfter = error?.retry_after_seconds ?? 0;
  const decisionId = error?.decision_id ?? "";
  const [remaining, setRemaining] = useState(retryAfter);
  const [prevKey, setPrevKey] = useState(`${retryAfter}:${decisionId}`);
  // react-hooks v6 (set-state-in-effect): the reset adjusts DURING
  // RENDER when the admission error changes — the official
  // adjust-on-prop-change pattern.
  const nextKey = `${retryAfter}:${decisionId}`;
  if (prevKey !== nextKey) {
    setPrevKey(nextKey);
    setRemaining(retryAfter);
  }

  useEffect(() => {
    if (!retryAfter) return;
    const started = Date.now();
    const timer = setInterval(() => {
      const left = retryAfter - Math.floor((Date.now() - started) / 1000);
      setRemaining(Math.max(0, left));
      if (left <= 0) clearInterval(timer);
    }, 1000);
    return () => clearInterval(timer);
  }, [retryAfter, decisionId]);

  if (!error) return null;

  const state = STATE_COPY[error.code] ?? {
    title: "Request not accepted",
    body: error.message || "The request was not accepted. Please retry shortly.",
    tone: "warning" as Tone,
  };
  const style = TONE_STYLE[state.tone];
  const retryable = remaining <= 0 && onRetry && error.code !== "POLICY_INVALID";

  return (
    <div
      role="alert"
      style={{
        padding: "10px 14px",
        marginBottom: 12,
        background: style.bg,
        borderRadius: 12,
        fontSize: 13,
        lineHeight: 1.45,
        border: `1px solid ${style.color}`,
      }}
    >
      <strong style={{ color: style.color }}>{state.title}</strong>
      <div style={{ color: "var(--text-secondary)", marginTop: 2 }}>{state.body}</div>
      {error.retry_after_seconds != null && error.retry_after_seconds > 0 && (
        <div style={{ color: "var(--text-tertiary)", marginTop: 2, fontSize: 12 }}>
          {remaining > 0 ? `Retry in ~${formatWait(remaining)}` : "You can retry now."}
        </div>
      )}
      {retryable && (
        <button
          type="button"
          onClick={onRetry}
          style={{
            marginTop: 6,
            padding: "4px 12px",
            borderRadius: 8,
            border: `1px solid ${style.color}`,
            background: "transparent",
            color: style.color,
            fontSize: 12,
            cursor: "pointer",
          }}
        >
          Retry
        </button>
      )}
    </div>
  );
}
