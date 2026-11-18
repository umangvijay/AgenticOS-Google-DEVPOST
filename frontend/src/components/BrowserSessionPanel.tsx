"use client";

/**
 * Browser session panel (Phase 20 §20.16): what the agent's browser is
 * doing — the CURRENT url, who controls it, and the action evidence —
 * with viewer/takeover state server-enforced (the panel only renders
 * what the backend authorizes; a 403 renders as an explicit message,
 * never as control).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";

export interface BrowserFrame {
  session_id: string;
  url: string | null;
  state: string;
  controller: string;
  controller_epoch: string;
  freshness: number;
  screenshot_sha256: string;
  frame_base64: string;
}

export interface BrowserSessionState {
  session_id: string;
  owner_user_id: string;
  controller: string;
  controller_epoch: string;
  url: string | null;
  role: "controller" | "viewer" | "observer";
  history: Array<{
    action: string;
    target: string;
    status: string;
    executed: boolean;
    detail: string;
    error: string | null;
  }>;
}

export function BrowserSessionPanel({ sessionId }: { sessionId: string }) {
  const [state, setState] = useState<BrowserSessionState | null>(null);
  const [frame, setFrame] = useState<BrowserFrame | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generationRef = useRef(0);
  const frameRef = useRef(0);
  const [input, setInput] = useState("");
  const [frameError, setFrameError] = useState("");
  const invalidatePending = useCallback(() => {
    generationRef.current++;
    frameRef.current++;
  }, []);

  // THE LIVE VIEW (§21.13): poll the REAL screenshot of the agent's
  // page — same session, authenticated, server-enforced.
  const loadFrame = useCallback(() => {
    const generation = ++frameRef.current;
    apiFetch<BrowserFrame>(`/browser/sessions/${sessionId}/frame`)
      .then((res) => {
        if (generation !== frameRef.current) return;
        setFrame(res);
        setFrameError("");
      })
      .catch(() => {
        if (generation === frameRef.current) {
          setFrame(null);
          setFrameError("Live view disconnected. Reconnecting…");
        }
      });
  }, [sessionId]);

  const load = useCallback(() => {
    const generation = ++generationRef.current;
    apiFetch<BrowserSessionState>(`/browser/sessions/${sessionId}`)
      .then((res) => {
        if (generation !== generationRef.current) return;
        setState(res);
      })
      .catch((err: unknown) => {
        if (generation !== generationRef.current) return;
        setError(err instanceof Error ? err.message : "Could not load the browser session");
      });
  }, [sessionId]);
  useEffect(() => {
    void load();
    const id = setInterval(() => { load(); loadFrame(); }, 2000);
    return () => {
      clearInterval(id);
      invalidatePending();
    };
  }, [load, loadFrame, invalidatePending]);

  const act = async (path: string) => {
    setBusy(true);
    setError("");
    try {
      await apiFetch(`/browser/sessions/${sessionId}${path}`, {
        method: "POST",
      });
      load();
      loadFrame();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "The action failed";
      setError(/403|not control/i.test(message)
        ? "You do not control this session — ownership is enforced server-side."
        : `The action failed: ${message}`);
    } finally {
      setBusy(false);
    }
  };

  const execute = async (action: string, target = "", text = "") => {
    if (!state || state.role !== "controller" || busy) return;
    setBusy(true); setError("");
    try {
      const result = await apiFetch<{ status: string; error?: string }>(`/browser/sessions/${sessionId}/execute`, {
        method: "POST", body: JSON.stringify({ action, target, text,
          controller_epoch: state.controller_epoch }),
      });
      if (result.status !== "executed") throw new Error(result.error || "Browser action failed");
      if (action === "insert_text") setInput("");
      load(); loadFrame();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Browser action failed");
    } finally { setBusy(false); }
  };

  if (error) {
    return (
      <section className="glass-card" style={{ padding: 24 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600 }}>Browser session</h2>
        <p role="alert" style={{ color: "var(--error)", fontSize: 13 }}>
          {error}
        </p>
        <button className="btn btn-secondary btn-sm" onClick={() => { setError(""); load(); loadFrame(); }}>
          Retry
        </button>
      </section>
    );
  }
  if (!state) return <div className="spinner" />;

  const isController = state.role === "controller";
  const isPaused = frame?.state === "paused";
  return (
    <section className="glass-card" style={{ padding: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>Browser session</h2>
        <span className={`badge ${isController ? "badge-success" : "badge-warn"}`}>
          {state.role}{isPaused ? " · paused" : ""}
        </span>
      </div>
      <p style={{ fontSize: 12.5, color: "var(--text-tertiary)", marginTop: 6 }}>
        {state.url ? `On: ${state.url}` : "Not navigating yet"}
        {" · controller "}{state.controller}
        {frame ? ` · view updated ${new Date(frame.freshness * 1000).toLocaleTimeString()}` : ""}
      </p>
      {/* THE ACTUAL VIEW — a real screenshot of the agent's page. */}
      {frameError && <p role="status">{frameError}</p>}
      {frame ? (
        <button type="button" disabled={!isController || busy || isPaused}
          aria-label="Interact with browser view"
          style={{ display: "block", padding: 0, width: "100%", background: "transparent", border: 0 }}
          onClick={event => {
            const img = event.currentTarget.querySelector("img");
            if (!img) return;
            const rect = img.getBoundingClientRect();
            if (!rect.width || !rect.height || event.detail === 0) return;
            void execute("click_at", JSON.stringify({
              x: (event.clientX - rect.left) * img.naturalWidth / rect.width,
              y: (event.clientY - rect.top) * img.naturalHeight / rect.height,
            }));
          }}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={`data:image/png;base64,${frame.frame_base64}`}
             alt="Live view of the agent's browser session"
             style={{ display: "block", width: "100%", borderRadius: 8, border: "1px solid var(--border-primary)" }} />
        </button>
      ) : (
        <div style={{ padding: 16, border: "1px dashed var(--border-primary)", borderRadius: 8, fontSize: 12.5, color: "var(--text-tertiary)" }}>
          Live view not available yet — the panel shows the actual session as soon as frames arrive.
        </div>
      )}
      <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
        {state.role === "observer" && (
          <button className="btn btn-primary btn-sm" disabled={busy}
                  onClick={() => void act("/takeover")}>
            Take control
          </button>
        )}
        {state.role !== "viewer" && !isPaused && (
          <button className="btn btn-secondary btn-sm" disabled={busy}
                  onClick={() => void act("/pause")}>
            Pause agent
          </button>
        )}
        {state.role !== "viewer" && isPaused && (
          <button className="btn btn-secondary btn-sm" disabled={busy}
                  onClick={() => void act("/resume")}>
            Resume agent
          </button>
        )}
        {isController && (
          <button className="btn btn-ghost btn-sm" disabled={busy}
                  onClick={() => void act("/return-control")}>
            Return control to agent
          </button>
        )}
      </div>
      {isController && <form onSubmit={event => { event.preventDefault(); void execute("insert_text", "", input); }}
        style={{ display: "grid", gap: 8, marginTop: 12 }}>
        <label>Text to enter in the focused field
          <input value={input} maxLength={4000} onChange={event => setInput(event.target.value)}
            autoComplete="off" style={{ display: "block", width: "100%" }} />
        </label>
        <button className="btn btn-secondary btn-sm" type="submit" disabled={busy || isPaused || !input}>Enter text</button>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {["Tab", "Shift+Tab", "Enter", "Escape", "Backspace", "ArrowDown", "ArrowUp"].map(key =>
            <button key={key} type="button" className="btn btn-ghost btn-sm" disabled={busy || isPaused}
              onClick={() => void execute("press_key", key)}>{key}</button>)}
        </div>
      </form>}
      {state.role === "viewer" && (
        <p style={{ fontSize: 12, color: "var(--text-tertiary)" }}>
          You are viewing read-only. Control changes require a takeover.
        </p>
      )}
      <h3 style={{ fontSize: 13, fontWeight: 600, margin: "14px 0 6px" }}>
        Action evidence
      </h3>
      <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: 13 }}>
        {state.history.length === 0 && (
          <li style={{ color: "var(--text-tertiary)" }}>No actions yet.</li>
        )}
        {state.history.map((h, i) => (
          <li key={i} style={{ padding: "4px 0", borderTop: "1px solid var(--border-primary)" }}>
            <span className={`badge ${h.status === "executed" ? "badge-success" : "badge-error"}`}>
              {h.status}
            </span>{" "}
            <span style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{h.action}</span>{" "}
            {h.target && (
              <span style={{ color: "var(--text-tertiary)", fontFamily: "var(--font-mono)", fontSize: 12 }}>
                {h.target}
              </span>
            )}
            {h.error && (
              <div style={{ color: "var(--error)", fontSize: 12 }}>Error: {h.error}</div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
