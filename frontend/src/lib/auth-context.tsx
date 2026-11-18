"use client";

import React, { createContext, useContext, useState, useEffect, useCallback, ReactNode } from "react";

import { AUTH_KEY, AUTH_CHANGED, readSession, writeSession, refreshSession } from "./auth-session";
import { nextAuthGeneration } from "./auth-state";

export interface User {
  id: string;
  email: string;
  auth_provider: string;
  name: string;
  avatar_url?: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

interface AuthState {
  user: User | null;
  accessToken: string | null;
  refreshToken: string | null;
  isLoading: boolean;
  isAuthenticated: boolean;
}

interface AuthContextType extends AuthState {
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string, name: string) => Promise<void>;
  loginWithGoogle: (idToken?: string, code?: string) => Promise<void>;
  loginAsGuest: (signal?: AbortSignal) => Promise<void>;
  logout: () => Promise<void>;
  refreshAccessToken: () => Promise<string | null>;
  updateProfile: (data: { name?: string; avatar_url?: string }) => Promise<void>;
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function useAuth(): AuthContextType {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}

function parseApiError(data: unknown, fallback: string): string {
  if (!data || typeof data !== "object") return fallback;
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object" && "msg" in item) return String((item as { msg: unknown }).msg);
      return "";
    }).filter(Boolean);
    return parts.join(". ") || fallback;
  }
  return fallback;
}

async function fetchJson(url: string, init: RequestInit, timeoutMs: number) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const parent = init.signal;
  if (parent) {
    if (parent.aborted) controller.abort();
    else parent.addEventListener("abort", () => controller.abort(), { once: true });
  }
  try {
    const res = await fetch(url, { ...init, signal: controller.signal });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(parseApiError(data, `Request failed (${res.status})`));
    }
    return data;
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error("The API took too long to wake up (Cloud Run cold start). Wait a few seconds and try again.");
    }
    if (err instanceof TypeError) {
      throw new Error("Could not reach the API. Wait a few seconds if the service is starting, then try again.");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    user: null,
    accessToken: null,
    refreshToken: null,
    isLoading: true,
    isAuthenticated: false,
  });

  useEffect(() => {
    let cancelled = false;
    const sync = () => {
      const session = readSession();
      if (!cancelled) setState({
        user: session?.user || null, accessToken: session?.accessToken || null,
        refreshToken: session?.refreshToken || null, isLoading: false,
        isAuthenticated: !!session?.accessToken,
      });
    };
    const onStorage = (event: StorageEvent) => { if (!event.key || event.key === AUTH_KEY) sync(); };
    queueMicrotask(sync);
    window.addEventListener(AUTH_CHANGED, sync);
    window.addEventListener("storage", onStorage);
    return () => {
      cancelled = true;
      window.removeEventListener(AUTH_CHANGED, sync);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const persistAuth = useCallback((user: User, accessToken: string, refreshToken: string) => {
    const data = { user, accessToken, refreshToken };
    writeSession(data);
    setState({
      user,
      accessToken,
      refreshToken,
      isLoading: false,
      isAuthenticated: true,
    });
  }, []);

  const clearAuth = useCallback(() => {
    writeSession(null);
    setState({
      user: null,
      accessToken: null,
      refreshToken: null,
      isLoading: false,
      isAuthenticated: false,
    });
  }, []);

  // §18.11/§19 (C7): the generation guard has PRODUCTION callers —
  // login/logout/account switch bump it so stale async responses from
  // an older identity are discarded by consumers.
  const login = useCallback(async (email: string, password: string) => {
    const data = await fetchJson(`${API_BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ email, password }),
    }, 30000);
    nextAuthGeneration();
    persistAuth(data.user, data.access_token, data.refresh_token);
  }, [persistAuth]);

  const signup = useCallback(async (email: string, password: string, name: string) => {
    const data = await fetchJson(`${API_BASE}/auth/signup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ email, password, name }),
    }, 30000);
    nextAuthGeneration();
    persistAuth(data.user, data.access_token, data.refresh_token);
  }, [persistAuth]);

  const loginWithGoogle = useCallback(async (idToken?: string, code?: string) => {
    const body: Record<string, string> = {};
    if (idToken) body.id_token = idToken;
    if (code) body.code = code;

    const data = await fetchJson(`${API_BASE}/auth/google`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(body),
    }, 30000);
    nextAuthGeneration();
    persistAuth(data.user, data.access_token, data.refresh_token);
  }, [persistAuth]);

  const loginAsGuest = useCallback(async (signal?: AbortSignal) => {
    const attempt = () => fetchJson(`${API_BASE}/auth/guest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      signal,
    }, 45000);
    let data;
    try {
      data = await attempt();
    } catch (err) {
      // Guest establishment was observed flaking on cold starts (audit R10):
      // one retry covers the transient window before surfacing an error.
      if (signal?.aborted) throw err;
      await new Promise((r) => setTimeout(r, 1200));
      data = await attempt();
    }
    persistAuth(data.user, data.access_token, data.refresh_token);
  }, [persistAuth]);

  const logout = useCallback(async () => {
    if (state.accessToken) {
      try {
        await fetchJson(`${API_BASE}/auth/logout`, {
          method: "POST",
          headers: { Authorization: `Bearer ${state.accessToken}` },
          credentials: "include",
        }, 5000);
      } catch {
        // Logout locally even if server fails
      }
    }
    nextAuthGeneration();   // stale responses from this identity die here
    clearAuth();
  }, [state.accessToken, clearAuth]);

  const refreshAccessToken = useCallback(async (): Promise<string | null> => {
    return refreshSession(readSession()?.accessToken);
  }, []);

  const updateProfile = useCallback(async (updates: { name?: string; avatar_url?: string }) => {
    const user = await fetchJson(`${API_BASE}/auth/me`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${state.accessToken}`,
      },
      credentials: "include",
      body: JSON.stringify(updates),
    }, 8000);
    setState((s) => ({
      ...s,
      user: { ...s.user!, ...user },
    }));
  }, [state.accessToken]);

  return (
    <AuthContext.Provider
      value={{
        ...state,
        login,
        signup,
        loginWithGoogle,
        loginAsGuest,
        logout,
        refreshAccessToken,
        updateProfile,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}
