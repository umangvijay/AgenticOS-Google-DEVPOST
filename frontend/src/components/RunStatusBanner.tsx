"use client";

/**
 * Run status banner (Phase 18 §18.25) — the ONE consistent display of
 * execution, outcome, connection, and freshness for any run. Every page
 * derives the same meaning from the same backend state (§18.8).
 */
import { presentationFor } from "@/lib/run-state";
import type { ConnectionState, FreshnessState } from "@/lib/run-state";
import { runStateStyle } from "@/lib/task-states";

export function RunStatusBanner({ status, connection, freshness }: {
  status: string | undefined | null;
  connection: ConnectionState;
  freshness: FreshnessState;
}) {
  const view = presentationFor({ status }, connection, freshness);
  const st = runStateStyle(status);
  const notices: string[] = [];
  if (view.degraded) {
    if (connection === "offline") notices.push("You are offline — work is preserved locally; monitoring is paused.");
    else if (connection === "reconnecting") notices.push("Reconnecting — the view may be stale until the stream resumes.");
    else if (freshness === "stale") notices.push("Data may be out of date — refresh is pending.");
  }
  return (
    <div role="status" aria-live="polite"
         style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
      <span className={`badge ${st.badge}`}>{st.label}</span>
      {view.degraded && (
        <span style={{ fontSize: 12, color: "var(--warning)" }}>
          {notices.join(" ")}
        </span>
      )}
    </div>
  );
}
