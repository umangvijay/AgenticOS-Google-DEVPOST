"use client";

import { BudgetErrorBody } from "@/lib/api";

/**
 * Budget-blocked messaging (Phase 2, §2.16).
 *
 * User-facing copy for the structured backend errors (§2.15): exhausted
 * budgets show what was needed vs available; unavailable accounting says
 * paid work is paused (never "0 usage"); uncertain outcomes explain that the
 * allowance stays reserved while the outcome is checked.
 */

const DEFAULT_EXHAUSTED_MESSAGE =
  "This task needs an allowance of approximately {required} tokens. You currently have {available} available.";

function fill(template: string, values: Record<string, string>) {
  return template.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? `{${key}}`);
}

function formatTokens(n: number | undefined) {
  if (n == null) return "—";
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}K`;
  return String(n);
}

export default function BudgetBlockedNotice({ error, onRetry }: {
  error: BudgetErrorBody | null;
  onRetry?: () => void;
}) {
  if (!error) return null;

  let message: string;
  let tone: "blocked" | "paused" | "checking" = "blocked";

  switch (error.code) {
    case "BUDGET_EXHAUSTED":
      message = fill(DEFAULT_EXHAUSTED_MESSAGE, {
        required: formatTokens(error.required),
        available: formatTokens(error.available),
      });
      break;
    case "ACCOUNTING_UNAVAILABLE":
      tone = "paused";
      message = "Usage information is temporarily unavailable. New paid work is paused.";
      break;
    case "RESERVATION_CONFLICT":
      message = "A conflicting request for the same task is already in progress. Refresh to see its outcome.";
      break;
    case "SETTLEMENT_CONFLICT":
      message = "This task's usage is being reconciled. Its outcome will appear shortly.";
      tone = "checking";
      break;
    case "BUDGET_POLICY_INVALID":
      message = error.message || "This request is not allowed by the current usage policy.";
      break;
    default:
      message = error.message || "The request could not be started.";
  }

  const palette = {
    blocked: { border: "var(--error, #ef4444)", bg: "8%", label: "Budget limit reached" },
    paused: { border: "var(--warm)", bg: "8%", label: "Paid work paused" },
    checking: { border: "var(--warm)", bg: "8%", label: "Verifying outcome" },
  }[tone];

  return (
    <div
      role="alert"
      data-testid="budget-blocked-notice"
      style={{
        padding: "12px 16px", borderRadius: "var(--radius-md)", fontSize: 13, lineHeight: 1.55,
        border: `1px solid color-mix(in srgb, ${palette.border} 45%, transparent)`,
        background: `color-mix(in srgb, ${palette.border} ${palette.bg}, transparent)`,
        color: "var(--text-secondary)",
      }}
    >
      <strong style={{ display: "block", marginBottom: 4, color: "var(--text-primary)" }}>
        {palette.label}
      </strong>
      {message}
      {error.code === "BUDGET_EXHAUSTED" && (
        <span style={{ display: "block", marginTop: 4, fontSize: 12, color: "var(--text-tertiary)" }}>
          Your allowance resets at the start of the next UTC day. Usage already recorded and
          reservations in progress are not affected.
        </span>
      )}
      {onRetry && (
        <button className="btn btn-ghost btn-sm" style={{ marginTop: 8 }} onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}
