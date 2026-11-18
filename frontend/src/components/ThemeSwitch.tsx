"use client";

import { useSyncExternalStore } from "react";
import { Moon, Sun } from "lucide-react";
import { applyTheme } from "@/lib/theme";

function subscribe(notify: () => void) {
  const observer = new MutationObserver(notify);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => observer.disconnect();
}

export default function ThemeSwitch() {
  const dark = useSyncExternalStore(subscribe, () => document.documentElement.dataset.theme === "dark", () => false);
  return <button type="button" className="theme-switch" aria-label={`Switch to ${dark ? "day" : "night"} theme`} aria-pressed={dark} onClick={() => applyTheme(dark ? "light" : "dark")}>
    {dark ? <Moon size={15} /> : <Sun size={15} />}<span>{dark ? "NIGHT" : "DAY"}</span>
  </button>;
}
