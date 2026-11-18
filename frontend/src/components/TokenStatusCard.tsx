"use client";

import { useEffect, useState } from "react";
import { API_BASE, getBudgetSnapshot } from "@/lib/api";
import BudgetUsagePanel from "@/components/BudgetUsagePanel";

/**
 * Live "Model & tokens" status for the Settings page:
 * - Which LLM provider is active and whose credential it is using (BYOK)
 * - The authoritative daily token budget (Phase 2 §2.16): settled
 *   consumption, active reservations, and available allowance — rendered by
 *   BudgetUsagePanel. Context estimates are a different concept and are NOT
 *   shown as spending.
 */
export default function TokenStatusCard() {
  const [provider, setProvider] = useState<{ llm: string; status: string; detail: string } | null>(null);
  // §27.D5: spending comes from the ledger, independent of credential
  // status — a missing key is not zero consumption.
  const [ledger, setLedger] = useState<{ settledTokens?: number; period?: string } | null>(null);
  useEffect(() => {
    let alive = true;
    getBudgetSnapshot()
      .then((snap) => {
        if (!alive) return;
        const s = snap as unknown as { consumed?: number | null; period?: string };
        setLedger({
          settledTokens: typeof s?.consumed === "number" ? s.consumed : undefined,
          period: s?.period,
        });
      })
      .catch(() => {
        if (alive) setLedger(null);  // unknown stays unknown
      });
    return () => { alive = false; };
  }, []);
  const hasKey = provider ? !["unset", "missing"].includes(provider.status) : false;

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const res = await fetch(`${API_BASE.replace(/\/api\/v1\/?$/, "")}/health`);
        const h = await res.json();
        if (!active) return;
        setProvider({
          llm: h.llm || "unset",
          status: h.llm_status?.status || "unknown",
          detail: h.llm_status?.detail || "",
        });
      } catch { /* health unreachable — leave status quiet */ }
    })();
    return () => { active = false; };
  }, []);

  const providerLabel: Record<string, string> = {
    gemini: "Gemini (AI Studio key)", vertex: "Google Vertex AI",
    groq: "Groq", huggingface: "Hugging Face", unset: "Not configured",
  };

  return (
    <div className="glass-card panel-3d" style={{ padding: 24, marginBottom: 24 }}>
      <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 14, borderBottom: "1px solid var(--border-primary)", paddingBottom: 12 }}>
        Model & tokens
      </h2>

      {!hasKey && provider && (
        <div style={{
          padding: "10px 14px", marginBottom: 14, borderRadius: "var(--radius-md)",
          border: "1px solid color-mix(in srgb, var(--warm) 45%, transparent)",
          background: "color-mix(in srgb, var(--warm) 8%, transparent)",
          fontSize: 12.5, color: "var(--text-secondary)", lineHeight: 1.5,
        }}>
          {ledger && typeof ledger.settledTokens === "number" && ledger.settledTokens > 0
            ? `No model API key is configured right now — your account still has ${ledger.settledTokens.toLocaleString()} settled tokens on the ledger (${ledger.period || "current period"}). Historical usage is preserved; adding a key below resumes billing against the same ledger.`
            : "No model API key is configured yet. Historical spending, if any, is shown from the usage ledger — a missing key never means zero consumption. The context figures elsewhere are estimates from text size (~4 characters per token), not billing data."}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 16, marginBottom: 16 }}>
        <div style={{ padding: 14, borderRadius: "var(--radius-md)", background: "var(--bg-tertiary)", border: "1px solid var(--border-primary)" }}>
          <div style={{ fontSize: 12, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>Active provider</div>
          <div style={{ fontSize: 15, fontWeight: 600 }}>{providerLabel[provider?.llm || ""] || (provider?.llm || "…")}</div>
          <div style={{ fontSize: 12.5, color: provider?.status === "catalog_ok" || provider?.status === "vertex" ? "var(--success, #22c55e)" : "var(--text-tertiary)", marginTop: 4 }}>
            {provider?.status === "catalog_ok" ? "Your key is working"
              : provider?.status === "vertex" ? "Vertex credentials working"
              : provider?.status === "missing" ? "No credential on the server — add your own key below"
              : provider?.detail?.slice(0, 80) || "…"}
          </div>
        </div>
      </div>

      {!hasKey && (
        <p style={{ fontSize: 11.5, color: "var(--text-tertiary)", margin: "0 0 10px" }}>
          The budget below comes from the durable usage ledger, not from
          your key status — past spending stays visible whether or not a
          provider key is configured right now.
        </p>
      )}

      <BudgetUsagePanel />
    </div>
  );
}
