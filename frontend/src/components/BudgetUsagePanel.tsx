"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getBudgetSnapshot, BudgetSnapshot } from "@/lib/api";
import { AUTH_CHANGED } from "@/lib/auth-session";
import { useNetworkStatus } from "@/hooks/useNetworkStatus";

/**
 * Authoritative budget panel (Phase 2, §2.16).
 *
 * Shows the accounting equation explicitly — daily allowance, settled
 * consumption, active reservations (incl. the uncertain subset), available
 * allowance, reset time in the user's timezone, measurement quality, and the
 * accounting service status. Reads GET /usage/budget: when accounting is
 * unavailable it shows an explicit unavailable state, NEVER a plausible zero.
 */

function formatTokens(n: number | null | undefined) {
  if (n == null) return "—";
  if (Math.abs(n) >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}K`;
  return String(n);
}

function resetLabel(period: string | null | undefined) {
  if (!period) return "—";
  // The accounting period is a UTC day (§2.14); display the reset in the
  // user's timezone and say so.
  const [y, m, d] = period.split("-").map(Number);
  if (!y || !m || !d) return "—";
  const reset = new Date(Date.UTC(y, m - 1, d + 1, 0, 0, 0));
  return reset.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export default function BudgetUsagePanel({ refreshMs = 30000 }: { refreshMs?: number }) {
  const [snapshot, setSnapshot] = useState<BudgetSnapshot | null>(null);
  const [error, setError] = useState("");
  const [lastRefresh, setLastRefresh] = useState<number | null>(null);
  const { online } = useNetworkStatus();

  // §28.7 reconciliation fencing: every load takes a monotonically
  // increasing token; a LATE response (an older request resolving
  // after a newer one) never overwrites newer state. This covers the
  // disconnect → refresh and account-switch races — an in-flight
  // response fetched under the PREVIOUS principal is discarded when
  // the switch triggers a newer load.
  const loadTokenRef = useRef(0);
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const load = useCallback(async () => {
    const token = ++loadTokenRef.current;
    try {
      // (set-state-in-effect) no synchronous setState when called from
      // the effect: the stale-error clear happens WITH the new data.
      const fresh = await getBudgetSnapshot();
      if (!aliveRef.current || token !== loadTokenRef.current) return;
      setSnapshot(fresh);
      setError("");
      setLastRefresh(Date.now());
    } catch (e) {
      if (!aliveRef.current || token !== loadTokenRef.current) return;
      setError(e instanceof Error ? e.message : "Budget information is temporarily unavailable");
    }
  }, []);

  useEffect(() => {
    // (set-state-in-effect) the first fetch is deferred off the effect's
    // synchronous window; the interval boundary is already async.
    queueMicrotask(load);
    const id = setInterval(load, refreshMs);
    return () => { if (id) clearInterval(id); };
  }, [load, refreshMs]);

  // §27.D6/§28.7: actual CONNECTIVITY is the reconciliation trigger —
  // after a disconnect the panel reloads immediately from the ledger
  // instead of waiting for the next interval tick.
  const onlineRef = useRef(online);
  useEffect(() => {
    const recovered = online && !onlineRef.current;
    onlineRef.current = online;
    if (recovered) void queueMicrotask(load);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [online]);

  // §28.7: an account switch reloads the panel for the NEW principal;
  // the token guard discards any in-flight response of the old one.
  useEffect(() => {
    const onChange = () => { void queueMicrotask(load); };
    window.addEventListener(AUTH_CHANGED, onChange);
    return () => window.removeEventListener(AUTH_CHANGED, onChange);
  }, [load]);

  const unavailable = snapshot?.accounting_status === "unavailable";
  const limit = snapshot?.limit ?? null;
  const consumed = snapshot?.consumed ?? null;
  const reserved = snapshot?.reserved ?? null;
  const uncertain = snapshot?.uncertain_reserved ?? null;
  const available = snapshot?.available ?? null;
  const pct = limit && limit > 0 && consumed != null && reserved != null
    ? Math.min(100, Math.round(((consumed + reserved) / limit) * 100))
    : null;

  if (error) {
    return (
      <div className="glass-card" data-testid="budget-panel" style={{ padding: 20, marginBottom: 24 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: "0 0 10px" }}>Token budget</h2>
        <p style={{ fontSize: 13, color: "var(--warm)", margin: 0 }}>
          Usage information is temporarily unavailable. New paid work is paused.
        </p>
      </div>
    );
  }

  return (
    <div className="glass-card" data-testid="budget-panel" style={{ padding: 20, marginBottom: 24 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 4 }}>
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>Token budget</h2>
        <span style={{ fontSize: 11.5, color: "var(--text-tertiary)", fontFamily: "var(--font-mono)" }}>
          {snapshot?.as_of && lastRefresh ? `updated ${new Date(lastRefresh).toLocaleTimeString()}` : ""}
        </span>
      </div>
      <p style={{ fontSize: 12, color: "var(--text-tertiary)", margin: "0 0 14px" }}>
        Measured provider usage — distinct from the context-window estimate shown elsewhere.
      </p>

      {unavailable ? (
        <div style={{
          padding: "10px 14px", borderRadius: "var(--radius-md)", fontSize: 12.5, lineHeight: 1.5,
          border: "1px solid color-mix(in srgb, var(--warm) 45%, transparent)",
          background: "color-mix(in srgb, var(--warm) 8%, transparent)",
          color: "var(--text-secondary)",
        }}>
          {snapshot?.message || "Usage information is temporarily unavailable. New paid work is paused."}
        </div>
      ) : (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12, marginBottom: 14 }}>
            <div style={{ padding: 12, borderRadius: "var(--radius-md)", background: "var(--bg-tertiary)", border: "1px solid var(--border-primary)" }}>
              <div style={{ fontSize: 11, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Daily allowance</div>
              <div style={{ fontSize: 15, fontWeight: 600, fontFamily: "var(--font-mono)" }}>{formatTokens(limit)}</div>
            </div>
            <div style={{ padding: 12, borderRadius: "var(--radius-md)", background: "var(--bg-tertiary)", border: "1px solid var(--border-primary)" }}>
              <div style={{ fontSize: 11, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Settled consumption</div>
              <div style={{ fontSize: 15, fontWeight: 600, fontFamily: "var(--font-mono)" }}>{formatTokens(consumed)}</div>
            </div>
            <div style={{ padding: 12, borderRadius: "var(--radius-md)", background: "var(--bg-tertiary)", border: "1px solid var(--border-primary)" }}>
              <div style={{ fontSize: 11, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Active reservations</div>
              <div style={{ fontSize: 15, fontWeight: 600, fontFamily: "var(--font-mono)" }}>
                {formatTokens(reserved)}
                {uncertain ? (
                  <span style={{ fontSize: 11.5, fontWeight: 400, color: "var(--warm)", marginLeft: 6 }}>
                    ({formatTokens(uncertain)} uncertain)
                  </span>
                ) : null}
              </div>
            </div>
            <div style={{ padding: 12, borderRadius: "var(--radius-md)", background: "var(--bg-tertiary)", border: "1px solid var(--border-primary)" }}>
              <div style={{ fontSize: 11, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Available</div>
              <div style={{
                fontSize: 15, fontWeight: 600, fontFamily: "var(--font-mono)",
                color: available != null && available < 0 ? "var(--error, #ef4444)" : undefined,
              }}>{formatTokens(available)}</div>
            </div>
          </div>

          {pct != null && (
            <>
              <div style={{ height: 8, borderRadius: 6, background: "var(--bg-tertiary)", overflow: "hidden", border: "1px solid var(--border-primary)" }}>
                <div style={{
                  width: `${Math.max(1, pct)}%`, height: "100%",
                  background: pct > 85 ? "var(--error, #ef4444)" : "var(--forge-grad, var(--accent))",
                  borderRadius: 6, transition: "width .6s ease",
                }} />
              </div>
              <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: "var(--text-tertiary)", marginTop: 6 }}>
                <span>{pct}% committed (settled + reservations)</span>
                <span>Resets {resetLabel(snapshot?.period)} · UTC accounting</span>
              </div>
            </>
          )}

          <p style={{ margin: "10px 0 0", fontSize: 11, color: "var(--text-tertiary)" }}>
            Measurement quality: provider-reported usage where available, size-based estimates otherwise
            {snapshot?.uncertain_reserved ? " · some reservations are awaiting outcome verification" : ""}
            {snapshot?.policy_version ? ` · policy ${snapshot.policy_version}` : ""}
          </p>

          {/* 30.32: the PROVIDER rate limit is a distinct quantity with its
              own source; absent provider data renders as explicitly
              unavailable, never a guessed reset. */}
          <p
            data-testid="provider-rate-limit"
            style={{ margin: "6px 0 0", fontSize: 11, color: "var(--text-tertiary)" }}
          >
            {snapshot?.provider_rate_limit
              ? `Provider rate limit (${snapshot.provider_rate_limit.provider}, from ${snapshot.provider_rate_limit.source.replace("provider_", "")})` +
                `${snapshot.provider_rate_limit.remaining != null && snapshot.provider_rate_limit.limit != null
                  ? `: ${snapshot.provider_rate_limit.remaining}/${snapshot.provider_rate_limit.limit} requests remaining`
                  : ": limit details not reported"}` +
                `${snapshot.provider_rate_limit.reset_at
                  ? ` · resets ${new Date(snapshot.provider_rate_limit.reset_at).toLocaleString()}`
                  : " · reset not reported"}`
              : "Provider rate limit: not reported by the provider"}
          </p>
        </>
      )}
    </div>
  );
}
