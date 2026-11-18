import type { User } from "./auth-context";

export const AUTH_KEY = "agentos_auth";
export const AUTH_CHANGED = "agentos:auth-changed";
export interface Session { user: User; accessToken: string; refreshToken: string }

export function readSession(): Session | null {
  if (typeof window === "undefined") return null;
  try {
    const value = JSON.parse(localStorage.getItem(AUTH_KEY) || "null");
    return value?.accessToken && value?.refreshToken && value?.user ? value : null;
  } catch { return null; }
}

export function writeSession(session: Session | null) {
  if (session) localStorage.setItem(AUTH_KEY, JSON.stringify(session));
  else localStorage.removeItem(AUTH_KEY);
  window.dispatchEvent(new Event(AUTH_CHANGED));
}

let refreshing: Promise<string | null> | null = null;

/** Single-use refresh tokens are serialized within a tab and across Web Locks tabs. */
export async function refreshSession(expectedAccessToken?: string | null): Promise<string | null> {
  const current = readSession();
  if (!current) return null;
  if (expectedAccessToken && current.accessToken !== expectedAccessToken) return current.accessToken;
  if (refreshing) return refreshing;
  const observed = current;
  const rotate = async () => {
    const latest = readSession();
    if (!latest) return null;
    if (latest.refreshToken !== observed.refreshToken) return latest.accessToken;
    const base = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
    const res = await fetch(`${base}/auth/refresh`, {
      method: "POST", credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: latest.refreshToken }),
      signal: AbortSignal.timeout(15000),
    });
    // A logout, login or another tab's refresh may finish while the request is in flight.
    if (readSession()?.refreshToken !== latest.refreshToken) return readSession()?.accessToken || null;
    if (!res.ok) {
      if (res.status === 401 || res.status === 403) {
        writeSession(null);
        return null;
      }
      throw new Error("Session refresh is temporarily unavailable. Please retry.");
    }
    const data = await res.json();
    if (!data.access_token || !data.refresh_token || !data.user) throw new Error("Invalid refresh response");
    if (readSession()?.refreshToken !== latest.refreshToken) return readSession()?.accessToken || null;
    writeSession({ user: data.user, accessToken: data.access_token, refreshToken: data.refresh_token });
    return data.access_token as string;
  };
  refreshing = (async () => {
    if (typeof navigator !== "undefined" && navigator.locks) {
      return await navigator.locks.request("agentos-session-refresh", rotate);
    }
    // §18.12 (finding F9): WITHOUT Web Locks, the cross-tab LOCK
    // coordinates ownership — a non-owner WAITS and re-reads the
    // (possibly rotated) session instead of rotating the same
    // single-use refresh token (the old path raced).
    const { acquireRefreshLock, waitForPeerRefresh } =
      await import("./refresh-lock");
    const coordination = await acquireRefreshLock();
    if (!coordination.owns) {
      coordination.release();
      return await waitForPeerRefresh();
    }
    try {
      return await rotate();
    } finally {
      coordination.release();
    }
  })();
  try { return await refreshing; }
  finally { refreshing = null; }
}
