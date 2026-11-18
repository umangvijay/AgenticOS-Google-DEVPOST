"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  ApiError,
  createOrg, listMyOrgs, getOrg, removeOrgMember,
  setOrgMemberRole, deleteOrg, getOrgRuns, inviteOrgMember, acceptOrgInvitation,
  OrgRow, OrgMember, OrgRunRow,
} from "@/lib/api";
import { useAuth } from "@/lib/auth-context";

const ROLE_BADGE: Record<string, string> = {
  owner: "badge-success", admin: "badge-info", member: "badge-neutral", viewer: "badge-neutral",
};

export default function OrgsPage() {
  const { user } = useAuth();
  const [orgs, setOrgs] = useState<OrgRow[]>([]);
  const [selected, setSelected] = useState<OrgRow | null>(null);
  const [members, setMembers] = useState<OrgMember[]>([]);
  const [runs, setRuns] = useState<OrgRunRow[] | null>(null);
  const [showOversight, setShowOversight] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [lastToken, setLastToken] = useState("");
  const [inviteToken, setInviteToken] = useState("");
  const [showAccept, setShowAccept] = useState(false);

  const loadOrgs = useCallback(async () => {
    try {
      const res = await listMyOrgs();
      setOrgs(res.orgs || []);
      setError("");
      return res.orgs || [];
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not load organizations");
      return [];
    }
  }, []);

  useEffect(() => {
    let active = true;
    listMyOrgs().then(res => { if (active) setOrgs(res.orgs || []); })
      .catch(err => { if (active) setError(err instanceof Error ? err.message : "Could not load organizations"); });
    return () => { active = false; };
  }, []);

  // Phase 1.13: when access changes while the user is on the page (removed,
  // demoted, or the org deleted), stale privileged state must not linger.
  // 403/404 from an org-scoped call clears the selected org, its members,
  // and oversight results, then refreshes the org list.
  const handleAccessLoss = useCallback(async (err: unknown, fallback: string) => {
    if (err instanceof ApiError && (err.status === 403 || err.status === 404)) {
      setSelected(null);
      setMembers([]);
      setRuns(null);
      setShowOversight(false);
      setError("Your access to this organization changed, so its data was cleared. Reload to see your current organizations.");
      await loadOrgs();
      return;
    }
    setError(err instanceof Error ? err.message : fallback);
  }, [loadOrgs]);

  const openOrg = useCallback(async (org: OrgRow) => {
    setSelected(org);
    setRuns(null);
    setShowOversight(false);
    try {
      const detail = await getOrg(org.org_id);
      setMembers(detail.members || []);
      setError("");
    } catch (err: unknown) {
      await handleAccessLoss(err, "Could not load organization");
    }
  }, [handleAccessLoss]);

  const handleCreate = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(""); setInfo("");
    try {
      const org = await createOrg(name.trim());
      setInfo(`Organization "${org.name}" created.`);
      setName("");
      await loadOrgs();
      await openOrg(org);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not create the organization");
    } finally {
      setBusy(false);
    }
  };

  // Invitations (audit F01): membership requires the invitee's acceptance —
  // adding someone never silently attaches them (or their history) to the org.
  const handleAdd = async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    setBusy(true); setError(""); setInfo(""); setLastToken("");
    try {
      const invite = await inviteOrgMember(selected.org_id, email.trim(), role);
      setLastToken(invite.token);
      setInfo(`Invitation created for ${invite.email} (${invite.role}). The token below is shown once — share it with them to accept.`);
      setEmail("");
      const detail = await getOrg(selected.org_id);
      setMembers(detail.members || []);
    } catch (err: unknown) {
      await handleAccessLoss(err, "Could not create the invitation");
    } finally {
      setBusy(false);
    }
  };

  const handleAccept = async (e: FormEvent) => {
    e.preventDefault();
    if (!inviteToken.trim()) return;
    setBusy(true); setError(""); setInfo("");
    try {
      const res = await acceptOrgInvitation(inviteToken.trim());
      setInfo(`Invitation accepted — you are now a ${res.role} of the organization.`);
      setInviteToken(""); setShowAccept(false);
      await loadOrgs();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not accept the invitation");
    } finally {
      setBusy(false);
    }
  };

  const myRole = (org: OrgRow) => orgs.find(o => o.org_id === org.org_id)?.role || (org as { role?: string }).role || "";
  const isOwner = selected ? myRole(selected) === "owner" : false;
  const isAdmin = selected ? ["owner", "admin"].includes(myRole(selected)) : false;

  const handleRemove = async (member: OrgMember) => {
    if (!selected) return;
    if (!confirm(`Remove ${member.email || member.user_id.slice(0, 8)} from ${selected.name}?`)) return;
    try {
      await removeOrgMember(selected.org_id, member.user_id);
      setMembers(m => m.filter(x => x.user_id !== member.user_id));
    } catch (err: unknown) {
      await handleAccessLoss(err, "Could not remove the member");
    }
  };

  const handleRole = async (member: OrgMember, newRole: string) => {
    if (!selected) return;
    try {
      await setOrgMemberRole(selected.org_id, member.user_id, newRole);
      setMembers(m => m.map(x => x.user_id === member.user_id ? { ...x, role: newRole } : x));
    } catch (err: unknown) {
      await handleAccessLoss(err, "Could not change the role");
    }
  };

  const handleDelete = async () => {
    if (!selected || !confirm(`Delete "${selected.name}" and its membership? This cannot be undone.`)) return;
    try {
      await deleteOrg(selected.org_id);
      setSelected(null); setMembers([]); setRuns(null);
      setInfo("Organization deleted.");
      await loadOrgs();
    } catch (err: unknown) {
      await handleAccessLoss(err, "Could not delete the organization");
    }
  };

  const loadOversight = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      const res = await getOrgRuns(selected.org_id);
      setRuns(res.runs || []);
      setShowOversight(true);
    } catch (err: unknown) {
      await handleAccessLoss(err, "Could not load run oversight");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="animate-fade-in" data-scroll-reveal style={{ maxWidth: 1000, margin: "0 auto" }}>
      <div style={{ marginBottom: 28 }}>
        <h1 style={{ fontSize: 28, fontWeight: 700, marginBottom: 8 }}>Organizations</h1>
        <p style={{ color: "var(--text-secondary)", lineHeight: 1.5 }}>
          Group your team under one roof. Org admins get read-only oversight of member runs;
          the owner manages roles. Shared vaults are coming next.
        </p>
      </div>

      {error && <div style={{ padding: "12px 16px", background: "var(--error-subtle)", color: "var(--error)", borderRadius: "var(--radius-md)", marginBottom: 20 }}>{error}</div>}
      {info && <div style={{ padding: "12px 16px", background: "var(--success-subtle, rgba(34,197,94,0.12))", color: "var(--success, #22c55e)", borderRadius: "var(--radius-md)", marginBottom: 20 }}>{info}</div>}

      <div className="glass-card panel-3d" style={{ padding: 24, marginBottom: 24 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 14 }}>Create an organization</h2>
        <form onSubmit={handleCreate} style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <input className="input" placeholder="Organization name" value={name} maxLength={120}
            onChange={e => setName(e.target.value)} style={{ maxWidth: 320 }} required />
          <button type="submit" className="btn btn-primary" disabled={busy || !user}>Create</button>
        </form>
      </div>

      <div className="glass-card panel-3d" style={{ padding: 24, marginBottom: 24 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
          <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>Have an invitation?</h2>
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowAccept(v => !v)}>
            {showAccept ? "Hide" : "Enter token"}
          </button>
        </div>
        {showAccept && (
          <form onSubmit={handleAccept} style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <input className="input" placeholder="Paste your invitation token" value={inviteToken}
              onChange={e => setInviteToken(e.target.value)} style={{ maxWidth: 460, fontFamily: "var(--font-mono)", fontSize: 12 }} required />
            <button type="submit" className="btn btn-primary" disabled={busy || !user}>Accept invitation</button>
            <span style={{ fontSize: 12, color: "var(--text-tertiary)" }}>
              Accepting joins the organization with the invited role — your personal work stays yours.
            </span>
          </form>
        )}
      </div>

      {orgs.length === 0 ? (
        <div className="glass-card" style={{ padding: 32, textAlign: "center" }}>
          <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>No organizations yet</h3>
          <p style={{ color: "var(--text-secondary)", fontSize: 14 }}>Create one above, then add teammates by email.</p>
        </div>
      ) : (
        <div className="glass-card" style={{ padding: 24 }}>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: selected ? 0 : 8 }}>
            {orgs.map(o => (
              <button key={o.org_id} type="button"
                className={`btn ${selected?.org_id === o.org_id ? "btn-primary" : "btn-secondary"}`}
                onClick={() => void openOrg(o)}>
                {o.name}
                <span className={`badge ${ROLE_BADGE[myRole(o)] || "badge-neutral"}`} style={{ marginLeft: 8 }}>{myRole(o)}</span>
              </button>
            ))}
          </div>

          {selected && (
            <div style={{ borderTop: "1px solid var(--border-primary)", paddingTop: 20 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14, flexWrap: "wrap", gap: 10 }}>
                <h3 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>{selected.name}</h3>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {isAdmin && (
                    <button type="button" className="btn btn-secondary" onClick={() => void loadOversight()} disabled={busy}>
                      {busy ? "Loading…" : "Run oversight"}
                    </button>
                  )}
                  {isOwner && (
                    <button type="button" className="btn btn-ghost" style={{ color: "var(--error)" }} onClick={() => void handleDelete()}>
                      Delete organization
                    </button>
                  )}
                </div>
              </div>

              {isAdmin && (
                <form onSubmit={handleAdd} style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
                  <input className="input" type="email" required placeholder="teammate@company.com"
                    value={email} onChange={e => setEmail(e.target.value)} style={{ maxWidth: 280 }} />
                  <select className="input" value={role} onChange={e => setRole(e.target.value)} style={{ maxWidth: 140 }}>
                    <option value="admin">admin</option>
                    <option value="member">member</option>
                    <option value="viewer">viewer</option>
                  </select>
                  <button type="submit" className="btn btn-primary" disabled={busy}>Invite</button>
                </form>
              )}
              {isAdmin && lastToken && (
                <div className="glass-panel" style={{ padding: 12, marginBottom: 16, border: "1px solid var(--accent)" }}>
                  <p style={{ margin: "0 0 6px", fontSize: 12, fontWeight: 600 }}>Invitation token — shown once, copy now:</p>
                  <code style={{ fontSize: 11, wordBreak: "break-all", fontFamily: "var(--font-mono)" }}>{lastToken}</code>
                  <p style={{ margin: "6px 0 0", fontSize: 11, color: "var(--text-tertiary)" }}>
                    The invitee signs in and accepts at Settings → Organizations (or POST /orgs/invitations/accept). Membership activates only on acceptance.
                  </p>
                </div>
              )}

              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead>
                    <tr style={{ textAlign: "left", color: "var(--text-tertiary)", fontSize: 12 }}>
                      <th style={{ padding: "8px 12px 8px 0" }}>Member</th>
                      <th style={{ padding: "8px 12px" }}>Role</th>
                      <th style={{ padding: 12 }}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {members.map(m => (
                      <tr key={m.user_id} style={{ borderTop: "1px solid var(--border-primary)" }}>
                        <td style={{ padding: "9px 12px 9px 0" }}>
                          <div style={{ fontWeight: 600 }}>{m.name || m.email || m.user_id.slice(0, 8)}</div>
                          {m.email && <div style={{ fontSize: 12, color: "var(--text-tertiary)" }}>{m.email}</div>}
                        </td>
                        <td style={{ padding: 12 }}>
                          {m.role === "owner" || !isOwner ? (
                            <span className={`badge ${ROLE_BADGE[m.role] || "badge-neutral"}`}>{m.role}</span>
                          ) : (
                            <select className="input" style={{ padding: "4px 8px", fontSize: 12.5 }}
                              value={m.role} onChange={e => void handleRole(m, e.target.value)}
                              aria-label={`Role for ${m.email || m.user_id}`}>
                              <option value="admin">admin</option>
                              <option value="member">member</option>
                              <option value="viewer">viewer</option>
                            </select>
                          )}
                        </td>
                        <td style={{ padding: 12, textAlign: "right" }}>
                          {m.role !== "owner" && isAdmin && m.user_id !== user?.id && (
                            <button type="button" className="btn btn-ghost btn-sm" style={{ color: "var(--error)" }}
                              onClick={() => void handleRemove(m)}>Remove</button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {showOversight && (
                <div style={{ borderTop: "1px solid var(--border-primary)", marginTop: 18, paddingTop: 16 }}>
                  <h4 style={{ fontSize: 14, fontWeight: 600, marginBottom: 10 }}>Member runs (read-only oversight)</h4>
                  {runs && runs.length === 0 && <p style={{ fontSize: 13, color: "var(--text-tertiary)" }}>No runs in this organization yet.</p>}
                  {runs && runs.slice(0, 20).map(r => (
                    <div key={r.run_id} style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "10px 0", borderBottom: "1px solid var(--border-primary)", flexWrap: "wrap" }}>
                      <div style={{ minWidth: 220 }}>
                        <div style={{ fontSize: 13, fontWeight: 600 }}>{r.goal?.slice(0, 80) || r.run_id.slice(0, 12)}</div>
                        <div style={{ fontSize: 12, color: "var(--text-tertiary)" }}>{r.member_email || r.user_id.slice(0, 8)} · {new Date(r.created_at).toLocaleString()}</div>
                      </div>
                      <span className={`badge ${r.status === "COMPLETED" ? "badge-success" : r.status === "FAILED" ? "badge-error" : "badge-info"}`}>{r.status}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
