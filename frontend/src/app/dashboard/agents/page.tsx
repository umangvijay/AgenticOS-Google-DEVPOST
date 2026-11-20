"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { AgentPlugin, AgentTool, installAgentPlugin, listAgentPlugins, listAgentTools, setAgentPluginEnabled } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

function toId(value: string) {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

export default function AgentsPage() {
  const { user } = useAuth();
  const [plugins, setPlugins] = useState<AgentPlugin[]>([]);
  const [tools, setTools] = useState<AgentTool[]>([]);
  const [name, setName] = useState("");
  const [instructions, setInstructions] = useState("");
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const refresh = useCallback(async () => {
    setError("");
    try {
      const [pluginResult, toolResult] = await Promise.all([listAgentPlugins(), listAgentTools()]);
      setPlugins(pluginResult.plugins || []);
      setTools(toolResult.tools || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load agents and available tools");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void Promise.resolve().then(() => { if (!cancelled) return refresh(); });
    return () => { cancelled = true; };
  }, [refresh]);

  async function createAgent(event: FormEvent) {
    event.preventDefault();
    setError(""); setNotice("");
    if (!name.trim() || !instructions.trim() || selectedTools.length === 0) {
      setError("Add a name, clear instructions, and at least one available tool.");
      return;
    }
    const agentId = toId(name);
    if (!agentId) { setError("Choose a name with letters or numbers."); return; }
    setBusy(true);
    try {
      const installed = await installAgentPlugin({
        plugin_id: `user-${agentId}`,
        name: `${name.trim()} Agent`,
        version: "1.0.0",
        description: instructions.trim().slice(0, 300),
        author: user?.name || user?.email || "Sandtable user",
        agentos_version: ">=1.0.0",
        requested_permissions: [],
        required_tools: selectedTools,
        agents: [{ agent_id: agentId, display_name: name.trim(), instructions: instructions.trim(), allowed_tools: selectedTools }],
        scope: "USER",
      });
      await setAgentPluginEnabled(installed.plugin.id, true, selectedTools);
      setName(""); setInstructions(""); setSelectedTools([]);
      setNotice(`${name.trim()} is enabled and available to workflow planning.`);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Agent could not be installed");
    } finally {
      setBusy(false);
    }
  }

  async function toggle(plugin: AgentPlugin, enabled: boolean) {
    setBusy(true); setError(""); setNotice("");
    try {
      await setAgentPluginEnabled(plugin.id, enabled, plugin.manifest.required_tools);
      await refresh();
      setNotice(`${plugin.manifest.name} ${enabled ? "enabled" : "disabled"}.`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Agent state could not be changed");
    } finally { setBusy(false); }
  }

  return <div className="animate-fade-in-up" style={{ maxWidth: 1040, margin: "0 auto" }}>
    <div style={{ marginBottom: 26 }}>
      <p className="eyebrow">Specialist agents</p>
      <h1 style={{ fontSize: 26, fontWeight: 700, margin: "4px 0 8px" }}>Build your agent team</h1>
      <p style={{ color: "var(--text-secondary)", maxWidth: 680 }}>Define a focused agent, grant only the connected tools it needs, and make it selectable by the workflow planner.</p>
    </div>
    {error && <div role="alert" className="glass-card" style={{ color: "var(--error)", padding: 14, marginBottom: 16 }}>{error}</div>}
    {notice && <div role="status" className="glass-card" style={{ color: "var(--success)", padding: 14, marginBottom: 16 }}>{notice}</div>}

    <section className="glass-card" style={{ padding: 22, marginBottom: 26 }}>
      <h2 style={{ fontSize: 17, margin: "0 0 6px" }}>Create a specialist</h2>
      <p style={{ color: "var(--text-secondary)", fontSize: 13, margin: "0 0 18px" }}>Agents can use only the tools selected here. Add an MCP from Integrations if the tool you need is missing.</p>
      <form onSubmit={createAgent} style={{ display: "grid", gap: 14 }}>
        <label style={{ display: "grid", gap: 6, fontSize: 13 }}>Agent name
          <input className="input" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Release coordinator" maxLength={80} />
        </label>
        <label style={{ display: "grid", gap: 6, fontSize: 13 }}>What should this agent do?
          <textarea className="input" rows={4} value={instructions} onChange={e => setInstructions(e.target.value)} placeholder="Describe the agent's responsibilities, decision rules, and the result it should return." maxLength={5000} />
        </label>
        <fieldset style={{ border: "1px solid var(--border-primary)", borderRadius: 10, padding: 14 }}>
          <legend style={{ fontSize: 13, padding: "0 6px" }}>Available tools</legend>
          {loading ? <p style={{ color: "var(--text-secondary)", fontSize: 13 }}>Loading connected tools…</p> : tools.length === 0 ?
            <p style={{ color: "var(--text-secondary)", fontSize: 13, margin: 0 }}>No tools are connected yet. Build an MCP integration first.</p> :
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(250px, 1fr))", gap: 8 }}>
              {tools.map(tool => <label key={tool.agent_tool_name} style={{ display: "flex", alignItems: "flex-start", gap: 9, padding: 10, border: "1px solid var(--border-primary)", borderRadius: 8, cursor: "pointer" }}>
                <input type="checkbox" checked={selectedTools.includes(tool.agent_tool_name)} onChange={e => setSelectedTools(current => e.target.checked ? [...current, tool.agent_tool_name] : current.filter(t => t !== tool.agent_tool_name))} />
                <span><strong style={{ display: "block", fontSize: 12 }}>{tool.agent_tool_name}</strong><span style={{ color: "var(--text-secondary)", fontSize: 12 }}>{tool.description || tool.name}</span></span>
              </label>)}
            </div>}
        </fieldset>
        <div><button className="btn btn-primary" disabled={busy || loading || tools.length === 0}>{busy ? "Creating…" : "Create and enable agent"}</button></div>
      </form>
    </section>

    <section>
      <h2 style={{ fontSize: 17, marginBottom: 12 }}>Your agents</h2>
      {loading ? <div className="skeleton" style={{ height: 100 }} /> : plugins.length === 0 ?
        <div className="glass-card" style={{ padding: 18, color: "var(--text-secondary)" }}>Your installed agents will appear here after creation.</div> :
        <div style={{ display: "grid", gap: 12 }}>
          {plugins.map(plugin => <article className="glass-card" key={plugin.id} style={{ padding: 18, display: "flex", gap: 16, justifyContent: "space-between", alignItems: "center", flexWrap: "wrap" }}>
            <div><div style={{ display: "flex", alignItems: "center", gap: 10 }}><strong>{plugin.manifest.name}</strong><span className={`badge ${plugin.state === "ENABLED" ? "badge-success" : "badge-neutral"}`}>{plugin.state}</span></div>
              <p style={{ color: "var(--text-secondary)", fontSize: 13, margin: "7px 0" }}>{plugin.manifest.description}</p>
              {plugin.state === "ENABLED" && <div style={{ color: "var(--text-secondary)", fontSize: 13 }}>
                Start a chat message with {plugin.manifest.agents.map(agent => `@${agent.agent_id}`).join(" or ")} to use this agent.
              </div>}
            </div>
            <button className="btn btn-secondary" disabled={busy || !["ENABLED", "DISABLED"].includes(plugin.state)} onClick={() => void toggle(plugin, plugin.state !== "ENABLED")}>
              {plugin.state === "ENABLED" ? "Disable" : plugin.state === "DISABLED" ? "Enable" : "Unavailable"}
            </button>
          </article>)}
        </div>}
    </section>
  </div>;
}
