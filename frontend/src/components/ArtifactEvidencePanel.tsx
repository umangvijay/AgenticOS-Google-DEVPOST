"use client";

/**
 * Artifact evidence panel (Phase 17 §17.21): what is ready, what
 * remains uncertain, and why — generated, verified, and reviewed states
 * stay SEPARATE. A fetch error renders an explicit error, never an
 * empty "all checks passed" panel.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  DeliverableRecord, getDeliverable, reviewDeliverableClaim,
} from "@/lib/api";
import { decorativeMotionAllowed } from "@/lib/motion";
import { artifactVerdictStyle } from "@/lib/task-states";

export function ArtifactEvidencePanel({ deliverableId }: {
  deliverableId: string;
}) {
  const [record, setRecord] = useState<DeliverableRecord | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generationRef = useRef(0);
  const motionAllowed = decorativeMotionAllowed();

  const [lastRefreshedAt, setLastRefreshedAt] = useState<number | null>(null);
  const [reviewError, setReviewError] = useState("");
  const load = useCallback(() => {
    // §18.21 (finding F10): every request carries a GENERATION guard —
    // a response for a CHANGED deliverable ID is discarded.
    const generation = ++generationRef.current;
    getDeliverable(deliverableId)
      .then((res) => {
        if (generation !== generationRef.current) return;
        setRecord(res); setError(""); setReviewError("");
        setLastRefreshedAt(Date.now());
      })
      .catch((err: unknown) => {
        if (generation !== generationRef.current) return;
        setError(err instanceof Error ? err.message : "Could not load the artifact evidence");
      });
  }, [deliverableId]);
  useEffect(() => { void load(); }, [load]);

  const review = async (field: string, action: "confirm" | "remove") => {
    if (!record) return;
    setBusy(true);
    setReviewError("");
    try {
      // Tied to the EXACT revision — a delayed response cannot approve
      // a newer draft accidentally (§17.22).
      await reviewDeliverableClaim(deliverableId, record.revision, action,
                                   { field });
      load();
    } catch (err: unknown) {
      // §18.21: review failures are ACTIONABLE, never silent.
      const message = err instanceof Error ? err.message : "The review action failed";
      if (/409|revision/i.test(message)) {
        setReviewError("This artifact changed while you were reviewing — the view is refreshing with the current revision.");
        load();
      } else {
        setReviewError(`The ${action} action failed: ${message}`);
      }
    } finally {
      setBusy(false);
    }
  };

  if (error) {
    return (
      <section className="glass-card" style={{ padding: 24 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600 }}>Artifact evidence</h2>
        <p style={{ color: "var(--error)", fontSize: 13 }}>
          {error} — the evidence state is UNKNOWN, not “all checks passed”.
        </p>
        <button className="btn btn-secondary btn-sm" onClick={load}>Retry</button>
      </section>
    );
  }
  if (!record) return <div className="spinner" />;

  const v = record.verification ?? {};
  const checks = v.checks ?? [];
  const claims = v.unsupported_claims ?? [];
  const isDelivered = record.lifecycle_state === "delivered";

  return (
    <section className="glass-card" style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>Artifact evidence</h2>
        <span className={`badge ${artifactVerdictStyle(v.verdict).badge}`}>
          {artifactVerdictStyle(v.verdict).label}
        </span>
      </div>
      {reviewError && (
        <p role="alert" style={{ color: "var(--error)", fontSize: 13, marginTop: 8 }}>
          {reviewError}
        </p>
      )}
      <p style={{ fontSize: 12, color: "var(--text-tertiary)", marginTop: 6 }}>
        Revision {record.revision}
        {record.parent_revision ? ` (from r${record.parent_revision})` : ""}
        {" · "}{record.deliverable_type}
        {" · "}digest {record.content_checksum?.slice(0, 12) || "—"}
        {isDelivered ? " · DELIVERED" : " · not yet delivered"}
        {lastRefreshedAt ? ` · refreshed ${new Date(lastRefreshedAt).toLocaleTimeString()}` : ""}
      </p>
      {record.verification?.stale === true && (
        <p style={{ color: "var(--warning)", fontSize: 13 }}>
          ⚠ Verification STALE — a source changed after this artifact was
          verified. Regenerate or re-review (§17.24).
        </p>
      )}

      <h3 style={{ fontSize: 13, fontWeight: 600, margin: "14px 0 6px" }}>Files</h3>
      <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: 12.5, fontFamily: "var(--font-mono)" }}>
        {record.file_manifest.map((f) => (
          <li key={f.path} style={{ padding: "3px 0" }}>
            {f.path} <span style={{ color: "var(--text-tertiary)" }}>
              ({f.byte_length ?? "?"} bytes)
            </span>
          </li>
        ))}
      </ul>

      <h3 style={{ fontSize: 13, fontWeight: 600, margin: "14px 0 6px" }}>Verification checks</h3>
      <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: 13 }}>
        {checks.map((c, i) => (
          <li key={i} style={{ padding: "3px 0" }}>
            <span className={`badge ${c.status === "pass" ? "badge-success" : c.status === "missing" ? "badge-warn" : "badge-error"}`}>
              {c.status}
            </span>{" "}
            {c.check}{c.detail ? ` — ${c.detail}` : ""}
            {motionAllowed && c.status === "pass" && null}
          </li>
        ))}
        {!checks.length && <li style={{ color: "var(--text-tertiary)" }}>No checks recorded yet.</li>}
      </ul>

      {claims.length > 0 && (
        <>
          <h3 style={{ fontSize: 13, fontWeight: 600, margin: "14px 0 6px" }}>
            Unsupported or conflicting claims
          </h3>
          <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: 13 }}>
            {claims.map((c, i) => (
              <li key={i} style={{ padding: "6px 0", borderTop: "1px solid var(--border-primary)" }}>
                <strong>{c.field}</strong> — {c.reason}
                <div style={{ marginTop: 4 }}>
                  <button className="btn btn-ghost btn-sm" disabled={busy}
                          onClick={() => void review(c.field, "confirm")}>
                    Confirm as user-attested
                  </button>{" "}
                  <button className="btn btn-ghost btn-sm" disabled={busy}
                          onClick={() => void review(c.field, "remove")}>
                    Remove claim
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      {(v.blocking_reasons ?? []).length > 0 && (
        <p style={{ color: "var(--error)", fontSize: 13, marginTop: 12 }}>
          Blocking: {(v.blocking_reasons ?? []).join("; ")}
        </p>
      )}
    </section>
  );
}
