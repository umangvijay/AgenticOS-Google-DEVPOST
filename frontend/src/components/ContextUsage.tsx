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

export default function ContextUsageButton() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ContextUsage | null>(null);
  const [error, setError] = useState("");
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
      const usage = await getContextUsage();
      setData(usage);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load usage");
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    const id = setInterval(load, 20000);
    const onClick = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!ref.current?.contains(t) && !panelRef.current?.contains(t)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("resize", place);
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      clearInterval(id);
      window.removeEventListener("resize", place);
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, load, place]);

  const used = data?.context_estimate ?? 0;
  const windowTokens = data?.window_tokens ?? 256000;
  const remaining = data?.remaining_tokens ?? Math.max(0, windowTokens - used);

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        className="btn btn-ghost btn-sm context-usage-trigger"
        onClick={() => { place(); if (!open) void load(); setOpen((v) => !v); }}
        aria-label="Context usage"
        aria-expanded={open}
        title="Context usage details"
        style={{ gap: 8, fontSize: 12, fontWeight: 600, color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}
      >
        Context
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
                Current prompt context · about {used.toLocaleString()} / {windowTokens.toLocaleString()} tokens ({data.percent}%)
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
