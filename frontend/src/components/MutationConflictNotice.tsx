"use client";

/**
 * Mutation conflict notice (Phase 18 §18.22) — the standardized
 * conflict envelope display: what was requested, which version applies,
 * and the permitted next action. Never fabricates success.
 */
export interface ConflictInfo {
  code?: string;              // e.g. REVISION_MISMATCH
  message?: string;
  current_version?: number;
  next_action?: string;
}

export function MutationConflictNotice({ conflict, onAction }: {
  conflict: ConflictInfo;
  onAction?: () => void;
}) {
  if (!conflict.code && !conflict.message) return null;
  return (
    <div role="alert"
         style={{ padding: 12, borderRadius: 8, border: "1px solid var(--warning)",
                  background: "var(--bg-tertiary)", fontSize: 13, marginTop: 8 }}>
      <strong style={{ color: "var(--warning)" }}>
        {conflict.code ?? "CONFLICT"}
      </strong>
      <p style={{ margin: "4px 0" }}>{conflict.message}</p>
      {conflict.current_version !== undefined && (
        <p style={{ margin: "4px 0", color: "var(--text-tertiary)" }}>
          Current version: {conflict.current_version}
        </p>
      )}
      {conflict.next_action && (
        <p style={{ margin: "4px 0" }}>
          Next action: {conflict.next_action}
          {onAction && (
            <button className="btn btn-secondary btn-sm" style={{ marginLeft: 8 }}
                    onClick={onAction}>
              Refresh
            </button>
          )}
        </p>
      )}
    </div>
  );
}
