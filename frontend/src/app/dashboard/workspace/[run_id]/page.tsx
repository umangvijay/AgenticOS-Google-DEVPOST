"use client";

import { useEffect, useRef, useState, CSSProperties } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import {
  getWorkflow, getWorkflowThread, cancelWorkflow, retryWorkflow,
  resumeWorkflow, WorkflowRun, WorkflowEvent, Task,
} from "@/lib/api";
import { subscribeRun } from "@/lib/run-subscriptions";
import { API_BASE, apiFetch } from "@/lib/api";
import { safeSnippet } from "@/lib/safe-snippet";
import ChatComposer from "@/components/ChatComposer";
import CommandStatusPanel from "@/components/CommandStatusPanel";
import CancellationStatus, { cancellationPhaseFrom } from "@/components/CancellationStatus";
import WorkflowGraph from "@/components/WorkflowGraph";
import { getCommandByRun, cancelCommand, CommandReceipt, getEffectsByRun, EffectSummary } from "@/lib/api";
import UncertainOutcomePanel from "@/components/UncertainOutcomePanel";
import BudgetUsagePanel from "@/components/BudgetUsagePanel";
import EvaluationPanel from "@/components/EvaluationPanel";
import { useRunEvents } from "@/hooks/useRunEvents";
import ExecutionTimeline from "@/components/ExecutionTimeline";
import QueueStatusPanel, { QueueState } from "@/components/QueueStatusPanel";
import RunFeedbackPanel from "@/components/RunFeedbackPanel";
import WorkspaceBrowserSessions from "@/components/WorkspaceBrowserSessions";
import { ChatLinkPreviewAnchor, linkifyText, isEligibleHttpUrl } from "@/components/ChatLinkPreview";
import { WorkspacePreviewDock, useDockLayout } from "@/components/WorkspacePreviewDock";
import { clearPrincipalLayout } from "@/lib/workspace-tabs";
import { useAuth } from "@/lib/auth-context";

function bubbleStyle(role: "user" | "agent"): CSSProperties {
  if (role === "user") {
    return {
      alignSelf: "flex-end", maxWidth: "min(640px, 92%)",
      background: "linear-gradient(135deg, var(--accent), var(--accent-pink))",
      color: "white", borderRadius: "18px 18px 4px 18px", padding: "12px 16px",
    };
  }
  return {
    alignSelf: "flex-start", maxWidth: "min(720px, 96%)",
    background: "var(--bg-card)", border: "1px solid var(--border-primary)",
    borderRadius: "18px 18px 18px 4px", padding: "12px 16px",
    backdropFilter: "blur(20px)",
  };
}

function assistantText(task: Task): string | null {
  const out = task.output_data;
  if (!out || typeof out !== "object") return null;
  for (const key of ["reply", "message", "result", "summary"]) {
    const value = out[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  if (Array.isArray(out.tools)) {
    const names = (out.tools as { name?: string }[]).map((t) => t?.name).filter(Boolean).join(", ");
    const mcp = typeof out.mcp_id === "string" ? out.mcp_id : "";
    return `Integration generated${names ? ` with ${names}` : ""}.${mcp ? `\nmcp_id: ${mcp}` : ""}`;
  }
  if (typeof out.status_code === "number") {
    const snippet = safeSnippet(out.data);
    // Health-check results get a full report line, not just "HTTP 200"
    // (audit bug 6: the chat answer under-reported the check).
    const health: string[] = [`HTTP ${out.status_code}`];
    if (typeof out.latency_ms === "number") health.push(`${out.latency_ms} ms`);
    const tls = out.tls as { days_until_expiry?: number; expires_at?: string } | undefined;
    if (tls && typeof tls.days_until_expiry === "number") {
      health.push(`TLS expires in ${tls.days_until_expiry}d${tls.expires_at ? ` (${tls.expires_at.slice(0, 10)})` : ""}`);
    }
    if (typeof out.grade === "string") health.push(`grade: ${out.grade}`);
    const headers = out.security_headers as { missing?: string[] } | undefined;
    const missing = headers && Array.isArray(headers.missing) ? headers.missing : [];
    if (missing.length) health.push(`missing headers: ${missing.join(", ")}`);
    return `${health.join(" · ")}${snippet ? `\n\n${snippet}` : ""}`;
  }
  if (out.ok === true || out.reachable === true || out.sent === true) {
    return safeSnippet(out);
  }
  const pretty = safeSnippet(out, 1200);
  if (pretty && pretty !== "{}" && pretty.length > 0) return pretty;
  return null;
}

function isTerminal(status?: string) {
  return ["COMPLETED", "FAILED", "CANCELLED"].includes(status || "");
}

function isWaitingHuman(workflow: WorkflowRun) {
  return (workflow.tasks || []).some((t) => t.status === "WAITING_APPROVAL");
}

function UserBubble({ goal }: { goal: string }) {
  const [expanded, setExpanded] = useState(false);
  const needsClamp = goal.length > 600;
  return (
    <div style={bubbleStyle("user")}>
      <div style={{ fontSize: 11, opacity: 0.85, marginBottom: 4 }}>You</div>
      <div
        style={{
          whiteSpace: "pre-wrap",
          lineHeight: 1.5,
          ...(!expanded && needsClamp
            ? {
                display: "-webkit-box",
                WebkitLineClamp: 6,
                WebkitBoxOrient: "vertical" as const,
                overflow: "hidden",
              }
            : {}),
        }}
      >
        {/* §28.7: eligible HTTP(S) text becomes a preview anchor; code
            spans, punctuation, balanced parens and line breaks survive. */}
        {linkifyText(goal, (href, content, key) => (
          <ChatLinkPreviewAnchor key={key} href={href} variant="user">
            {content}
          </ChatLinkPreviewAnchor>
        ))}
      </div>
      {needsClamp && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          style={{
            background: "none",
            border: "none",
            color: "rgba(255,255,255,0.75)",
            cursor: "pointer",
            fontSize: 12,
            padding: "4px 0 0",
            textDecoration: "underline",
          }}
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

function TurnView({ workflow }: { workflow: WorkflowRun }) {
  const replies = (workflow.tasks || [])
    .map((t) => ({ task: t, text: t.status === "COMPLETED" ? assistantText(t) : null }))
    .filter((x) => x.text);
  const failed = (workflow.tasks || []).filter((t) => t.status === "FAILED");
  const inFlight = (workflow.tasks || []).some((t) =>
    t.status === "RUNNING" || t.status === "PENDING" || t.status === "WAITING" || t.status === "RETRYING"
  );
  const busy = (!isTerminal(workflow.status) || inFlight) && replies.length === 0 && failed.length === 0;

  return (
    <>
      <UserBubble goal={workflow.goal} />
      {replies.map(({ task, text }) => (
        <div key={`${workflow.run_id}-out-${task.task_id}`} style={bubbleStyle("agent")}>
          <div style={{ fontSize: 11, color: "var(--text-tertiary)", marginBottom: 6 }}>Sandtable</div>
          <div className="agent-markdown">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeSanitize]}
              components={{
                // §28.7: sanitized Markdown anchors share the SAME
                // preview-card interaction; rehypeSanitize still runs and
                // standard anchor semantics (modifier-click, context menu)
                // are preserved.
                a: (props) => {
                  const { href, children } = props;
                  const safeHref = typeof href === "string" ? href : undefined;
                  if (!safeHref || !isEligibleHttpUrl(safeHref)) {
                    return <a {...props} />;
                  }
                  return (
                    <ChatLinkPreviewAnchor href={safeHref} variant="agent">
                      {children}
                    </ChatLinkPreviewAnchor>
                  );
                },
              }}
            >{text!}</ReactMarkdown>
          </div>
        </div>
      ))}
      {failed.map((t) => (
        <div key={`${workflow.run_id}-fail-${t.task_id}`} style={bubbleStyle("agent")}>
          <div style={{ fontSize: 11, color: "var(--error)", marginBottom: 6 }}>Sandtable</div>
          <div style={{ fontSize: 14, lineHeight: 1.55 }}>{t.error || "That step failed. Try again or rephrase the goal."}</div>
        </div>
      ))}
      {(workflow.tasks || []).filter((t) => t.status === "WAITING_APPROVAL").map((t) => (
        <div key={`${workflow.run_id}-wait-${t.task_id}`} style={bubbleStyle("agent")}>
          <div style={{ fontSize: 11, color: "var(--warning, #b45309)", marginBottom: 6 }}>Waiting on you</div>
          <div style={{ fontSize: 14, lineHeight: 1.55, whiteSpace: "pre-wrap" }}>
            {typeof t.output_data?.message === "string"
              ? t.output_data.message
              : "Complete the CAPTCHA, OTP, or MFA in the browser window, then click Resume. Sandtable will not fill those fields."}
          </div>
        </div>
      ))}
      {(() => {
        // §3.19: accepted work shows its REAL queue/run state — backed by
        // the run id the backend issued, never an invented position.
        const queueState: QueueState | null = isTerminal(workflow.status)
          ? (workflow.status === "COMPLETED" ? "completed" : null)
          : (workflow.status === "RUNNING" ? "running" : "queued");
        return queueState ? (
          <div style={{ margin: "8px 0" }}>
            <QueueStatusPanel state={queueState} runId={workflow.run_id} />
          </div>
        ) : null;
      })()}
      {busy && (
        <div style={bubbleStyle("agent")}>
          <span className="typing-dots" style={{ marginRight: 10 }} aria-hidden>
            <span /><span /><span />
          </span>
          Thinking…
        </div>
      )}
      {isTerminal(workflow.status) && workflow.status === "FAILED" && replies.length === 0 && failed.length === 0 && (
        <div style={bubbleStyle("agent")}>
          <div style={{ fontSize: 11, color: "var(--error)", marginBottom: 6 }}>Sandtable</div>
          <div style={{ fontSize: 14, lineHeight: 1.55 }}>That run failed before a reply was written. Try sending again.</div>
        </div>
      )}
    </>
  );
}

export default function WorkspaceChatPage() {
  const params = useParams();
  const runId = params.run_id as string;
  const [turns, setTurns] = useState<WorkflowRun[]>([]);
  const [threadId, setThreadId] = useState(runId);
  // §28.7: the resource dock — persisted per principal + thread; an
  // account switch clears the prior principal's resources.
  const auth = useAuth();
  const principal = auth?.user?.id || "anonymous";
  const [dockLayout, setDockLayout] = useDockLayout(principal, threadId);
  useEffect(() => {
    // Account switch: the dock store clears the prior principal's
    // resources when this principal unmounts.
    return () => { clearPrincipalLayout(principal); };
  }, [principal]);
  const [error, setError] = useState("");
  const [showGraph, setShowGraph] = useState(false);
  const [showTimeline, setShowTimeline] = useState(true);
  const [stopping, setStopping] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const [timelineOpen, setTimelineOpen] = useState(true);
  const endRef = useRef<HTMLDivElement>(null);
  // §13.26: the shared event layer's REAL consumer — snapshot-first
  // reconciliation on mount/reconnect; stale events rejected after
  // navigation. The reconciled run merges into the thread turns.
  const { run: reconciledRun } = useRunEvents(runId);
  useEffect(() => {
    if (!reconciledRun) return;
    // react-hooks v6 (set-state-in-effect): the merge is a pure list
    // update deferred off the effect's synchronous window — semantically
    // identical, without a cascading render inside the effect.
    queueMicrotask(() => {
      setTurns((prev) => {
        const idx = prev.findIndex((t) => t.run_id === reconciledRun.run_id);
        if (idx === -1) return prev;
        if (prev[idx] === reconciledRun) return prev;
        const next = [...prev];
        next[idx] = reconciledRun;
        return next;
      });
    });
  }, [reconciledRun]);

  async function loadThread(anchorId: string) {
    try {
      const data = await getWorkflowThread(anchorId);
      if (data.workflows?.length) {
        setTurns(data.workflows);
        setThreadId(data.thread_id || anchorId);
        return;
      }
    } catch { /* older backends without /thread */ }
    const wf = await getWorkflow(anchorId);
    setTurns([wf]);
    setThreadId(wf.thread_id || wf.run_id);
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await loadThread(runId);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load run");
      }
    })();
    return () => { cancelled = true; };
  }, [runId]);

  const turnStatusKey = turns.map((t) => `${t.run_id}:${t.status}`).join("|");
  const turnIdsKey = turns.map((t) => t.run_id).join("|");

  useEffect(() => {
    let cancelled = false;
    let failCount = 0;
    const tick = async () => {
      try {
        const ids = Array.from(new Set([runId, ...turns.map((t) => t.run_id)]));
        const fresh = await Promise.all(ids.map((id) => getWorkflow(id)));
        if (cancelled) return;
        failCount = 0;
        setError("");
        setTurns((prev) => {
          const byId = new Map(fresh.map((w) => [w.run_id, w]));
          const next = prev.map((w) => byId.get(w.run_id) || w);
          for (const w of fresh) {
            if (!next.some((p) => p.run_id === w.run_id)) next.push(w);
          }
          return next;
        });
      } catch (e) {
        failCount += 1;
        if (!cancelled && failCount >= 3) {
          setError(e instanceof Error ? e.message : "Could not refresh run status");
        }
      }
    };
    const active =
      turns.length === 0 ||
      turns.some((t) => !isTerminal(t.status) || isWaitingHuman(t));
    if (!active) return;
    void tick();
    const id = window.setInterval(() => { void tick(); }, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // Polling is re-bound when a run's status changes. The signature captures the fields
    // from turns that control polling, while outputs are refreshed by the poll itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, turnStatusKey]);

  useEffect(() => {
    // §18.14 (finding F4): per-turn subscriptions go through the SHARED
    // reference-counted manager — no overlapping owners.
    const unsubs = turns.map((turn) => subscribeRun(turn.run_id, ({ event: rawEvent }) => {
      const event = rawEvent as WorkflowEvent | null;
      if (!event) return;
      setEvents((prev) => {
        if (prev.some((e) => e.event_id && e.event_id === event.event_id)) return prev;
        return [...prev, event];
      });
      setTurns((prev) => prev.map((wf) => {
        if (wf.run_id !== turn.run_id) return wf;
        const updated = { ...wf, tasks: [...wf.tasks] };
        if (event.type === "WORKFLOW_COMPLETED") updated.status = "COMPLETED";
        if (event.type === "WORKFLOW_FAILED") updated.status = "FAILED";
        if (event.type === "WORKFLOW_CANCELLED") updated.status = "CANCELLED";
        if (event.task_id) {
          const i = updated.tasks.findIndex((t) => t.task_id === event.task_id);
          if (i >= 0) {
            const task = { ...updated.tasks[i] };
            if (event.type === "TASK_STARTED") task.status = "RUNNING";
            if (event.type === "TASK_COMPLETED") {
              task.status = "COMPLETED";
              if (event.sanitized_metadata?.output) {
                task.output_data = event.sanitized_metadata.output as Record<string, unknown>;
              }
            }
            if (event.type === "TASK_FAILED") {
              task.status = "FAILED";
              task.error = event.summary;
            }
            if (event.type === "APPROVAL_REQUIRED") {
              task.status = "WAITING_APPROVAL";
              task.output_data = {
                ...(task.output_data || {}),
                message: event.summary,
                ...(event.sanitized_metadata || {}),
              };
            }
            updated.tasks[i] = task;
          }
        }
        return updated;
      }));
    }));
    return () => { unsubs.forEach((u) => u()); };
  // Subscribe again only when the set of run IDs changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnIdsKey]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const latest = turns[turns.length - 1];
  const waitingHuman = turns.some(isWaitingHuman);
  const busy = turns.some((t) => !isTerminal(t.status) && !isWaitingHuman(t));
  const latestFailed = latest?.status === "FAILED";

  async function handleResume() {
    if (!latest || resuming) return;
    setResuming(true);
    try {
      await resumeWorkflow(latest.run_id);
      await loadThread(runId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not resume");
    } finally {
      setResuming(false);
    }
  }

  async function handleStop() {
    if (stopping || !latest || isTerminal(latest.status)) return;
    setStopping(true);
    try {
      await cancelWorkflow(latest.run_id);
      await loadThread(runId);
    } catch {
      /* already finished */
    } finally {
      setStopping(false);
    }
  }

  return (
    <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
    <div className="workspace-chat animate-fade-in-up" style={{ flex: 1, minWidth: 0 }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, flexWrap: "wrap", marginBottom: 16, alignItems: "center" }}>
        <div>
          <p style={{ margin: 0, fontSize: 12, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: 0.6 }}>Workspace</p>
          <h1 style={{ margin: "4px 0 0", fontSize: 20, fontWeight: 700 }}>Chat</h1>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button className="btn btn-secondary btn-sm" onClick={() => setShowGraph((v) => !v)}>
            {showGraph ? "Hide graph" : "Show graph"}
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => setShowTimeline((v) => !v)}>
            {showTimeline ? "Hide timeline" : "Show timeline"}
          </button>
          {latest && <Link href={`/dashboard/workflows/${latest.run_id}`} className="btn btn-ghost btn-sm">Details</Link>}
          {waitingHuman && (
            <button className="btn btn-primary btn-sm" onClick={() => void handleResume()} disabled={resuming}>
              {resuming ? "Resuming…" : "Resume after challenge"}
            </button>
          )}
          {latest && !isTerminal(latest.status) && (
            <button className="btn btn-ghost btn-sm" onClick={() => void handleStop()} disabled={stopping}>
              {stopping ? "Stopping…" : "Stop"}
            </button>
          )}
          {latestFailed && (
            <button className="btn btn-secondary btn-sm" onClick={() => retryWorkflow(latest.run_id)}>Retry</button>
          )}
        </div>
      </div>

      {error && <p style={{ color: "var(--error)" }}>{error}</p>}

      {latest && latest.run_id && <RunCommandPanel runId={latest.run_id} />}

      {latest && latest.run_id && <EvaluationPanel runId={latest.run_id} runStatus={latest.status} />}
      <BudgetUsagePanel refreshMs={60000} />
      {latest?.run_id && <WorkspaceBrowserSessions runId={latest.run_id} />}
      {latest?.run_id && <RunFeedbackPanel runId={latest.run_id} />}

      {waitingHuman && (
        <div className="glass-panel animate-fade-in" style={{ padding: 14, marginBottom: 12, border: "1px solid rgba(245,158,11,0.4)" }}>
          Complete CAPTCHA, OTP, or MFA in the browser window Sandtable opened. It will not fill those fields. Then click Resume.
        </div>
      )}

      <div
        className="chat-thread hide-scrollbar"
        onScroll={(e) => {
          // Auto-collapse the timeline so messages get full width while reading.
          if (e.currentTarget.scrollTop > 60 && timelineOpen) setTimelineOpen(false);
        }}
      >
        {turns.map((wf) => (
          <div key={wf.run_id} id={`turn-${wf.run_id}`} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <TurnView workflow={wf} />
          </div>
        ))}
        <div ref={endRef} />
      </div>

      {showTimeline && events.length > 0 && (
        <div className="glass-card" style={{ padding: 0, marginBottom: 8, overflow: "hidden" }}>
          <button
            type="button"
            onClick={() => setTimelineOpen((v) => !v)}
            style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              width: "100%", padding: "10px 14px", background: "none", border: "none",
              cursor: "pointer", color: "var(--text-secondary)", fontSize: 13, fontWeight: 600,
            }}
          >
            <span>Execution Timeline · {events.length} events</span>
            <span aria-hidden>{timelineOpen ? "▾" : "▸"}</span>
          </button>
          {timelineOpen && (
            <div style={{ padding: 12, maxHeight: 280, overflow: "auto", borderTop: "1px solid var(--border-primary)" }}>
              <ExecutionTimeline events={events} />
            </div>
          )}
        </div>
      )}

      {showGraph && latest && (
        <div className="glass-card graph-sheet" style={{ padding: 12 }}>
          <WorkflowGraph workflow={latest} />
        </div>
      )}

      <div className="workspace-composer-dock">
        {waitingHuman && (
          <p className="composer-hint" style={{ textAlign: "center", marginBottom: 8 }}>
            Paused for a human security check. Resume when you have finished it.
          </p>
        )}
        {busy && (
          <p className="composer-hint" style={{ textAlign: "center", marginBottom: 8 }}>Sandtable is working on this. You can send the next message when it finishes.</p>
        )}
        <ChatComposer
          compact
          disabled={busy}
          threadId={threadId}
          parentRunId={latest?.run_id || runId}
          onRunCreated={(res) => {
            void (async () => {
              // Immediately fetch the new follow-up run and append it
              try {
                const newRun = await getWorkflow(res.run_id);
                setTurns((prev) => {
                  if (prev.some((t) => t.run_id === res.run_id)) return prev;
                  return [...prev, newRun];
                });
              } catch { /* will appear on next poll */ }
              // Reload the full thread using threadId (not URL runId)
              await new Promise((r) => setTimeout(r, 350));
              await loadThread(threadId);
            })();
          }}
        />
      </div>
    </div>
    {dockLayout.tabs.length > 0 && (
      <aside aria-label="Resource dock" style={{ flexShrink: 0 }}>
        <WorkspacePreviewDock
          principal={principal}
          threadId={threadId}
          layout={dockLayout}
          onLayoutChange={setDockLayout}
          artifactFileUrl={(artifactId, filePath) =>
            `${API_BASE}/artifacts/${artifactId}/files/${filePath}?disposition=preview`}
          onBrowserStop={(sessionId) => {
            void apiFetch(`/browser/sessions/${sessionId}`, { method: "DELETE" });
          }}
        />
      </aside>
    )}
    </div>
  );
}



/**
 * C15 (§5.0 Batch E): the REAL command lifecycle mounted in the workspace —
 * durable state via GET /commands/by-run/{id}, truthful cancellation states,
 * and a cancel control that records intent through the protocol.
 */
function RunCommandPanel({ runId }: { runId: string }) {
  const [command, setCommand] = useState<CommandReceipt | null>(null);
  const [effects, setEffects] = useState<EffectSummary[]>([]);
  const [cancelling, setCancelling] = useState(false);
  const [cancelled, setCancelled] = useState(false);
  // react-hooks v6 (set-state-in-effect): reset when the run changes
  // DURING RENDER (the official adjust-on-prop-change pattern) instead
  // of synchronously inside the effect.
  const [loadedRunId, setLoadedRunId] = useState(runId);
  if (loadedRunId !== runId) {
    setLoadedRunId(runId);
    setCommand(null);
    setEffects([]);
    setCancelled(false);
  }

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const receipt = await getCommandByRun(runId);
        if (alive) setCommand(receipt);
      } catch {
        /* no command for this run (legacy runs) — the queue panel covers it */
      }
      try {
        const fx = await getEffectsByRun(runId);
        if (alive) setEffects(fx.effects || []);
      } catch {
        /* no effects ledger on this deployment */
      }
    };
    void load();
    return () => { alive = false; };
  }, [runId]);

  if (!command && effects.length === 0) return null;
  const phase = command ? cancellationPhaseFrom(command.state) : null;
  const canCancel = command && !command.terminal && command.state !== "CANCEL_REQUESTED";

  return (
    <div style={{ marginBottom: 12, display: "flex", flexDirection: "column", gap: 8 }}>
      {command && <CommandStatusPanel status={{ ...command, state: command.state as never }} />}
      {phase && <CancellationStatus phase={phase} />}
      {command && canCancel && (
        <button
          className="btn btn-ghost btn-sm"
          style={{ alignSelf: "flex-start" }}
          disabled={cancelling}
          onClick={async () => {
            setCancelling(true);
            try {
              await cancelCommand(command.command_id);
              setCancelled(true);
              const fresh = await getCommandByRun(runId);
              if (fresh) setCommand(fresh);
            } finally {
              setCancelling(false);
            }
          }}
        >
          {cancelling || cancelled ? "Cancelling…" : "Cancel command"}
        </button>
      )}
      {effects.filter(fx => !fx.terminal || fx.state === "OUTCOME_UNKNOWN").map(fx => (
        <UncertainOutcomePanel key={fx.effect_id} effect={fx} />
      ))}
    </div>
  );
}
