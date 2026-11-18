"use client";

import { useCallback, useEffect, useState } from "react";
import { createApiKey, listApiKeys, revokeApiKey, ApiKeyRow } from "@/lib/api";

/**
 * API keys for programmatic access (MCP clients, CI, automations).
 * The raw key is shown exactly once at creation; only a prefix is
 * retained for identification.
 */
export default function ApiKeyManager() {
  const [keys, setKeys] = useState<ApiKeyRow[]>([]);
  const [name, setName] = useState("");
  const [created, setCreated] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await listApiKeys();
      setKeys(res.keys || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load API keys");
    }
  }, []);

  useEffect(() => {
    let active = true;
    listApiKeys().then(res => { if (active) setKeys(res.keys || []); })
      .catch(err => { if (active) setError(err instanceof Error ? err.message : "Could not load API keys"); });
    return () => { active = false; };
  }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await createApiKey(name.trim() || "default");
      setCreated(res.key);
      setName("");
      await load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not create the key");
    } finally {
      setBusy(false);
    }
  };

  const handleRevoke = async (id: string) => {
    if (!confirm("Revoke this key? Anything using it stops working immediately.")) return;
    try {
      await revokeApiKey(id);
      await load();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Revoke failed");
    }
  };

  return (
    <div className="glass-card" style={{ padding: 24, marginBottom: 24 }}>
      <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 6, borderBottom: "1px solid var(--border-primary)", paddingBottom: 12 }}>
        API Keys
      </h2>
      <p style={{ fontSize: 13, color: "var(--text-secondary)", margin: "10px 0 16px", lineHeight: 1.5 }}>
        Authenticate scripts and MCP clients with <code style={{ fontFamily: "var(--font-mono)" }}>Authorization: Bearer vry_…</code> —
        no 15-minute session expiry, revocable instantly.
      </p>

      {error && <p style={{ color: "var(--error)", fontSize: 13, marginBottom: 12 }}>{error}</p>}

      {created && (
        <div style={{ padding: 12, background: "var(--accent-subtle)", borderRadius: "var(--radius-md)", marginBottom: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 6 }}>Copy your key now — it is never shown again:</div>
          <code style={{ fontFamily: "var(--font-mono)", fontSize: 13, wordBreak: "break-all", userSelect: "all" }}>{created}</code>
          <button type="button" className="btn btn-ghost btn-sm" style={{ marginLeft: 8 }} onClick={() => setCreated(null)}>
            Done
          </button>
        </div>
      )}

      <form onSubmit={handleCreate} style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <input
          className="input"
          placeholder="Key name (e.g. ci-pipeline)"
          value={name}
          onChange={e => setName(e.target.value)}
          maxLength={100}
          style={{ maxWidth: 280 }}
        />
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Creating…" : "Create key"}
        </button>
      </form>

      {keys.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--text-tertiary)", fontSize: 12 }}>
                <th style={{ padding: "8px 12px 8px 0" }}>Name</th>
                <th style={{ padding: "8px 12px" }}>Key</th>
                <th style={{ padding: "8px 12px" }}>Created</th>
                <th style={{ padding: "8px 12px" }}>Last used</th>
                <th style={{ padding: 12 }}></th>
              </tr>
            </thead>
            <tbody>
              {keys.map(k => (
                <tr key={k.id} style={{ borderTop: "1px solid var(--border-primary)" }}>
                  <td style={{ padding: "9px 12px 9px 0" }}>{k.name}</td>
                  <td style={{ padding: 12, fontFamily: "var(--font-mono)", fontSize: 12 }}>
                    {k.prefix}…
                    {(k.revoked_at || k.is_active === false) && <span className="badge badge-error" style={{ marginLeft: 6 }}>revoked</span>}
                  </td>
                  <td style={{ padding: 12, color: "var(--text-tertiary)" }}>{k.created_at?.slice(0, 10)}</td>
                  <td style={{ padding: 12, color: "var(--text-tertiary)" }}>
                    {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}
                  </td>
                  <td style={{ padding: 12, textAlign: "right" }}>
                    {!k.revoked_at && k.is_active !== false && (
                      <button type="button" className="btn btn-ghost btn-sm" style={{ color: "var(--error)" }} onClick={() => void handleRevoke(k.id)}>
                        Revoke
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {keys.length === 0 && !created && (
        <p style={{ fontSize: 13, color: "var(--text-tertiary)" }}>No keys yet.</p>
      )}
    </div>
  );
}
