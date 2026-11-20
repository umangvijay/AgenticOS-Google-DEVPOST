"use client";

import { useEffect, useRef, useState, CSSProperties } from "react";
import { useParams } from "next/navigation";
import { PanelRight, ChevronRight, SquareTerminal } from "lucide-react";
import WorkspaceExecutionCard from "@/components/WorkspaceExecutionCard";
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
import ExecutionTimeline from "@/components/ExecutionTimeline";
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
      background: "var(--bg-secondary)", border: "1px solid var(--border-primary)",
      color: "var(--text-primary)", borderRadius: 20, padding: "12px 16px",
    };
  }
  return {
    alignSelf: "flex-start", maxWidth: "min(720px, 96%)",
    background: "transparent", padding: "8px 0",
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
  return ["COMPLETED", "FAILED", "CANCELLED", "NEEDS_REVIEW", "EXPIRED"].includes(status || "");
}

function isWaitingHuman(workflow: WorkflowRun) {
  return !isTerminal(workflow.status) && (workflow.tasks || []).some((t) => t.status === "WAITING_APPROVAL");
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

function TurnView({ workflow, events, onInspect }: { workflow: WorkflowRun; events: WorkflowEvent[]; onInspect: () => void }) {
  const replies = (workflow.tasks || [])
    .map((t) => ({ task: t, text: t.status === "COMPLETED" ? assistantText(t) : null }))
    .filter((x) => x.text);
  const failed = (workflow.tasks || []).filter((t) => t.status === "FAILED");
  const busy = !isTerminal(workflow.status) && !isWaitingHuman(workflow) && replies.length === 0 && failed.length === 0;


  return (
    <>
      <UserBubble goal={workflow.goal} />
      {(workflow.tasks.length > 0 || events.length > 0) && <details className="turn-activity" onToggle={event => { if (event.currentTarget.open) onInspect(); }}>
        <summary><SquareTerminal size={15} /><span>{isTerminal(workflow.status) ? "View activity" : "Working"}{workflow.tasks.length ? ` · ${workflow.tasks.length} ${workflow.tasks.length === 1 ? "step" : "steps"}` : ""}</span><ChevronRight size={14} className="disclosure-chevron" /></summary>
        <ExecutionTimeline events={events} tasks={workflow.tasks} compact />
      </details>}
      {replies.map(({ task, text }) => (
        <div id={`result-${workflow.run_id}-${task.task_id}`} key={`${workflow.run_id}-out-${task.task_id}`} style={bubbleStyle("agent")}>
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
      {!isTerminal(workflow.status) && (workflow.tasks || []).filter((t) => t.status === "WAITING_APPROVAL").map((t) => (
        <div key={`${workflow.run_id}-wait-${t.task_id}`} style={bubbleStyle("agent")}>
          <div style={{ fontSize: 11, color: "var(--warning, #b45309)", marginBottom: 6 }}>Waiting on you</div>
          <div style={{ fontSize: 14, lineHeight: 1.55, whiteSpace: "pre-wrap" }}>
            {typeof t.output_data?.message === "string"
              ? t.output_data.message
              : "Complete the CAPTCHA, OTP, or MFA in the browser window, then click Resume. Sandtable will not fill those fields."}
          </div>
        </div>
      ))}
      {busy && (
        <div style={bubbleStyle("agent")}>
          <span className="typing-dots" style={{ marginRight: 10 }} aria-hidden>
            <span /><span /><span />
          </span>
          Thinking…
        </div>
      )}
      {isTerminal(workflow.status) && replies.length === 0 && failed.length === 0 && (
        <div role="status" style={bubbleStyle("agent")}>
          {workflow.status === "COMPLETED"
            ? "The run completed without a saved reply. Open Details to inspect its results, or send a follow-up."
            : workflow.status === "CANCELLED" ? "This run was stopped."
            : workflow.status === "NEEDS_REVIEW" ? "This run needs review. Open Details to inspect the result."
            : workflow.status === "EXPIRED" ? "This run expired. You can send your message again."
            : "That run failed before a reply was written. Try sending again."}
        </div>
      )}
    </>
  );
}

export default function WorkspaceChatPage() {
  const params = useParams();
  const auth = useAuth();
  const runId = params.run_id as string;
  return <WorkspaceConversation key={`${auth?.user?.id || "anonymous"}:${runId}`} runId={runId} />;
}

function WorkspaceConversation({ runId }: { runId: string }) {
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
  const [showTimeline, setShowTimeline] = useState(() => typeof window !== "undefined" && window.matchMedia("(min-width: 1100px)").matches);
  const executionTrigger = useRef<HTMLButtonElement>(null);
  function closeExecution() {
    setShowTimeline(false);
    executionTrigger.current?.focus();
  }
  useEffect(() => {
    if (!showTimeline) return;
    const dismiss = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setShowTimeline(false); executionTrigger.current?.focus(); }
    };
    document.addEventListener("keydown", dismiss);
    return () => document.removeEventListener("keydown", dismiss);
  }, [showTimeline]);
  const [subscriptionEpoch, setSubscriptionEpoch] = useState(0);
  const [pendingMessage, setPendingMessage] = useState<string | null>(null);
  const [awayFromBottom, setAwayFromBottom] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const [inspectedRunId, setInspectedRunId] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const followBottom = useRef(true);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  async function loadThread(anchorId: string) {
    let workflows: WorkflowRun[];
    let id: string;
    try {
      const data = await getWorkflowThread(anchorId);
      workflows = data.workflows;
      id = data.thread_id || anchorId;
      if (!workflows?.length) throw new Error("Empty thread");
    } catch {
      const wf = await getWorkflow(anchorId);
      workflows = [wf];
      id = wf.thread_id || wf.run_id;
    }
    if (!alive.current) return;
    // Preserve accepted follow-ups if a lagging thread listing omits them.
    setTurns(prev => [...workflows, ...prev.filter(w => !workflows.some(f => f.run_id === w.run_id))]);
    setThreadId(id);
    setError("");
  }

  useEffect(() => {
    void loadThread(runId).catch(e => {
      if (alive.current) setError(e instanceof Error ? e.message : "Failed to load run");
    });
    // This keyed component owns one conversation for its entire lifetime.
  }, [runId]);

  // One shared stream per active turn, plus the latest turn for replayed history.
  // Snapshots, including the final outputs, come from the shared reconciler.
  const subscribedIds = [...new Set([...turns.filter((t, i) => !isTerminal(t.status) || i === turns.length - 1)
    .map(t => t.run_id), inspectedRunId].filter(Boolean))].join("|");
  useEffect(() => {
    let active = true;
    const unsubs = subscribedIds.split("|").filter(Boolean).map(id => subscribeRun(id, ({ event, run }) => {
      if (!active) return;
      if (event) setEvents(prev => {
        if (prev.some(e => e.event_id === event.event_id)) return prev;
        return [...prev, event].slice(-500);
      });
      if (run) setTurns(prev => prev.map(w => w.run_id === run.run_id ? run : w));
    }));
    return () => { active = false; unsubs.forEach(stop => stop()); };
  }, [subscribedIds, subscriptionEpoch]);

  useEffect(() => {
    const scroller = scrollRef.current;
    if (scroller && followBottom.current) scroller.scrollTop = scroller.scrollHeight;
  }, [turns, pendingMessage]);

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
    <div className="workspace-shell">
    <div className="workspace-chat">
      <div className="workspace-conversation-toolbar">
        <span className="workspace-conversation-title" title={turns[0]?.goal}>{turns[0]?.goal || "Conversation"}</span>
        <div className="workspace-conversation-actions">
          <button ref={executionTrigger} className="btn btn-ghost btn-sm" aria-label="Execution" title="Toggle execution card" aria-expanded={showTimeline} aria-controls="execution-panel" onClick={() => setShowTimeline(value => !value)}>
            <PanelRight size={18} /><span>Execution</span>
          </button>
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
            <button className="btn btn-secondary btn-sm" onClick={() => void retryWorkflow(latest.run_id).then(async () => { await loadThread(runId); setSubscriptionEpoch(v => v + 1); }).catch(e => setError(String(e)))}>Retry</button>
          )}
        </div>
      </div>

      {error && <p style={{ color: "var(--error)" }}>{error}</p>}

      {latest?.run_id && <WorkspaceBrowserSessions runId={latest.run_id} />}
      {waitingHuman && (
        <div className="glass-panel animate-fade-in" style={{ padding: 14, marginBottom: 12, border: "1px solid rgba(245,158,11,0.4)" }}>
          Complete CAPTCHA, OTP, or MFA in the browser window Sandtable opened. It will not fill those fields. Then click Resume.
        </div>
      )}

      <div
        ref={scrollRef}
        className="chat-thread"
        role="region"
        aria-label="Conversation"
        tabIndex={0}
        onScroll={e => {
          const el = e.currentTarget;
          followBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
          setAwayFromBottom(!followBottom.current);
        }}
      >
        {turns.map((wf) => (
          <div key={wf.run_id} id={`turn-${wf.run_id}`} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <TurnView workflow={wf} events={events.filter(event => event.run_id === wf.run_id)} onInspect={() => setInspectedRunId(wf.run_id)} />
          </div>
        ))}
        {pendingMessage && <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <UserBubble goal={pendingMessage} />
          <span role="status" className="composer-hint">Sending…</span>
        </div>}
        {!turns.length && !error && <p role="status">Loading conversation…</p>}
      </div>

      {awayFromBottom && <button className="btn btn-secondary btn-sm chat-jump" onClick={() => {
        followBottom.current = true;
        setAwayFromBottom(false);
        if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      }}>Jump to latest</button>}

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
          onSubmitting={message => {
            if (!alive.current) return;
            if (message) followBottom.current = true;
            setPendingMessage(message);
          }}
          onRunCreated={(res) => {
            if (!alive.current) return;
            followBottom.current = true;
            setPendingMessage(null);
            setTurns(prev => prev.some(t => t.run_id === res.run_id) ? prev : [...prev, {
              run_id: res.run_id, workflow_id: res.workflow_id, user_id: principal,
              goal: res.goal, status: res.status, tasks: [], created_at: new Date().toISOString(),
              thread_id: res.thread_id || threadId,
            }]);
          }}
        />
      </div>
    </div>
    <aside id="execution-panel" hidden={!showTimeline} aria-label="Execution details" className="workspace-execution">
      <WorkspaceExecutionCard turns={turns} events={events} onClose={closeExecution} onInspectRun={setInspectedRunId}>
        {run => <RunInspection key={run.run_id} run={run} />}
      </WorkspaceExecutionCard>
    </aside>
    {dockLayout.tabs.length > 0 && (
      <aside aria-label="Resource dock" className="workspace-resource-dock">
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



function RunInspection({ run }: { run: WorkflowRun }) {
  const [showGraph, setShowGraph] = useState(false);
  const [showUsage, setShowUsage] = useState(false);
  const [showFeedback, setShowFeedback] = useState(false);
  return <div className="execution-diagnostics">
    <RunCommandPanel runId={run.run_id} />
    <EvaluationPanel runId={run.run_id} runStatus={run.status} />
    <button className="execution-text-link" aria-expanded={showGraph} onClick={() => setShowGraph(value => !value)}>{showGraph ? "Hide graph" : "Show graph"}</button>
    {showGraph && <div style={{ height: 260 }}><WorkflowGraph workflow={run} /></div>}
    <button className="execution-text-link" aria-expanded={showUsage} onClick={() => setShowUsage(value => !value)}>Usage details</button>
    {showUsage && <BudgetUsagePanel refreshMs={60000} />}
    <button className="execution-text-link" aria-expanded={showFeedback} onClick={() => setShowFeedback(value => !value)}>Feedback</button>
    {showFeedback && <RunFeedbackPanel runId={run.run_id} />}
  </div>;
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
