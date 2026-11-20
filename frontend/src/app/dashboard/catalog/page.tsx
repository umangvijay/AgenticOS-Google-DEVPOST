"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listCatalog, installFromCatalog, CatalogEntry } from "@/lib/api";

export default function CatalogPage() {
  const [entries, setEntries] = useState<CatalogEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [info, setInfo] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    listCatalog().then(res => { if (active) setEntries(res.integrations || []); })
      .catch(err => { if (active) setLoadError(err instanceof Error ? err.message : "Please try again in a moment."); })
      .finally(() => { if (active) setIsLoading(false); });
    return () => { active = false; };
  }, [loadAttempt]);

  const handleInstall = async (entry: CatalogEntry) => {
    setBusyId(entry.mcp_id);
    setInfo("");
    setError("");
    try {
      const res = await installFromCatalog(entry.mcp_id);
      setInfo(res.installed
        ? `Installed "${res.name}" with ${res.tools} tools. Attach credentials on the Integrations page if the API needs a key.`
        : res.message || "Already installed.");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Install failed");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="animate-fade-in" data-scroll-reveal style={{ maxWidth: 1000, margin: "0 auto" }}>
      <div style={{ marginBottom: 28 }}>
        <h1 style={{ fontSize: 28, fontWeight: 700, marginBottom: 8 }}>Catalog</h1>
        <p style={{ color: "var(--text-secondary)", lineHeight: 1.5 }}>
          Published integrations shared by the community. Only live-probe-verified builds can be
          published; installing gives you your own copy — credentials are never shared, attach your
          own in the Vault.
        </p>
      </div>

      {loadError && (
        <div className="glass-card load-error" role="alert">
          <h2>Couldn’t load the catalog</h2>
          <p>{loadError}</p>
          <button className="btn btn-secondary" onClick={() => { setLoadError(""); setIsLoading(true); setLoadAttempt(n => n + 1); }}>Try again</button>
        </div>
      )}
      {error && (
        <div style={{ padding: "12px 16px", background: "var(--error-subtle)", color: "var(--error)", borderRadius: "var(--radius-md)", marginBottom: 20 }}>
          {error}
        </div>
      )}
      {info && (
        <div style={{ padding: "12px 16px", background: "var(--success-subtle, rgba(34,197,94,0.12))", color: "var(--success, #22c55e)", borderRadius: "var(--radius-md)", marginBottom: 20 }}>
          {info}
        </div>
      )}

      {isLoading ? (
        <div className="glass-card" style={{ padding: 24, display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="skeleton" style={{ height: 90 }} />
          <div className="skeleton" style={{ height: 90 }} />
        </div>
      ) : loadError ? null : entries.length === 0 ? (
        <div className="glass-card" style={{ padding: 32, textAlign: "center" }}>
          <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>The catalog is empty</h3>
          <p style={{ color: "var(--text-secondary)", fontSize: 14 }}>
            Build an integration from an OpenAPI URL — once its live probe passes you can publish it here.
          </p>
          <Link className="btn btn-secondary" href="/dashboard/integrations/create" style={{ marginTop: 16 }}>Build an integration</Link>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {entries.map(entry => (
            <div key={entry.mcp_id} className="glass-card" style={{ padding: "18px 22px" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
                    <h3 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>{entry.name}</h3>
                    <span className="badge badge-success">{entry.trust_tier}</span>
                    <span className="badge badge-info" style={{ textTransform: "none" }}>{entry.tool_count} tools</span>
                  </div>
                  {entry.description && (
                    <p style={{ fontSize: 13.5, color: "var(--text-secondary)", margin: "0 0 8px", lineHeight: 1.5 }}>{entry.description}</p>
                  )}
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    style={{ padding: 0, color: "var(--accent)" }}
                    onClick={() => setExpanded(expanded === entry.mcp_id ? null : entry.mcp_id)}
                  >
                    {expanded === entry.mcp_id ? "Hide tools" : "Show tools"}
                  </button>
                  {expanded === entry.mcp_id && (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
                      {entry.tools.map(t => (
                        <span key={t.name} className="badge badge-neutral" style={{ fontFamily: "var(--font-mono)", textTransform: "none", fontSize: 11.5 }}>
                          {t.name}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busyId === entry.mcp_id}
                  onClick={() => void handleInstall(entry)}
                >
                  {busyId === entry.mcp_id ? "Installing…" : "Install"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
