"use client";

import { useState, useEffect, useCallback } from "react";
import { connectorStateBadges } from "@/lib/task-states";
import { listIntegrations, Integration } from "@/lib/api";
import Link from "next/link";

export default function IntegrationsPage() {
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(() => listIntegrations().then(data => {
    setIntegrations(Array.isArray(data.integrations) ? data.integrations : []);
  }).catch((err: unknown) => {
    setError(err instanceof Error ? err.message : "Failed to load integrations");
  }).finally(() => setLoading(false)), []);

  useEffect(() => { void load(); }, [load]);

  function healthLabel(mcp: Integration) {
    if (!mcp.is_enabled) return "Disabled";
    if (mcp.circuit_breaker?.state === "OPEN") return "Circuit open";
    if (mcp.health?.toUpperCase() === "UNHEALTHY") return "Check failed";
    if (mcp.health?.toUpperCase() === "HEALTHY") return "Last check passed";
    return "Not verified";
  }

  return (
    <div className="animate-fade-in" data-scroll-reveal>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 32 }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 700 }}>Integrations & MCPs</h1>
          <p style={{ color: "var(--text-secondary)" }}>Manage the tools your agents can use.</p>
        </div>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <button className="btn btn-secondary" onClick={() => { setLoading(true); setError(""); void load(); }} disabled={loading}>
            {loading ? "Checking…" : "Refresh status"}
          </button>
          <Link href="/dashboard/integrations/create" className="btn btn-primary">Build New Integration</Link>
        </div>
      </div>

      <div className="glass-card" style={{ padding: 20, marginBottom: 24 }}>
        <p style={{ margin: 0 }}>An enabled connector is available to agents. Its last check shows whether it was tested successfully; it does not guarantee every operation will work.</p>
        <p style={{ color: "var(--text-secondary)", marginBottom: 0 }}>Open a connector to run a safe test, inspect its tools, or update credentials. Your agent can also build a missing connector during a run.</p>
      </div>
      {error && (
        <div style={{
          padding: "12px 16px", marginBottom: 20,
          background: "var(--error-subtle)", borderRadius: "var(--radius-md)",
          color: "var(--error)", fontSize: 14,
        }}>
          {error}
        </div>
      )}

      {loading ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 20 }}>
          <div className="skeleton" style={{ height: 160 }} />
          <div className="skeleton" style={{ height: 160 }} />
          <div className="skeleton" style={{ height: 160 }} />
        </div>
      ) : integrations.length > 0 ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 20 }}>
          {integrations.map(mcp => (
            <Link
              key={mcp.mcp_id}
              href={`/dashboard/integrations/${mcp.mcp_id}`}
              className="glass-card"
              style={{ padding: 20, textDecoration: "none", display: "flex", flexDirection: "column" }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
                <h3 style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)", margin: 0 }}>
                  {mcp.name || mcp.mcp_id}
                </h3>
                <span className={`badge ${
                  mcp.circuit_breaker?.state === "OPEN" ? "badge-error" : 
                  mcp.health?.toUpperCase() === "UNHEALTHY" ? "badge-error" :
                  mcp.is_enabled && mcp.health?.toUpperCase() === "HEALTHY" ? "badge-success" : "badge-neutral"
                }`}>
                  {healthLabel(mcp)}
                </span>
              </div>
              
              <p style={{ fontSize: 14, color: "var(--text-secondary)", marginBottom: 20, flex: 1 }}>
                {mcp.description || "No description provided."}
              </p>
              
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
                {connectorStateBadges(mcp).map(b => (
                  <span key={b.label} className={`badge ${b.badge}`} style={{ fontSize: 10 }}>{b.label}</span>
                ))}
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", borderTop: "1px solid var(--border-primary)", paddingTop: 16 }}>
                <div style={{ fontSize: 13, color: "var(--text-tertiary)" }}>
                  {mcp.validated_tool_count ?? 0} / {mcp.tool_count ?? 0} operations tested
                </div>
                <div style={{ fontSize: 12, fontWeight: 600, color: mcp.trust_tier === 'verified' ? "var(--success)" : "var(--warning)" }}>
                  {mcp.is_enabled ? "Enabled · tested operations only" : "Disabled"}
                </div>
              </div>
            </Link>
          ))}
        </div>
      ) : (
        <div className="glass-card empty-state">
          <svg width="48" height="48" fill="none" stroke="currentColor" strokeWidth="1" viewBox="0 0 24 24">
            <path d="M12 4v16m8-8H4" />
          </svg>
          <h3 style={{ fontSize: 16, fontWeight: 500, color: "var(--text-primary)", marginBottom: 8 }}>No integrations</h3>
          <p style={{ marginBottom: 24 }}>Connect Sandtable to external services by building an MCP.</p>
          <Link href="/dashboard/integrations/create" className="btn btn-primary">Build Integration</Link>
        </div>
      )}
    </div>
  );
}
