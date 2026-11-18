"use client";

import { useState, useEffect, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import { getIntegration, listConnectorRevisions, checkpointConnector, restoreConnectorRevision, ConnectorRevision, enableIntegration, disableIntegration, deleteIntegration, testIntegration, attachIntegrationKey, downloadIntegrationServer, eligibilitySummary, eligibilityReasonLabel } from "@/lib/api";

export default function IntegrationDetailPage() {
  const params = useParams();
  const router = useRouter();
  const mcpId = params.mcp_id as string;

  const [mcp, setMcp] = useState<Awaited<ReturnType<typeof getIntegration>> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [testStatus, setTestStatus] = useState("");
  const [testing, setTesting] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [keyBusy, setKeyBusy] = useState(false);
  const [revisions, setRevisions] = useState<ConnectorRevision[]>([]);
  const [revisionBusy, setRevisionBusy] = useState(false);
  const [revisionError, setRevisionError] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await getIntegration(mcpId);
      setMcp(data);
      try {
        const history = await listConnectorRevisions(mcpId);
        setRevisions(history.revisions);
        setRevisionError("");
      } catch {
        setRevisionError("Could not load version history. Refresh to retry.");
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load integration");
    } finally {
      setLoading(false);
    }
  }, [mcpId]);

  useEffect(() => {
    // This starts an async request; state updates happen after its response.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  const handleToggle = async () => {
    if (!mcp) return;
    try {
      if (mcp.is_enabled) {
        await disableIntegration(mcpId);
      } else {
        await enableIntegration(mcpId);
      }
      await load();
    } catch (err) {
      alert("Failed to toggle state: " + (err instanceof Error ? err.message : String(err)));
    }
  };

  const handleTest = async () => {
    setTesting(true);
    try {
      const result = await testIntegration(mcpId);
      setTestStatus(result.status === "untested" ? "No safe probe was available. This integration has not been verified." : `Live test: ${result.status}`);
      await load();
    } catch (err) {
      alert("Test failed: " + (err instanceof Error ? err.message : String(err)));
    } finally {
      setTesting(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm("Are you sure you want to delete this integration? This will break workflows depending on it.")) return;
    try {
      await deleteIntegration(mcpId);
      router.push("/dashboard/integrations");
    } catch (err) {
      alert("Delete failed: " + (err instanceof Error ? err.message : String(err)));
    }
  };

  if (loading) return (
    <div style={{ display: "flex", justifyContent: "center", padding: 100 }}>
      <div className="spinner" />
    </div>
  );

  if (error || !mcp) return (
    <div className="empty-state">
      <div style={{ color: "var(--error)", marginBottom: 16 }}>{error || "Integration not found"}</div>
      <button className="btn btn-secondary" onClick={() => router.push("/dashboard/integrations")}>Back</button>
    </div>
  );

  return (
    <div className="animate-fade-in" data-scroll-reveal style={{ maxWidth: 900, margin: "0 auto" }}>
      
      {/* Header */}
      <div className="glass-card" style={{ padding: 32, marginBottom: 24, display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
            <h1 style={{ fontSize: 24, fontWeight: 700, margin: 0 }}>{mcp.name}</h1>
            <span className={`badge ${
              mcp.circuit_breaker?.state === "OPEN" ? "badge-error" : 
              !mcp.is_enabled ? "badge-neutral" : "badge-success"
            }`}>
              {mcp.circuit_breaker?.state === "OPEN" ? "Outage" : mcp.is_enabled ? "Active" : "Disabled"}
            </span>
          </div>
          <p style={{ color: "var(--text-secondary)", fontSize: 15 }}>{mcp.description || "No description provided."}</p>
        </div>
        
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <button className="btn btn-secondary" onClick={() => downloadIntegrationServer(mcpId).catch(err => setTestStatus(err.message))}>Download MCP server</button>
          <button className="btn btn-secondary" onClick={handleTest} disabled={testing}>
            {testing ? <span className="spinner" style={{ width: 14, height: 14 }}/> : "Run Test"}
          </button>
          <button className={mcp.is_enabled ? "btn btn-secondary" : "btn btn-primary"} onClick={handleToggle}>
            {mcp.is_enabled ? "Disable" : "Enable"}
          </button>
        </div>
      </div>

      {testStatus && <p role="status">{testStatus}</p>}
      <section className="glass-card" style={{ padding: 24, marginBottom: 24 }} aria-label="Connector version history">
        <h2 style={{ fontSize: 18, marginBottom: 8 }}>Version history</h2>
        <p style={{ color: "var(--text-secondary)", marginBottom: 16 }}>Save a definition before making changes. Restore creates a disabled copy; reconnect credentials and test it before use.</p>
        {revisionError && <p role="alert">{revisionError}</p>}
        <button className="btn btn-secondary" disabled={revisionBusy} onClick={async () => {
          setRevisionBusy(true);
          try { await checkpointConnector(mcpId); await load(); }
          catch (err) { setRevisionError(err instanceof Error ? err.message : "Could not save version"); }
          finally { setRevisionBusy(false); }
        }}>Save current version</button>
        {revisions.length === 0 && <p style={{ marginTop: 16 }}>No saved versions yet.</p>}
        {revisions.map(revision => <div key={revision.revision_id} style={{ display: "flex", flexWrap: "wrap", gap: 16, alignItems: "center", justifyContent: "space-between", padding: "16px 0", borderBottom: "1px solid var(--border-primary)" }}>
          <div><strong>{revision.version || "Saved version"}</strong><br />
            <span style={{ color: "var(--text-secondary)", fontSize: 13 }}>{new Date(revision.created_at).toLocaleString()} · {revision.tool_count} tools · {revision.revision_id.slice(0, 10)}</span>
          </div>
          <button className="btn btn-secondary" disabled={revisionBusy} onClick={async () => {
            setRevisionBusy(true);
            try {
              const result = await restoreConnectorRevision(mcpId, revision.revision_id);
              router.push(`/dashboard/integrations/${result.mcp_id}`);
            } catch (err) { setRevisionError(err instanceof Error ? err.message : "Could not restore version"); }
            finally { setRevisionBusy(false); }
          }}>Restore as disabled copy</button>
        </div>)}
      </section>
      <div style={{ display: "flex", gap: 24 }}>
        {/* Main Content */}
        <div style={{ flex: 2, display: "flex", flexDirection: "column", gap: 24 }}>
          <div className="glass-card" style={{ padding: 24 }}>
            {/* Phase 6 §6.17: operation-level truth — eligibility per
                operation from the ONE authoritative evaluator, provenance
                origin, and the specific blocked reason. Never a broad green
                badge implying the whole integration is tested. */}
            {mcp.operation_eligibility?.length ? (
              <div style={{ marginBottom: 20 }}>
                <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>Operations</h2>
                {(() => {
                  const summary = eligibilitySummary(mcp);
                  return <div style={{ color: "var(--text-secondary)", fontSize: 13, marginBottom: 8 }}>
                    {summary.eligible} eligible · {summary.blocked} blocked · {summary.total} total
                  </div>;
                })()}
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead>
                    <tr style={{ textAlign: "left", color: "var(--text-tertiary)" }}>
                      <th style={{ padding: "6px 8px" }}>Operation</th>
                      <th style={{ padding: "6px 8px" }}>Origin</th>
                      <th style={{ padding: "6px 8px" }}>Eligibility</th>
                      <th style={{ padding: "6px 8px" }}>Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {mcp.operation_eligibility.map(op => (
                      <tr key={op.tool_name} style={{ borderTop: "1px solid var(--border, rgba(128,128,128,0.2))" }}>
                        <td style={{ padding: "6px 8px" }}><code>{op.tool_name}</code></td>
                        <td style={{ padding: "6px 8px", color: "var(--text-tertiary)" }}>{op.provenance}</td>
                        <td style={{ padding: "6px 8px", color: op.allowed ? "var(--success, #22c55e)" : "var(--warning, #eab308)" }}>
                          {op.allowed ? "Eligible" : "Blocked"}
                        </td>
                        <td style={{ padding: "6px 8px", color: "var(--text-secondary)" }}>
                          {op.allowed ? "—" : op.reason_codes.map(eligibilityReasonLabel).join("; ")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
            <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 20 }}>Available Tools ({mcp.tools?.length || 0})</h2>
            
            {mcp.tools && mcp.tools.length > 0 ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                {mcp.tools.map(tool => (
                  <div key={tool.tool_name || tool.name} style={{
                    padding: 16, background: "var(--bg-tertiary)", 
                    borderRadius: "var(--radius-md)", border: "1px solid var(--border-primary)"
                  }}>
                    <div style={{ fontWeight: 600, fontSize: 15, color: "var(--accent)", fontFamily: "var(--font-mono)", marginBottom: 4 }}>
                      {tool.tool_name || tool.name}
                    </div>
                    <div style={{ fontSize: 14, color: "var(--text-secondary)", marginBottom: 12 }}>
                      {tool.description}
                    </div>
                    {tool.inputSchema && (
                      <details style={{ fontSize: 13 }}>
                        <summary style={{ cursor: "pointer", color: "var(--text-tertiary)" }}>View Input Schema</summary>
                        <pre style={{ padding: 12, background: "var(--bg-secondary)", borderRadius: 4, marginTop: 8, overflowX: "auto" }}>
                          {JSON.stringify(tool.inputSchema, null, 2)}
                        </pre>
                      </details>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              <div style={{ color: "var(--text-tertiary)", fontSize: 14 }}>No tools discovered yet. Try running a test.</div>
            )}
          </div>
        </div>

        {/* Sidebar */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 24 }}>
          
          <div className="glass-card" style={{ padding: 20 }}>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 16, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
              Circuit Breaker
            </h3>
            <div style={{ display: "flex", flexDirection: "column", gap: 12, fontSize: 14 }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-tertiary)" }}>State</span>
                <span style={{ fontWeight: 600, color: mcp.circuit_breaker?.state === "CLOSED" ? "var(--success)" : mcp.circuit_breaker?.state === "OPEN" ? "var(--error)" : "var(--warning)" }}>
                  {mcp.circuit_breaker?.state || "UNKNOWN"}
                </span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-tertiary)" }}>Recent Failures</span>
                <span>{mcp.circuit_breaker?.failure_count || 0}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-tertiary)" }}>Total Successes</span>
                <span style={{ color: "var(--success)" }}>{mcp.circuit_breaker?.total_successes || 0}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-tertiary)" }}>Total Failures</span>
                <span style={{ color: "var(--error)" }}>{mcp.circuit_breaker?.total_failures || 0}</span>
              </div>
              {mcp.circuit_breaker?.retry_in_seconds !== undefined && mcp.circuit_breaker.retry_in_seconds > 0 && (
                <div style={{ display: "flex", justifyContent: "space-between", marginTop: 8, paddingTop: 8, borderTop: "1px solid var(--border-primary)" }}>
                  <span style={{ color: "var(--text-tertiary)" }}>Recovery Test In</span>
                  <span style={{ color: "var(--warning)" }}>{mcp.circuit_breaker.retry_in_seconds}s</span>
                </div>
              )}
            </div>
          </div>

          <div className="glass-card" style={{ padding: 20 }}>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
              Attach API key
            </h3>
            <p style={{ fontSize: 13, color: "var(--text-tertiary)", marginBottom: 12 }}>
              Stored in the Vault as AES-256-GCM. The value is never shown again.
            </p>
            <input className="input" type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="api_key" style={{ width: "100%", marginBottom: 8 }} autoComplete="off" />
            <button
              type="button"
              className="btn btn-secondary"
              style={{ width: "100%" }}
              disabled={keyBusy || !apiKey.trim()}
              onClick={async () => {
                setKeyBusy(true);
                try {
                  await attachIntegrationKey(mcpId, apiKey.trim());
                  setApiKey("");
                  alert("Key stored in Vault.");
                } catch (err) {
                  alert(err instanceof Error ? err.message : "Could not store key");
                } finally {
                  setKeyBusy(false);
                }
              }}
            >
              {keyBusy ? "Saving…" : "Save to Vault"}
            </button>
          </div>

          <div className="glass-card" style={{ padding: 20 }}>
            <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 16, color: "var(--error)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
              Danger Zone
            </h3>
            <button className="btn btn-danger" style={{ width: "100%" }} onClick={handleDelete}>
              Delete Integration
            </button>
          </div>

        </div>
      </div>
    </div>
  );
}
