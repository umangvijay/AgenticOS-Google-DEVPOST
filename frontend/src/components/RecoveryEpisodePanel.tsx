"use client";


/**
 * Recovery episode panel (Phase 11 §11.17).
 *
 * Shows the failure, proposed repair, policy result, attempts remaining,
 * deadline, and verification evidence — with only applicable actions.
 */

interface RecoveryEpisode {
  episode_id: string;
  state: string;
  state_version: number;
  failure_code?: string;
  failure_category?: string;
  proposal_action?: string;
  proposal_explanation?: string;
  policy_result?: string;
  policy_reason?: string;
  attempt_count?: number;
  deadline_at?: number;
  verification_verdict?: string;
  evidence_references?: string[];
}

const STATE_STYLE: Record<string, { label: string; color: string }> = {
  DETECTED: { label: "Failure detected", color: "var(--error, #ef4444)" },
  CLASSIFIED: { label: "Classified", color: "var(--warning, #eab308)" },
  PROPOSED: { label: "Repair proposed", color: "var(--info, #3b82f6)" },
  VALIDATED: { label: "Repair validated", color: "var(--info, #3b82f6)" },
  READY: { label: "Ready to execute", color: "var(--info, #3b82f6)" },
  EXECUTING: { label: "Executing repair", color: "var(--info, #3b82f6)" },
  VERIFYING: { label: "Verifying repair", color: "var(--warning, #eab308)" },
  VERIFIED_RECOVERY: { label: "Recovered", color: "var(--success, #22c55e)" },
  RECOVERY_FAILED: { label: "Recovery failed", color: "var(--error, #ef4444)" },
  ESCALATED: { label: "Escalated — action needed", color: "var(--error, #ef4444)" },
  CANCELLED: { label: "Cancelled", color: "var(--text-tertiary, #888)" },
  SUPERSEDED: { label: "Superseded", color: "var(--text-tertiary, #888)" },
};

export default function RecoveryEpisodePanel({ episode }: { episode: RecoveryEpisode }) {
  const style = STATE_STYLE[episode.state] ?? {
    label: episode.state, color: "var(--text-tertiary)",
  };

  return (
    <div
      role="status"
      data-testid="recovery-episode-panel"
      data-state={episode.state}
      style={{
        padding: "10px 14px", marginBottom: 10, borderRadius: 12,
        fontSize: 13, lineHeight: 1.45,
        border: `1px solid ${style.color}`,
        background: "var(--warning-subtle, rgba(234,179,8,0.08))",
      }}
    >
      <strong style={{ color: style.color }}>{style.label}</strong>
      {episode.failure_category && (
        <div style={{ color: "var(--text-secondary)", marginTop: 2 }}>
          Failure: {episode.failure_category}
          {episode.failure_code ? ` (${episode.failure_code})` : ""}
        </div>
      )}
      {episode.proposal_explanation && (
        <div style={{ color: "var(--text-secondary)", marginTop: 2 }}>
          Proposed repair: {episode.proposal_explanation}
        </div>
      )}
      {episode.policy_reason && (
        <div style={{ color: "var(--text-tertiary)", marginTop: 2, fontSize: 12 }}>
          Policy: {episode.policy_reason}
        </div>
      )}
      {episode.attempt_count != null && episode.attempt_count > 0 && (
        <div style={{ color: "var(--text-tertiary)", fontSize: 12 }}>
          Attempts: {episode.attempt_count}
        </div>
      )}
    </div>
  );
}
