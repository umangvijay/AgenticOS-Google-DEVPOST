"use client";

import { useState, useEffect } from "react";
import { getSettings, updateSettings, UserSettings, storeCredential, validateCredential, pingGemini, listAdminUsers, setUserActive, adminStats } from "@/lib/api";
import ApiKeyManager from "@/components/ApiKeyManager";
import TokenStatusCard from "@/components/TokenStatusCard";
import { applyTheme, type ThemePref } from "@/lib/theme";
import { useAuth } from "@/lib/auth-context";
import ModelSelector from "@/components/ModelSelector";
import type { ModelCatalog } from "@/lib/api";

export default function SettingsPage() {
  const { user, updateProfile } = useAuth();
  const [settings, setSettings] = useState<UserSettings | null>(null);
  const [availableModels, setAvailableModels] = useState<string[]>([]);
  const [modelCatalog, setModelCatalog] = useState<ModelCatalog>();
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState({ text: "", type: "" });

  const [name, setName] = useState(user?.name || "");
  const [testingKey, setTestingKey] = useState(false);
  const [keyMessage, setKeyMessage] = useState({ text: "", type: "" });
  const [keyProvider, setKeyProvider] = useState("gemini");
  const [unifiedKey, setUnifiedKey] = useState("");
  const [savingUnified, setSavingUnified] = useState(false);
  const [unifiedMsg, setUnifiedMsg] = useState({ text: "", type: "" });

  const PROVIDERS = [
    { id: "gemini", label: "Google Gemini", placeholder: "AIza… or AQ.…" },
    { id: "claude", label: "Anthropic Claude", placeholder: "sk-ant-…" },
    { id: "grok", label: "xAI Grok", placeholder: "xai-…" },
    { id: "zai", label: "Z.ai GLM", placeholder: "zai-… or key id" },
    { id: "openai", label: "OpenAI", placeholder: "sk-…" },
  ];

  const saveUnifiedKey = async () => {
    const key = unifiedKey.trim();
    if (!key) return;
    setSavingUnified(true);
    setUnifiedMsg({ text: "", type: "" });
    try {
      const validation = await validateCredential(keyProvider, { api_key: key });
      if (!validation.valid) {
        setUnifiedMsg({ text: validation.error || "That key was rejected by the provider.", type: "error" });
        return;
      }
      await storeCredential(keyProvider, { api_key: key });
      setUnifiedKey("");
      const label = PROVIDERS.find(p => p.id === keyProvider)?.label || keyProvider;
      setUnifiedMsg({
        text: `${label} key validated live and stored in the encrypted Vault.`
          + (keyProvider === "gemini" ? " The agent will use it for planning, MCP builds, and generation."
            : keyProvider === "grok" ? " It is used automatically when Gemini hits quota."
            : " Stored for integrations and tools that use this provider."),
        type: "success",
      });
    } catch (err) {
      setUnifiedMsg({ text: err instanceof Error ? err.message : "Could not save key", type: "error" });
    } finally {
      setSavingUnified(false);
    }
  };

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const { settings: s, available_models, model_catalog } = await getSettings();
        if (!active) return;
        setAvailableModels(available_models || []);
        setModelCatalog(model_catalog);
        setSettings(s);
      } catch (err) {
        if (active) setLoadError(err instanceof Error ? err.message : "Please try again in a moment.");
      } finally {
        if (active) setLoading(false);
      }
    }
    load();
    return () => { active = false; };
  }, [loadAttempt]);

  const handleSaveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settings) return;
    setSaving(true);
    setMessage({ text: "", type: "" });
    try {
      await updateSettings(settings);
      
      // Update profile if name changed
      if (name !== user?.name) {
        await updateProfile({ name });
      }

      // Apply theme immediately and persist for first-paint
      applyTheme((settings.theme as ThemePref) || "system");

      setMessage({ text: "Settings saved successfully", type: "success" });
    } catch (err: unknown) {
      setMessage({ text: err instanceof Error ? err.message : "Failed to save settings", type: "error" });
    } finally {
      setSaving(false);
    }
  };

  const testGemini = async () => {
    setTestingKey(true);
    setKeyMessage({ text: "", type: "" });
    try {
      // If a key is pasted in the field, test THAT key, not the vault key
      const candidateKey = keyProvider === "gemini" ? unifiedKey.trim() || undefined : undefined;
      const res = await pingGemini(candidateKey);
      setKeyMessage({
        text: res.ok
          ? `Gemini is working${res.using_user_key ? (candidateKey ? " with the pasted key" : " with your vault key") : " with the server key"}.`
          : `Gemini replied: ${res.reply || "unexpected response"}`,
        type: res.ok ? "success" : "error",
      });
    } catch (err: unknown) {
      setKeyMessage({
        text: err instanceof Error ? err.message : "Gemini test failed. Check the key and try again.",
        type: "error",
      });
    } finally {
      setTestingKey(false);
    }
  };

  const [adminUsers, setAdminUsers] = useState<Awaited<ReturnType<typeof listAdminUsers>> | null>(null);
  const [adminStatsData, setAdminStatsData] = useState<Awaited<ReturnType<typeof adminStats>> | null>(null);

  useEffect(() => {
    if (user?.role !== "admin") return;
    (async () => {
      try { setAdminUsers(await listAdminUsers()); } catch { /* not admin */ }
      try { setAdminStatsData(await adminStats()); } catch { /* ignore */ }
    })();
  }, [user?.role]);

  const toggleUser = async (userId: string, active: boolean) => {
    await setUserActive(userId, active);
    try { setAdminUsers(await listAdminUsers()); } catch { /* ignore */ }
  };

  if (loading) return (
    <div style={{ display: "flex", justifyContent: "center", padding: 100 }}>
      <div className="spinner" />
    </div>
  );

  if (loadError) return (
    <section className="glass-card load-error" role="alert">
      <h1>Couldn’t load settings</h1>
      <p>{loadError}</p>
      <button className="btn btn-secondary" onClick={() => { setLoadError(""); setLoading(true); setLoadAttempt(n => n + 1); }}>Try again</button>
    </section>
  );

  return (
    <div className="animate-fade-in" data-scroll-reveal style={{ maxWidth: 800, margin: "0 auto" }}>
      <div style={{ marginBottom: 32 }}>
        <h1 style={{ fontSize: 24, fontWeight: 700 }}>Settings</h1>
        <p style={{ color: "var(--text-secondary)" }}>Manage your account and Sandtable preferences.</p>

      </div>

      <form onSubmit={handleSaveSettings}>
        {message.text && (
          <div style={{
            padding: "12px 16px", marginBottom: 24,
            background: message.type === "success" ? "var(--success-subtle)" : "var(--error-subtle)", 
            borderRadius: "var(--radius-md)",
            color: message.type === "success" ? "var(--success)" : "var(--error)", 
            fontSize: 14,
          }}>
            {message.text}
          </div>
        )}

        <div className="glass-card" style={{ padding: 24, marginBottom: 24 }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 20, borderBottom: "1px solid var(--border-primary)", paddingBottom: 12 }}>
            Account Profile
          </h2>
          
          <div style={{ display: "flex", gap: 24, alignItems: "flex-start" }}>
            <div style={{
              width: 80, height: 80, borderRadius: "50%",
              background: "var(--accent-subtle)", color: "var(--accent)",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 32, fontWeight: 600, flexShrink: 0
            }}>
              {user?.name?.charAt(0)?.toUpperCase() || "U"}
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ marginBottom: 16 }}>
                <label style={{ display: "block", fontSize: 13, fontWeight: 500, color: "var(--text-secondary)", marginBottom: 6 }}>Full Name</label>
                <input type="text" className="input" value={name} onChange={e => setName(e.target.value)} required />
              </div>
              <div>
                <label style={{ display: "block", fontSize: 13, fontWeight: 500, color: "var(--text-secondary)", marginBottom: 6 }}>Email</label>
                <input type="email" className="input" value={user?.email || ""} disabled style={{ opacity: 0.7 }} />
              </div>
            </div>
          </div>
        </div>

        <div className="glass-card" style={{ padding: 24, marginBottom: 24 }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8, borderBottom: "1px solid var(--border-primary)", paddingBottom: 12 }}>
            Run Webhooks (outbound)
          </h2>
          <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 12, lineHeight: 1.5 }}>
            We POST a signed summary (HMAC-SHA256) to this HTTPS URL every time one of your runs
            finishes (COMPLETED or FAILED). Must be a public HTTPS endpoint. Leave empty to disable.
            Optionally store a Vault secret named <code>webhook_out</code> to override the default signing key.
          </p>
          <input
            type="url"
            className="input"
            placeholder="https://your-service.example.com/hooks/veyrune"
            value={settings?.webhook_url || ""}
            onChange={e => setSettings(s => s ? { ...s, webhook_url: e.target.value } : s)}
            style={{ maxWidth: 480 }}
          />
        </div>

        <div className="glass-card" style={{ padding: 24, marginBottom: 24 }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 20, borderBottom: "1px solid var(--border-primary)", paddingBottom: 12 }}>
            Agent Autonomy
          </h2>
          
          <div style={{ marginBottom: 24 }}>
            <label style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
              <div>
                <div style={{ fontSize: 14, fontWeight: 500 }}>Autonomy Level</div>
                <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>How much freedom agents have to execute tools.</div>
              </div>
            </label>
            <select 
              className="input" 
              value={settings?.autonomy_level || 1}
              onChange={e => setSettings(s => s ? {...s, autonomy_level: Number(e.target.value)} : s)}
            >
              <option value={0}>Level 0: No autonomous execution (Ask for all)</option>
              <option value={1}>Level 1: Safe actions only (Default)</option>
              <option value={2}>Level 2: Moderate actions</option>
              <option value={3}>Level 3: Full autonomy (Dangerous)</option>
            </select>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
            <input 
              type="checkbox" 
              id="autoApprove"
              checked={settings?.auto_approve_low_risk || false}
              onChange={e => setSettings(s => s ? {...s, auto_approve_low_risk: e.target.checked} : s)}
              style={{ width: 16, height: 16, accentColor: "var(--accent)" }}
            />
            <label htmlFor="autoApprove" style={{ fontSize: 14, cursor: "pointer" }}>Auto-approve low risk actions</label>
          </div>
        </div>

        <TokenStatusCard />

        <div className="glass-card" style={{ padding: 24, marginBottom: 24 }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 20, borderBottom: "1px solid var(--border-primary)", paddingBottom: 12 }}>
            Model API key
          </h2>
          <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 16 }}>
            One place for any provider. Keys are validated live against the provider before being stored encrypted (AES-256-GCM) in your Vault — invalid keys are rejected. Gemini powers the agent; Grok is the automatic fallback; other providers are stored for their integrations.
          </p>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
            <select className="input" value={keyProvider} onChange={e => { setKeyProvider(e.target.value); setUnifiedMsg({ text: "", type: "" }); }} style={{ maxWidth: 220 }}>
              {PROVIDERS.map(p => <option key={p.id} value={p.id}>{p.label}</option>)}
            </select>
            <input
              type="password"
              className="input"
              placeholder={PROVIDERS.find(p => p.id === keyProvider)?.placeholder || "Paste your API key"}
              value={unifiedKey}
              onChange={(e) => setUnifiedKey(e.target.value)}
              style={{ flex: 1, minWidth: 220, fontSize: 15 }}
            />
            <button type="button" className="btn btn-primary" disabled={savingUnified || !unifiedKey.trim()} onClick={saveUnifiedKey}>
              {savingUnified ? "Validating…" : "Validate & Save"}
            </button>
            <button type="button" className="btn btn-ghost" disabled={testingKey} onClick={testGemini}>
              {testingKey ? "Testing…" : "Test Gemini"}
            </button>
          </div>
          {unifiedMsg.text && <p style={{ marginTop: 8, fontSize: 13, color: unifiedMsg.type === "success" ? "var(--success)" : "var(--error)" }}>{unifiedMsg.text}</p>}
          {keyMessage.text && <p style={{ marginTop: 8, fontSize: 13, color: keyMessage.type === "success" ? "var(--success)" : "var(--error)" }}>{keyMessage.text}</p>}
        </div>

        <div className="glass-card" style={{ padding: 24, marginBottom: 32 }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 20, borderBottom: "1px solid var(--border-primary)", paddingBottom: 12 }}>
            System Preferences
          </h2>
          
          <div style={{ display: "flex", gap: 24 }}>
            <div style={{ flex: 1 }}>
              <label style={{ display: "block", fontSize: 13, fontWeight: 500, color: "var(--text-secondary)", marginBottom: 6 }}>Theme</label>
              <select
                className="input"
                value={settings?.theme || "light"}
                onChange={e => {
                  const theme = e.target.value as ThemePref;
                  setSettings(s => s ? {...s, theme} : s);
                  // Apply instantly so the user sees the change before saving.
                  applyTheme(theme);
                }}
              >
                <option value="system">System (auto)</option>
                <option value="light">Light</option>
                <option value="dark">Dark</option>
              </select>
            </div>
            <div style={{ flex: 1 }}>
              <ModelSelector label="Default AI model" value={settings?.default_model || ""}
                onChange={model => setSettings(s => s ? {...s, default_model: model} : s)}
                models={availableModels} catalog={modelCatalog} />
              {modelCatalog?.auth_mode === "service_account_adc" && <p style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                Vertex AI uses the server’s Google Cloud service account. A Gemini API key is not required.
              </p>}
            </div>
          </div>
        </div>

        {user?.role === "admin" && (
          <div className="glass-card" style={{ padding: 24, marginBottom: 24 }}>
            <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 12 }}>Admin console</h2>
            {adminStatsData && (
              <p style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 12 }}>
                {adminStatsData.users_total} users · {adminStatsData.users_active_5h} active in the last 5h · {adminStatsData.runs_total} total runs · {adminStatsData.integrations} integrations
              </p>
            )}
            {adminUsers && adminUsers.users.length > 0 && (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead><tr style={{ textAlign: "left", color: "var(--text-tertiary)" }}>
                    <th style={{ padding: 6 }}>User</th><th style={{ padding: 6 }}>Role</th>
                    <th style={{ padding: 6 }}>Runs (5h)</th><th style={{ padding: 6 }}>Total</th><th style={{ padding: 6 }}>Status</th><th></th>
                  </tr></thead>
                  <tbody>
                    {adminUsers.users.map(u => (
                      <tr key={u.user_id} style={{ borderTop: "1px solid var(--border-primary)" }}>
                        <td style={{ padding: 8 }}>{u.email}{u.name ? ` · ${u.name}` : ""}</td>
                        <td style={{ padding: 8 }}>{u.role}</td>
                        <td style={{ padding: 8 }}>{u.runs_5h ?? "—"}</td>
                        <td style={{ padding: 8 }}>{u.runs_total ?? "—"}</td>
                        <td style={{ padding: 8 }}>{u.is_active ? "active" : "disabled"}</td>
                        <td style={{ padding: 8 }}>
                          <button type="button" className="btn btn-ghost btn-sm" onClick={() => void toggleUser(u.user_id, !u.is_active)}>
                            {u.is_active ? "Disable" : "Enable"}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {!adminUsers && <p style={{ fontSize: 13, color: "var(--text-tertiary)" }}>Loading users…</p>}
          </div>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? <span className="spinner" style={{ width: 16, height: 16 }} /> : "Save Changes"}
          </button>
        </div>
      </form>

      <ApiKeyManager />
    </div>
  );
}
