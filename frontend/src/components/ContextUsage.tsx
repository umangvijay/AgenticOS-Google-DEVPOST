"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { getContextUsage, ContextUsage } from "@/lib/api";

const COLORS: Record<string, string> = {
  system: "#8a8a84",
  tools: "#8b6cc9",
  mcp: "#b8a0e8",
  subagents: "#7eb6d9",
  conversation: "#6b4fa0",
  memory: "#d8858f",
};

function formatTokens(n: number) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}K`;
  return String(n);
}

export default function ContextUsageButton() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ContextUsage | null>(null);
  const [error, setError] = useState("");
  const [noKey, setNoKey] = useState(false);
  const [pos, setPos] = useState({ top: 64, right: 16 });
  const ref = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const place = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setPos({ top: r.bottom + 8, right: Math.max(12, window.innerWidth - r.right) });
  }, []);

  const load = useCallback(async () => {
    try {
      setError("");
      setData(await getContextUsage());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load usage");
    }
  }, []);

  useEffect(() => {
    // load() commits fetched data after the API promise resolves.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
    (async () => {
      try {
        const res = await fetch("/api/v1/health");
        const h = await res.json();
        const status = h?.llm_status?.status;
        setNoKey(status === "missing" || h?.llm === "unset");
      } catch { /* health unreachable — keep estimate view */ }
    })();
    const id = setInterval(load, 20000);
    return () => clearInterval(id);
  }, [load]);

  useEffect(() => {
    if (!open) return;
    // Opening the panel triggers asynchronous data refresh and geometry sync.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
    place();
    const onClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (ref.current?.contains(t) || panelRef.current?.contains(t)) return;
      setOpen(false);
    };
    window.addEventListener("resize", place);
    document.addEventListener("mousedown", onClick);
    return () => {
      window.removeEventListener("resize", place);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open, load, place]);

  const pct = data?.percent ?? 0;
  const used = data?.context_estimate ?? 0;
  const windowTokens = data?.window_tokens ?? 256000;
  const remaining = data?.remaining_tokens ?? Math.max(0, windowTokens - used);

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        className="btn btn-ghost btn-sm context-usage-trigger"
        onClick={() => { place(); setOpen((v) => !v); }}
        aria-label={data
          ? `Estimated context ${used.toLocaleString()} of ${windowTokens.toLocaleString()} tokens`
          : error ? "Context estimate unavailable" : "Loading context estimate"}
        title={data
          ? `Estimated context ${used.toLocaleString()} of ${windowTokens.toLocaleString()} tokens`
          : error ? "Context estimate unavailable" : "Loading context estimate"}
        style={{ gap: 8, fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}
      >
        <span
          style={{
            width: 52, height: 6, borderRadius: 99, background: "var(--bg-tertiary)",
            overflow: "hidden", display: "inline-block", border: "1px solid var(--border-primary)",
          }}
        >
          <span
            style={{
              display: "block", height: "100%", width: `${pct > 0 ? Math.max(0.8, Math.min(pct, 100)) : 0}%`,
              background: pct > 85
                ? "var(--error)"
                : "linear-gradient(90deg, var(--accent-purple), var(--accent))",
              transition: "width 0.6s cubic-bezier(0.22, 1, 0.36, 1)",
            }}
          />
        </span>
        <span className="context-usage-label" aria-live="polite">
          {noKey ? (
            <span style={{ color: "var(--warm)" }}>No API key · add one in Settings</span>
          ) : data ? (
            <>Context ~{formatTokens(used)} / {formatTokens(windowTokens)}
              <span style={{ color: "var(--text-tertiary)", marginLeft: 6 }}>
                ({pct > 0 && pct < 1 ? pct.toFixed(1) : Math.round(pct)}%)
              </span>
            </>
          ) : error ? "Context unavailable" : "Context loading"}
        </span>
      </button>

      {open && typeof document !== "undefined" && createPortal(
        <div ref={panelRef} className="glass-card context-usage-panel animate-fade-in-up" style={{
          position: "fixed", top: pos.top, right: pos.right, width: 400,
          maxWidth: "min(400px, calc(100vw - 24px))", maxHeight: "min(70vh, 560px)",
          overflowY: "auto", zIndex: 400, padding: 18,
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
            <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>Context estimate</h3>
            <button className="btn btn-ghost" style={{ padding: 4, minWidth: 0 }} onClick={() => setOpen(false)} aria-label="Close">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>

          {error && <p style={{ color: "var(--error)", fontSize: 13 }}>{error}</p>}
          {noKey && (
            <p style={{
              margin: "0 0 12px", padding: "10px 12px", borderRadius: 10, fontSize: 12.5, lineHeight: 1.5,
              border: "1px solid color-mix(in srgb, var(--warm) 45%, transparent)",
              background: "color-mix(in srgb, var(--warm) 8%, transparent)",
              color: "var(--text-secondary)",
            }}>
              No model API key configured. Token usage is 0 — the figures below
              are size-based estimates of what a run <em>would</em> occupy, not
              anything spent. Add a key in Settings to start real usage.
            </p>
          )}
          {!data && !error && <div className="skeleton" style={{ height: 80 }} />}

          {data && (
            <>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 6, fontFamily: "var(--font-mono)" }}>
                <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{used.toLocaleString()} estimated</span>
                <span style={{ color: "var(--text-secondary)" }}>{remaining.toLocaleString()} left</span>
              </div>
              <div style={{
                height: 10, borderRadius: 99, overflow: "hidden", display: "flex",
                background: "var(--bg-tertiary)", border: "1px solid var(--border-primary)", marginBottom: 8,
              }}>
                {data.categories.filter((c) => c.tokens > 0).map((c) => (
                  <span
                    key={c.id}
                    title={`${c.label}: ${c.tokens.toLocaleString()}`}
                    style={{
                      width: `${Math.max(1.5, (c.tokens / Math.max(used, 1)) * 100)}%`,
                      background: COLORS[c.id] || "var(--accent)",
                      height: "100%",
                    }}
                  />
                ))}
              </div>
              <p style={{ margin: "0 0 14px", fontSize: 12, color: "var(--text-tertiary)", fontFamily: "var(--font-mono)" }}>
                Current prompt context · about {used.toLocaleString()} / {windowTokens.toLocaleString()} tokens ({pct}%)
              </p>
              <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 8 }}>
                {data.categories.map((c) => (
                  <li key={c.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: 13 }}>
                    <span style={{ display: "flex", alignItems: "center", gap: 10, color: "var(--text-primary)" }}>
                      <span style={{ width: 10, height: 10, borderRadius: 3, background: COLORS[c.id] || "var(--accent)", flexShrink: 0 }} />
                      {c.label}
                    </span>
                    <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--text-secondary)" }}>
                      {c.tokens.toLocaleString()}
                    </span>
                  </li>
                ))}
              </ul>
              <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--border-primary)", fontSize: 12, color: "var(--text-secondary)" }}>
                <p style={{ margin: 0, fontSize: 11, color: "var(--text-tertiary)" }}>
                  {data.model} · {data.tool_count} tools · context estimate from text size (~4 characters/token).
                  {" "}This is what a request may occupy — <strong>not</strong> spending. Measured usage lives in
                  the Token budget panel.
                </p>
              </div>
            </>
          )}
        </div>,
        document.body
      )}
    </div>
  );
}
