"use client";

/**
 * Cross-tab refresh mutual exclusion (Phase 18 §18.12, finding F9).
 *
 * With Web Locks: the browser guarantees single ownership.
 * WITHOUT Web Locks: a localStorage lock with an expiry — the
 * single-use refresh token is NEVER rotated by two tabs (finding F9's
 * defect was the no-Locks path racing). The fallback is best-effort
 * mutual exclusion: one tab owns the refresh window; others wait
 * briefly then RE-READ the session (which the owning tab has rotated)
 * instead of rotating again.
 */
import { readSession } from "./auth-session";

const LOCK_KEY = "agentos.refresh-lock";
const LOCK_TTL_MS = 10_000;

interface LockHandle {
  release: () => void;
}

function webLocksAvailable(): boolean {
  return typeof navigator !== "undefined" &&
    "locks" in navigator && !!(navigator as Navigator & { locks?: unknown }).locks;
}

function tryAcquireFallbackLock(owner: string): LockHandle | null {
  try {
    const raw = localStorage.getItem(LOCK_KEY);
    if (raw) {
      const existing = JSON.parse(raw) as { owner: string; expires: number };
      if (existing.expires > Date.now() && existing.owner !== owner) {
        return null;   // another tab holds a live lock
      }
    }
    localStorage.setItem(LOCK_KEY, JSON.stringify({
      owner, expires: Date.now() + LOCK_TTL_MS,
    }));
    return {
      release: () => {
        try {
          const cur = localStorage.getItem(LOCK_KEY);
          if (cur) {
            const parsed = JSON.parse(cur) as { owner: string };
            if (parsed.owner === owner) localStorage.removeItem(LOCK_KEY);
          }
        } catch { /* best effort */ }
      },
    };
  } catch {
    return null;   // storage unavailable: callers degrade to re-read
  }
}

export interface RefreshCoordination {
  /** This tab owns the refresh and must rotate. */
  owns: boolean;
  release: () => void;
}

export async function acquireRefreshLock(): Promise<RefreshCoordination> {
  const owner = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  if (webLocksAvailable()) {
    const navigatorLocks = (navigator as Navigator & {
      locks: { request: (name: string, cb: (lock: unknown) => Promise<void>) => Promise<void> };
    }).locks;
    let releaseFn: (() => void) | null = null;
    try {
      await navigatorLocks.request(LOCK_KEY, async () => {
        await new Promise<void>((resolve) => {
          releaseFn = resolve;   // released when the caller finishes
        });
      });
    } catch {
      releaseFn = null;
    }
    if (releaseFn) {
      return { owns: true, release: releaseFn };
    }
    return { owns: false, release: () => {} };
  }
  // §18.12: the no-Web-Locks fallback — a localStorage lock with TTL.
  const lock = tryAcquireFallbackLock(owner);
  if (lock) return { owns: true, release: lock.release };
  return { owns: false, release: () => {} };
}

/** The safe no-ownership path: WAIT briefly for the owner to finish,
  then RE-READ the (possibly rotated) session instead of rotating the
  single-use token again. Returns the freshest access token or null. */
export async function waitForPeerRefresh(maxWaitMs = 4000): Promise<string | null> {
  const deadline = Date.now() + maxWaitMs;
  let latest = readSession();
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 300));
    const current = readSession();
    if (current?.accessToken !== latest?.accessToken) {
      return current?.accessToken ?? null;   // the owner rotated for us
    }
    latest = current;
  }
  return latest?.accessToken ?? null;
}
