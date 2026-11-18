import { describe, it, expect, beforeEach, vi } from "vitest";
import { readThemePref, resolveTheme, applyTheme } from "../src/lib/theme";

describe("theme resolution (light/dark/system)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("defaults to system when nothing stored", () => {
    expect(readThemePref()).toBe("system");
  });

  it("resolves system from prefers-color-scheme", () => {
    vi.stubGlobal("matchMedia", vi.fn().mockImplementation((q: string) => ({ matches: q.includes("dark") })));
    expect(resolveTheme("system")).toBe("dark");
    vi.stubGlobal("matchMedia", vi.fn().mockImplementation(() => ({ matches: false })));
    expect(resolveTheme("system")).toBe("light");
    expect(resolveTheme("light")).toBe("light");
    expect(resolveTheme("dark")).toBe("dark");
  });

  it("keeps the theme switch usable when browser storage is blocked", () => {
    vi.stubGlobal("localStorage", {
      getItem: () => { throw new Error("Storage blocked"); },
      setItem: () => { throw new Error("Storage blocked"); },
    });
    expect(readThemePref()).toBe("system");
    expect(applyTheme("dark")).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("applyTheme persists the pref and stamps data-theme", () => {
    vi.stubGlobal("matchMedia", vi.fn().mockImplementation(() => ({ matches: true })));
    const resolved = applyTheme("system");
    expect(resolved).toBe("dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(window.localStorage.getItem("agentos_theme")).toBe("system");
    applyTheme("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });
});
