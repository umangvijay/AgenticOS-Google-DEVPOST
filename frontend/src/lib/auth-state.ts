"use client";

/**
 * Authentication state machine (Phase 18 §18.11) — definitive session
 * rejection is DISTINCT from network/server failure, and an
 * authentication GENERATION identifier prevents cross-account leakage:
 * a response created under an older login can never update a newer
 * account's UI.
 */
import { AUTH_KEY, readSession } from "./auth-session";

export type AuthState =
  | "initializing"
  | "authenticated"
  | "refreshing"
  | "temporarily_unavailable"
  | "expired"
  | "signed_out";

let _generation = 0;

/** Bump on login/logout/account switch. Older generations' async
  responses are STALE — callers compare and discard. */
export function nextAuthGeneration(): number {
  _generation += 1;
  return _generation;
}

export function currentAuthGeneration(): number {
  return _generation;
}

/** Is this response from the CURRENT generation? */
export function isCurrentGeneration(generation: number | undefined | null): boolean {
  return generation === _generation;
}

export function deriveAuthState(): AuthState {
  if (typeof window === "undefined") return "initializing";
  try {
    const raw = localStorage.getItem(AUTH_KEY);
    if (!raw) return "signed_out";
    const session = readSession();
    if (session) return "authenticated";
    return "expired";   // a record exists but no valid session
  } catch {
    return "signed_out";
  }
}

/** §18.11: classifying a failure — definitive rejection (401/403) ends
  the session; network/server failure is TEMPORARY. */
export function classifyAuthFailure(status: number | undefined,
                                    networkError: boolean): {
  state: "expired" | "temporarily_unavailable";
} {
  if (networkError) return { state: "temporarily_unavailable" };
  if (status === 401 || status === 403) return { state: "expired" };
  if (status !== undefined && status >= 500) {
    return { state: "temporarily_unavailable" };
  }
  return { state: "temporarily_unavailable" };
}
