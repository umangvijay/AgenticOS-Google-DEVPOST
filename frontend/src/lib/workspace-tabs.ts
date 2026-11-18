"use client";

/**
 * §28.7.4 — the workspace dock store.
 *
 * Persisted layout state for the resource dock: tab order, active tab,
 * dock width, floating bounds. Keyed by signed-in principal + workspace
 * thread so two accounts never share presentation state.
 *
 * SAFETY CONTRACT: only safe IDENTIFIERS are persisted — session IDs,
 * artifact IDs + revisions, qualified URL identities, run IDs. Never a
 * raw credential, signed URL, private thumbnail or browser frame.
 * Resource ACCESS is revalidated from the server after reload; a
 * persisted tab whose resource is no longer authorized renders as
 * "unavailable" and can be closed.
 */

export type DockTabKind = "browser" | "artifact" | "pdf" | "text" | "table" | "model" | "url";

export interface DockTab {
  id: string;                    // stable identity
  kind: DockTabKind;
  title: string;
  /** The originating run — prior-turn artifacts stay openable. */
  runId?: string;
  /** browser: session id; artifact/pdf/model: artifact id; url: canonical url. */
  resourceId: string;
  /** artifact revision, when the resource is versioned. */
  revision?: string;
  /** For url tabs: the qualified canonical identity (not the raw signed URL). */
  url?: string;
  filePath?: string;
}

export interface DockLayout {
  tabs: DockTab[];
  activeId: string | null;
  dockWidth: number;
  floating: boolean;
  floatingBounds: { x: number; y: number; w: number; h: number } | null;
}

const LAYOUT_VERSION = 1;

function storageKey(principal: string, threadId: string): string {
  return `agentos.dock.v${LAYOUT_VERSION}.${principal}.${threadId}`;
}

export function defaultLayout(): DockLayout {
  return { tabs: [], activeId: null, dockWidth: 460, floating: false, floatingBounds: null };
}

export function loadLayout(principal: string, threadId: string): DockLayout {
  if (typeof window === "undefined") return defaultLayout();
  try {
    const raw = window.localStorage.getItem(storageKey(principal, threadId));
    if (!raw) return defaultLayout();
    const parsed = JSON.parse(raw) as DockLayout;
    if (!parsed || !Array.isArray(parsed.tabs)) return defaultLayout();
    // Defensive: sanitize anything that should never have been persisted.
    const tabs = parsed.tabs.filter(
      (t) => t && typeof t.id === "string" && typeof t.kind === "string" &&
             typeof t.resourceId === "string");
    return {
      tabs,
      activeId: typeof parsed.activeId === "string" ? parsed.activeId : null,
      dockWidth: typeof parsed.dockWidth === "number" ? parsed.dockWidth : 460,
      floating: parsed.floating === true,
      floatingBounds: parsed.floatingBounds ?? null,
    };
  } catch {
    return defaultLayout();
  }
}

export function saveLayout(principal: string, threadId: string, layout: DockLayout): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey(principal, threadId),
                                JSON.stringify(layout));
  } catch {
    // Storage unavailable — layout simply does not persist.
  }
}

export function clearPrincipalLayout(principal: string): void {
  if (typeof window === "undefined") return;
  try {
    const doomed: string[] = [];
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i);
      if (key && key.startsWith(`agentos.dock.v${LAYOUT_VERSION}.${principal}.`)) {
        doomed.push(key);
      }
    }
    doomed.forEach((key) => window.localStorage.removeItem(key));
  } catch {
    // ignore
  }
}

export function openTab(layout: DockLayout, tab: DockTab): DockLayout {
  // Reopening an existing resource focuses the existing tab (one tab per
  // resource identity per kind).
  const existing = layout.tabs.find(
    (t) => t.kind === tab.kind && t.resourceId === tab.resourceId &&
           (t.kind !== "artifact" || t.revision === tab.revision));
  if (existing) {
    return { ...layout, activeId: existing.id };
  }
  const tabs = [...layout.tabs, tab];
  return { ...layout, tabs, activeId: tab.id };
}

export function closeTab(layout: DockLayout, tabId: string): DockLayout {
  const index = layout.tabs.findIndex((t) => t.id === tabId);
  if (index === -1) return layout;
  const tabs = layout.tabs.filter((t) => t.id !== tabId);
  let activeId = layout.activeId;
  if (activeId === tabId) {
    // Closing the active tab focuses a NEIGHBOUR, else the opener.
    activeId = tabs[Math.min(index, tabs.length - 1)]?.id ?? null;
  }
  return { ...layout, tabs, activeId };
}

export function activateTab(layout: DockLayout, tabId: string): DockLayout {
  return { ...layout, activeId: tabId };
}

export function moveTab(layout: DockLayout, tabId: string, delta: -1 | 1): DockLayout {
  const index = layout.tabs.findIndex((t) => t.id === tabId);
  const target = index + delta;
  if (index === -1 || target < 0 || target >= layout.tabs.length) return layout;
  const tabs = [...layout.tabs];
  [tabs[index], tabs[target]] = [tabs[target], tabs[index]];
  return { ...layout, tabs };
}

export function setDockWidth(layout: DockLayout, width: number): DockLayout {
  return { ...layout, dockWidth: Math.min(760, Math.max(300, width)) };
}

export function setFloating(layout: DockLayout, floating: boolean,
                            bounds?: DockLayout["floatingBounds"]): DockLayout {
  return { ...layout, floating, floatingBounds: bounds ?? layout.floatingBounds };
}

export function setFloatingBounds(layout: DockLayout,
                                  bounds: DockLayout["floatingBounds"]): DockLayout {
  return { ...layout, floatingBounds: bounds };
}
