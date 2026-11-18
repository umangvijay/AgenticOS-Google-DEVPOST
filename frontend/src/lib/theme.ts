/**
 * Sandtable theme resolution — light / dark / system.
 *
 * `system` follows the OS via prefers-color-scheme and live-updates when
 * the OS flips. One resolver for the first-paint script, the settings UI,
 * and the dashboard layout; the preference persists in localStorage.
 */

export type ThemePref = "light" | "dark" | "system";
const KEY = "agentos_theme";

export function readThemePref(): ThemePref {
  if (typeof window === "undefined") return "system";
  try {
    const stored = window.localStorage.getItem(KEY) as ThemePref | null;
    return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
  } catch {
    return "system";
  }
}

export function resolveTheme(pref: ThemePref): "light" | "dark" {
  if (pref !== "system") return pref;
  if (typeof window === "undefined" || !window.matchMedia) return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(pref: ThemePref): "light" | "dark" {
  const resolved = resolveTheme(pref);
  document.documentElement.setAttribute("data-theme", resolved);
  try { window.localStorage.setItem(KEY, pref); } catch { /* storage may be read-only */ }
  return resolved;
}

/** Live-follow OS changes while pref === "system". Called once by the provider. */
export function watchSystemTheme(): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onChange = () => {
    if (readThemePref() === "system") {
      document.documentElement.setAttribute("data-theme", mq.matches ? "dark" : "light");
    }
  };
  mq.addEventListener?.("change", onChange);
  return () => mq.removeEventListener?.("change", onChange);
}
