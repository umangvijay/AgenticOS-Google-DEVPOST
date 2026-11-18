import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { readSession, refreshSession, writeSession, AUTH_CHANGED } from "../src/lib/auth-session";
import { apiFetch } from "../src/lib/api";
import type { User } from "../src/lib/auth-context";

const user = { id: "u", name: "Test", role: "user" } as User;
const old = { user, accessToken: "old-access", refreshToken: "old-refresh" };
const next = { user, accessToken: "new-access", refreshToken: "new-refresh" };
const response = () => Response.json({ user, access_token: next.accessToken, refresh_token: next.refreshToken });

beforeEach(() => { localStorage.clear(); sessionStorage.clear(); writeSession(old); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("single-use token rotation", () => {
  it("shares one refresh across concurrent requests and emits a state update", async () => {
    const fetcher = vi.fn(async () => response());
    vi.stubGlobal("fetch", fetcher);
    const changed = vi.fn();
    window.addEventListener(AUTH_CHANGED, changed);
    try {
      expect(await Promise.all(Array.from({ length: 8 }, () => refreshSession(old.accessToken))))
        .toEqual(Array(8).fill(next.accessToken));
      expect(fetcher).toHaveBeenCalledTimes(1);
      expect(changed).toHaveBeenCalledTimes(1);
      expect(readSession()).toEqual(next);
    } finally { window.removeEventListener(AUTH_CHANGED, changed); }
  });

  it("a late unauthorized response cannot clear a newer session", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      writeSession(next);
      return new Response(null, { status: 401 });
    }));
    expect(await refreshSession(old.accessToken)).toBe(next.accessToken);
    expect(readSession()).toEqual(next);
  });

  it("does not undo a logout when refresh finishes", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { writeSession(null); return response(); }));
    expect(await refreshSession(old.accessToken)).toBeNull();
    expect(readSession()).toBeNull();
  });

  it("keeps the session on a transient server failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 503 })));
    await expect(refreshSession(old.accessToken)).rejects.toThrow("temporarily unavailable");
    expect(readSession()).toEqual(old);
  });

  it("reuses an already rotated token instead of rotating again", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    writeSession(next);
    expect(await refreshSession(old.accessToken)).toBe(next.accessToken);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("parses a successful no-content response after refresh", async () => {
    sessionStorage.setItem("agentos_csrf_token", "csrf");
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(null, { status: 401 }))
      .mockResolvedValueOnce(response())
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetcher);
    await expect(apiFetch("/test", { method: "DELETE" })).resolves.toEqual({});
    expect(fetcher).toHaveBeenCalledTimes(3);
  });
});
