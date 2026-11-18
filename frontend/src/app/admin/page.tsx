"use client";

/**
 * /admin — Stealth Control Center.
 *
 * Invisible to standard users: no navigation entry points anywhere in the app.
 * Non-admin navigation attempts are silently redirected to "/". Data comes
 * from the require_admin-guarded /api/v1/admin/* endpoints, which 403 anyone
 * without the admin role in their validated JWT.
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { useAuth } from "@/lib/auth-context";
import { listAdminUsers, adminStats, adminAudit, AdminAuditEvent, setUserActive, setUserRole } from "@/lib/api";
import GlassPanel, { AmbientOrbs, springSoft } from "@/components/GlassPanel";

type AdminUserRow = {
  user_id: string; email?: string; name?: string; role: string;
  is_active: boolean; created_at?: string | null;
  runs_total?: number | null; runs_5h?: number | null; last_run_at?: string | null;
};

export default function StealthAdminPage() {
  const { user, isLoading } = useAuth();
  const router = useRouter();
  const [users, setUsers] = useState<AdminUserRow[]>([]);
  const [stats, setStats] = useState<Awaited<ReturnType<typeof adminStats>> | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [audit, setAudit] = useState<AdminAuditEvent[]>([]);

  const isAdmin = user?.role === "admin" || user?.role === "super_admin";
  useEffect(() => {
    if (isLoading) return;
    if (!user) { router.replace("/login"); return; }
    if (!isAdmin) { router.replace("/"); return; }   // silent redirect — no hint
    (async () => {
      try {
        const [u, s, a] = await Promise.all([listAdminUsers(), adminStats(), adminAudit(50)]);
        setUsers(u.users as AdminUserRow[]);
        setStats(s);
        setAudit(a.events || []);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Access denied");
        router.replace("/");
      }
    })();
  }, [user, isLoading, isAdmin, router]);

  if (!isAdmin) return null;

  const tiles = stats ? [
    { label: "Users", value: stats.users_total },
    { label: "Active · 5h window", value: stats.users_active_5h },
    { label: "Workflow runs", value: stats.runs_total },
    { label: "Integrations", value: stats.integrations },
  ] : [];

  async function changeRole(u: AdminUserRow, role: string) {
    setError("");
    try {
      await setUserRole(u.user_id, role);
      const refreshed = await listAdminUsers();
      setUsers(refreshed.users as AdminUserRow[]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not change role");
    }
  }

  async function toggle(u: AdminUserRow) {
    setError("");
    try {
      await setUserActive(u.user_id, !u.is_active);
      const refreshed = await listAdminUsers();
      setUsers(refreshed.users as AdminUserRow[]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update status");
    }
  }

  return (
    <div style={{ minHeight: "100vh", padding: "48px clamp(16px, 5vw, 64px)", position: "relative" }}>
      <AmbientOrbs />
      <motion.div initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={springSoft}>
        <h1 style={{ fontSize: 30, fontWeight: 700, letterSpacing: "-0.02em", marginBottom: 4 }}>Control Center</h1>
        <p style={{ color: "var(--text-secondary)", marginBottom: 32, fontSize: 14 }}>
          Platform telemetry · usage auto-resets every {stats?.window_hours ?? 5} hours
        </p>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 16, marginBottom: 28 }}>
          {tiles.map((t) => (
            <GlassPanel key={t.label} glow={0.12} style={{ padding: "18px 20px 16px" }}>
              <div style={{ fontSize: 26, fontWeight: 700, lineHeight: 1.1 }}>{t.value}</div>
              <div style={{ fontSize: 12.5, color: "var(--text-secondary)", marginTop: 8, lineHeight: 1.4 }}>{t.label}</div>
            </GlassPanel>
          ))}
        </div>

        <GlassPanel glow={0.06} style={{ padding: 0 }}>
          <div style={{ padding: "16px 18px 10px" }}>
            <input
              className="input"
              placeholder="Search by name, email, or user id…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              style={{ maxWidth: 420 }}
            />
          </div>
          {error && <p style={{ padding: "0 18px 12px", color: "var(--error)", fontSize: 13 }}>{error}</p>}
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13.5 }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-tertiary)", fontSize: 12 }}>
                  <th style={{ padding: "14px 18px" }}>User</th>
                  <th style={{ padding: "14px 18px" }}>Role</th>
                  <th style={{ padding: "14px 18px" }}>Runs · 5h</th>
                  <th style={{ padding: "14px 18px" }}>Total runs</th>
                  <th style={{ padding: "14px 18px" }}>MCPs built</th>
                  <th style={{ padding: "14px 18px" }}>Joined</th>
                  <th style={{ padding: "14px 18px" }}>Status</th>
                  <th style={{ padding: "14px 18px" }}></th>
                </tr>
              </thead>
              <tbody>
                {users
                .filter((u) => {
                  const q = query.trim().toLowerCase();
                  if (!q) return true;
                  return (u.email || "").toLowerCase().includes(q)
                    || (u.name || "").toLowerCase().includes(q)
                    || u.user_id.toLowerCase().includes(q);
                })
                .map((u) => (
                  <tr key={u.user_id} style={{ borderTop: "1px solid var(--border-primary)" }}>
                    <td style={{ padding: "12px 18px" }}>
                      <div style={{ fontWeight: 600 }}>{u.name || u.email}</div>
                      <div style={{ fontSize: 12, color: "var(--text-tertiary)" }}>{u.email}</div>
                    </td>
                    <td style={{ padding: "12px 18px" }}>
                      <select
                        className="input"
                        style={{ padding: "4px 8px", fontSize: 12.5 }}
                        value={u.role}
                        onChange={(e) => void changeRole(u, e.target.value)}
                        disabled={u.user_id === user?.id}
                        title={u.user_id === user?.id ? "You cannot change your own role" : ""}
                        aria-label={`Role for ${u.email}`}
                      >
                        <option value="super_admin">super admin (unique)</option>
                        <option value="admin">admin</option>
                        <option value="user">user</option>
                        <option value="viewer">viewer</option>
                        <option value="guest">guest</option>
                      </select>
                    </td>
                    <td style={{ padding: "12px 18px" }}>{u.runs_5h ?? "—"}</td>
                    <td style={{ padding: "12px 18px" }}>{u.runs_total ?? "—"}</td>
                    <td style={{ padding: "12px 18px" }}>{(u as { mcps?: number }).mcps ?? "—"}</td>
                    <td style={{ padding: "12px 18px" }}>{u.created_at ? String(u.created_at).slice(0, 10) : "—"}</td>
                    <td style={{ padding: "12px 18px" }}>
                      <span className={`badge ${u.is_active ? "badge-success" : "badge-error"}`}>
                        {u.is_active ? "active" : "disabled"}
                      </span>
                    </td>
                    <td style={{ padding: "12px 18px", textAlign: "right" }}>
                      <button type="button" className="btn btn-ghost btn-sm" onClick={() => void toggle(u)}>
                        {u.is_active ? "Disable" : "Enable"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </GlassPanel>

        <GlassPanel glow={0.06} style={{ padding: 0, marginTop: 24 }}>
          <div style={{ padding: "16px 18px 10px", display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
            <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>Audit log</h2>
            <span style={{ fontSize: 12, color: "var(--text-tertiary)" }}>latest {audit.length} events</span>
          </div>
          <div style={{ overflowX: "auto", maxHeight: 420, overflowY: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-tertiary)", fontSize: 11.5 }}>
                  <th style={{ padding: "12px 18px" }}>When</th>
                  <th style={{ padding: "12px 18px" }}>Event</th>
                  <th style={{ padding: "12px 18px" }}>Actor</th>
                  <th style={{ padding: "12px 18px" }}>Resource</th>
                </tr>
              </thead>
              <tbody>
                {audit.map((e, i) => (
                  <tr key={`${e.timestamp}-${i}`} style={{ borderTop: "1px solid var(--border-primary)" }}>
                    <td style={{ padding: "9px 18px", whiteSpace: "nowrap", color: "var(--text-tertiary)" }}>
                      {e.timestamp ? new Date(e.timestamp).toLocaleString() : "—"}
                    </td>
                    <td style={{ padding: "9px 18px", fontFamily: "var(--font-mono)", fontSize: 12 }}>{e.event_type}</td>
                    <td style={{ padding: "9px 18px" }}>
                      {e.actor_type}: <span style={{ fontFamily: "var(--font-mono)", fontSize: 11.5 }}>{e.actor_id.slice(0, 8)}</span>
                    </td>
                    <td style={{ padding: "9px 18px", fontFamily: "var(--font-mono)", fontSize: 11.5 }}>
                      {e.run_id ? e.run_id.slice(0, 14) : e.resource_id?.slice(0, 14)}
                    </td>
                  </tr>
                ))}
                {audit.length === 0 && (
                  <tr><td colSpan={4} style={{ padding: "16px 18px", color: "var(--text-tertiary)" }}>No audit events yet.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </GlassPanel>
      </motion.div>
    </div>
  );
}
