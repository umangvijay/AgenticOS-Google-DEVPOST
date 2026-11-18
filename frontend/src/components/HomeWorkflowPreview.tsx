"use client";

import { FormEvent, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { submitGoal } from "@/lib/api";

const DEFAULT_GOAL = "Research the market, gather current sources, and make a concise brief for my team.";

export default function HomeWorkflowPreview() {
  const router = useRouter();
  const { isAuthenticated, loginAsGuest } = useAuth();
  const formRef = useRef<HTMLFormElement>(null);
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function startWorkflow(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const requestedGoal = goal.trim();
    if (!requestedGoal || busy) return;

    setBusy(true);
    setError("");
    try {
      if (!isAuthenticated) await loginAsGuest();
      const run = await submitGoal(requestedGoal);
      router.push(`/dashboard/workspace/${encodeURIComponent(run.run_id)}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not start this workflow. Please try again.");
      setBusy(false);
    }
  }

  const outcome = goal.trim() || DEFAULT_GOAL;

  return (
    <div className="orrery-preview-wrap" style={{ marginTop: 32, zIndex: 10 }}>
      <div className="orrery-preview" aria-label="Interactive Sandtable workspace preview">
        <div className="orrery-preview-topbar">
          <span className="preview-window-dots"><i /><i /><i /></span>
          <span className="preview-task-title">A workspace shaped around your goal</span>
          <span className="preview-project-pill">Workspace / Live run</span>
          <span className="preview-label">LIVE WORKSPACE</span>
        </div>
        <div className="orrery-preview-body">
          <section className="preview-conversation">
            <span className="preview-kicker">YOUR OUTCOME</span>
            <div className="preview-user-prompt">{outcome}</div>
            <div className="preview-agent-note">
              <span className="preview-orb">v</span>
              <div><strong>Sandtable</strong><p>Describe the result you want. I’ll plan the work, check for missing tools, and show you the run as it happens.</p></div>
            </div>
            <form ref={formRef} className="preview-composer" onSubmit={startWorkflow}>
              <label className="sr-only" htmlFor="homepage-goal">Describe the outcome you want</label>
              <input
                id="homepage-goal"
                value={goal}
                onInput={(event) => { setGoal(event.currentTarget.value); setError(""); }}
                placeholder="Ask for a result, not a recipe…"
                maxLength={12000}
                disabled={busy}
                aria-describedby={error ? "homepage-goal-error" : undefined}
              />
              <button className="preview-send" type="submit" disabled={busy || !goal.trim()} aria-label="Start this workflow">
                {busy ? <span className="preview-spinner" aria-hidden="true" /> : "↑"}
              </button>
            </form>
            {error && <p className="preview-inline-error" id="homepage-goal-error" role="alert">{error}</p>}
          </section>
          <section className="preview-run" aria-label="How your request moves through Sandtable">
            <div className="preview-run-heading"><div><span className="preview-kicker">YOUR RUN</span><h3>{goal.trim() ? "Ready to start" : "From goal to outcome"}</h3></div><span className="preview-status">{busy ? "Starting…" : "In your control"}</span></div>
            <div className="preview-step is-done"><span className="preview-step-icon">1</span><div><strong>Understand the goal</strong><small>Scope and output shaped by your request</small></div><span className="preview-step-time">Ready</span></div>
            <div className="preview-step"><span className="preview-step-icon">2</span><div><strong>Find a path</strong><small>Use available tools or identify a gap</small></div><span className="preview-step-time">Next</span></div>
            <div className="preview-step is-active"><span className="preview-step-icon">3</span><div><strong>Build, review, and act</strong><small>Missing tools are tested; sensitive actions pause for you</small></div><span className="preview-step-time">You decide</span></div>
            <div className="preview-deliverable">
              <span className="preview-file-icon">↗</span>
              <div><strong>Live workspace</strong><small>Run events, approvals, and results appear there</small></div>
              <button className="preview-open" type="button" onClick={() => formRef.current?.requestSubmit()} disabled={busy || !goal.trim()}>{busy ? "Starting" : "Start"}</button>
            </div>
            <p className="preview-disclaimer">Runs use connected services and provider access. Review consequential actions before they execute.</p>
          </section>
        </div>
      </div>
    </div>
  );
}
