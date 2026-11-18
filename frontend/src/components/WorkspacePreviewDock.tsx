"use client";

/**
 * §28.7.5/§28.7.6 — the workspace preview dock.
 *
 * Renders ordered browser / PDF / artifact / text / model tabs to the
 * right of the chat. One logical browser panel moves between the dock
 * and an in-app floating window without losing its frame subscription or
 * controller identity. Keyboard: arrows/Home/End select tabs, closing
 * the active tab focuses a neighbour.
 *
 * Distinct actions with distinct outcomes:
 * - Close tab    → presentation only (the resource itself is untouched).
 * - Stop browser → the session close route (the browser work ends).
 * - Cancel run   → the workflow command (handled by the chat surface).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  activateTab, closeTab, DockLayout, DockTab, loadLayout, saveLayout,
  setDockWidth, setFloating, setFloatingBounds,
} from "@/lib/workspace-tabs";

export type { DockTab };
// Named re-exports for page-level composition.
export { closeTab as closeDockTab };
import { BrowserSessionPanel } from "@/components/BrowserSessionPanel";

export interface WorkspacePreviewDockProps {
  principal: string;
  threadId: string;
  layout: DockLayout;
  onLayoutChange: (layout: DockLayout) => void;
  /** Close (presentation) vs stop (session close) disambiguation. */
  onBrowserStop?: (sessionId: string) => void;
  /** Fetch an artifact file URL through the authenticated client. */
  artifactFileUrl: (artifactId: string, path: string) => string;
  onRevalidateResource?: (tab: DockTab) => Promise<boolean>;
  smallScreen?: boolean;
}

export function WorkspacePreviewDock(props: WorkspacePreviewDockProps) {
  const { principal, threadId, layout, onLayoutChange } = props;
  const [unauthorized] = useState<Record<string, boolean>>({});
  const tablistRef = useRef<HTMLDivElement>(null);

  // Persist ONLY safe identifiers; access is revalidated server-side.
  useEffect(() => {
    saveLayout(principal, threadId, layout);
  }, [principal, threadId, layout]);

  const active = useMemo(
    () => layout.tabs.find((t) => t.id === layout.activeId) ?? null,
    [layout]);

  const handleKey = useCallback((event: React.KeyboardEvent) => {
    if (!layout.tabs.length) return;
    const index = layout.tabs.findIndex((t) => t.id === layout.activeId);
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      event.preventDefault();
      const next = layout.tabs[Math.min(index + 1, layout.tabs.length - 1)];
      onLayoutChange(activateTab(layout, next.id));
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      const prev = layout.tabs[Math.max(index - 1, 0)];
      onLayoutChange(activateTab(layout, prev.id));
    } else if (event.key === "Home") {
      event.preventDefault();
      onLayoutChange(activateTab(layout, layout.tabs[0].id));
    } else if (event.key === "End") {
      event.preventDefault();
      onLayoutChange(activateTab(layout, layout.tabs[layout.tabs.length - 1].id));
    }
  }, [layout, onLayoutChange]);

  const close = useCallback((tabId: string) => {
    onLayoutChange(closeTab(layout, tabId));
  }, [layout, onLayoutChange]);

  const resizeStart = useCallback((startEvent: React.PointerEvent) => {
    if (layout.floating || props.smallScreen) return;
    const startX = startEvent.clientX;
    const startWidth = layout.dockWidth;
    const onMove = (moveEvent: PointerEvent) => {
      onLayoutChange(setDockWidth(layout, startWidth - (moveEvent.clientX - startX)));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }, [layout, onLayoutChange, props.smallScreen]);

  if (!layout.tabs.length) return null;

  const tabButton = (tab: DockTab) => (
    <button
      key={tab.id}
      type="button"
      role="tab"
      aria-selected={tab.id === layout.activeId}
      tabIndex={tab.id === layout.activeId ? 0 : -1}
      data-testid={`dock-tab-${tab.kind}`}
      onClick={() => onLayoutChange(activateTab(layout, tab.id))}
      onKeyDown={handleKey}
      style={{
        padding: "6px 10px", fontSize: 12, cursor: "pointer",
        background: tab.id === layout.activeId ? "var(--bg-card)" : "transparent",
        color: tab.id === layout.activeId ? "var(--text-primary)" : "var(--text-secondary)",
        border: "1px solid var(--border-primary)",
        borderBottom: tab.id === layout.activeId ? "none" : "1px solid var(--border-primary)",
        borderRadius: "8px 8px 0 0",
        display: "inline-flex", alignItems: "center", gap: 6,
        maxWidth: 170, overflow: "hidden", textOverflow: "ellipsis",
        whiteSpace: "nowrap",
      }}
    >
      <span aria-hidden>{iconFor(tab.kind)}</span>
      <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>
        {tab.title}
      </span>
      <span
        role="button"
        aria-label={`Close ${tab.title}`}
        tabIndex={0}
        onClick={(event) => { event.stopPropagation(); close(tab.id); }}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.stopPropagation();
            close(tab.id);
          }
        }}
        style={{ color: "var(--text-tertiary)", padding: "0 2px" }}
      >
        ✕
      </span>
    </button>
  );

  const body = () => {
    if (!active) return null;
    if (unauthorized[active.id]) {
      return (
        <div style={{ padding: 18, fontSize: 13, color: "var(--text-secondary)" }}>
          This resource is no longer available for your account.
          <button
            type="button"
            className="btn"
            style={{ marginLeft: 10 }}
            onClick={() => close(active.id)}
          >
            Close tab
          </button>
        </div>
      );
    }
    if (active.kind === "browser") {
      return <BrowserSessionPanel sessionId={active.resourceId} />;
    }
    if (active.kind === "pdf") {
      return <PdfTabView key={active.id} tab={active} />;
    }
    if (active.kind === "model") {
      return (
        <div style={{ padding: 12, fontSize: 12 }}>
          <div style={{ marginBottom: 6, color: "var(--text-secondary)" }}>
            3D model asset — {active.title}
          </div>
          <a href={props.artifactFileUrl(active.resourceId, active.filePath || "model.obj")}
             target="_blank" rel="noopener noreferrer" className="btn">
            Download {active.filePath || "model.obj"}
          </a>
        </div>
      );
    }
    // text/table/url fallback: iframe the authenticated URL.
    return (
      <iframe
        title={active.title}
        src={active.url || props.artifactFileUrl(active.resourceId, active.filePath || "")}
        style={{ width: "100%", height: "100%", minHeight: 300, border: "none" }}
        sandbox="allow-same-origin"
      />
    );
  };

  const dockShell = (children: React.ReactNode) => (
    <div
      data-testid="workspace-preview-dock"
      style={{
        display: "flex", flexDirection: "column",
        width: layout.floating && layout.floatingBounds ? layout.floatingBounds.w : undefined,
        height: layout.floating && layout.floatingBounds ? layout.floatingBounds.h : undefined,
        border: "1px solid var(--border-primary)",
        borderRadius: 12, overflow: "hidden",
        background: "var(--bg-secondary)",
      }}
    >
      <div ref={tablistRef} role="tablist" aria-label="Workspace resources"
           tabIndex={0}
           onKeyDown={handleKey}
           style={{ display: "flex", gap: 2, padding: "6px 8px 0", overflowX: "auto" }}>
        {layout.tabs.map(tabButton)}
        <button
          type="button"
          aria-label={layout.floating ? "Redock panel" : "Float panel"}
          data-testid="dock-float-toggle"
          onClick={() => onLayoutChange(setFloating(layout, !layout.floating))}
          style={{
            marginLeft: "auto", background: "none",
            border: "1px solid var(--border-primary)", borderRadius: 6,
            color: "var(--text-secondary)", fontSize: 12, padding: "4px 8px",
            cursor: "pointer",
          }}
        >
          {layout.floating ? "⇲ Redock" : "⇱ Float"}
        </button>
      </div>
      <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
        {children}
      </div>
    </div>
  );

  if (layout.floating && layout.floatingBounds) {
    const bounds = layout.floatingBounds;
    return (
      <div style={{ position: "fixed", left: bounds.x, top: bounds.y, zIndex: 900 }}>
        {dockShell(
          <>
            <div
              style={{
                position: "absolute", top: -14, right: 10, width: 28, height: 28,
                background: "var(--bg-card)", border: "1px solid var(--border-primary)",
                borderRadius: 6, cursor: "grab",
              }}
              role="separator"
              aria-label="Move floating panel"
              tabIndex={0}
              onPointerDown={(event) => {
                const startX = event.clientX - bounds.x;
                const startY = event.clientY - bounds.y;
                const onMove = (move: PointerEvent) => {
                  onLayoutChange(setFloatingBounds(layout, {
                    ...bounds,
                    x: Math.max(4, move.clientX - startX),
                    y: Math.max(4, move.clientY - startY),
                  }));
                };
                const onUp = () => {
                  window.removeEventListener("pointermove", onMove);
                  window.removeEventListener("pointerup", onUp);
                };
                window.addEventListener("pointermove", onMove);
                window.addEventListener("pointerup", onUp);
              }}
            />
            {body()}
          </>
        )}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", height: "100%", minHeight: 320 }}>
      {!props.smallScreen && (
        <div
          role="separator"
          aria-label="Resize dock"
          aria-orientation="vertical"
          tabIndex={0}
          data-testid="dock-resizer"
          onPointerDown={resizeStart}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              onLayoutChange(setDockWidth(layout, layout.dockWidth + 24));
            } else if (event.key === "ArrowRight") {
              event.preventDefault();
              onLayoutChange(setDockWidth(layout, layout.dockWidth - 24));
            }
          }}
          style={{
            width: 6, cursor: "col-resize", background: "transparent",
            borderLeft: "2px solid var(--border-primary)",
          }}
        />
      )}
      <div style={{ width: props.smallScreen ? "100%" : layout.dockWidth, minWidth: 0 }}>
        {dockShell(body())}
      </div>
    </div>
  );
}

function iconFor(kind: DockTab["kind"]): string {
  return {
    browser: "Browser", artifact: "Files", pdf: "PDF", text: "Text",
    table: "Table", model: "Model", url: "Link",
  }[kind] ?? "Resource";
}

export function useDockLayout(principal: string, threadId: string) {
  const currentKey = `${principal}.${threadId}`;
  const [state, setState] = useState<{ key: string; layout: DockLayout }>(
    () => ({ key: currentKey, layout: defaultLayoutSafe(principal, threadId) }));
  // The sanctioned prop-derived state adjustment: re-load when the
  // principal/thread changes — during render, never in an effect.
  if (state.key !== currentKey) {
    setState({ key: currentKey, layout: loadLayout(principal, threadId) });
  }
  const change = useCallback(
    (next: DockLayout) => setState({ key: `${principal}.${threadId}`, layout: next }),
    [principal, threadId]);
  return [state.layout, change] as const;
}

function defaultLayoutSafe(principal: string, threadId: string): DockLayout {
  if (!principal || !threadId) {
    return { tabs: [], activeId: null, dockWidth: 460, floating: false, floatingBounds: null };
  }
  return loadLayout(principal, threadId);
}



/**
 * §28.8: the dock PDF viewer fetches through the AUTHORIZED client
 * (bearer header) and disposes of the object URL on unmount — the
 * browser cannot attach Authorization headers to <object>/<iframe>
 * subresources, so a plain data URL would 401.
 */
function PdfTabView({ tab }: { tab: DockTab }) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    const controller = new AbortController();
    fetchArtifactBlob(tab.resourceId, tab.filePath || "report.pdf",
                      { signal: controller.signal })
      .then((blob) => {
        if (cancelled || !(blob instanceof Blob)) return;
        url = URL.createObjectURL(blob);
        setObjectUrl(url);
      })
      .catch(() => {
        if (!cancelled) setError("Preview could not be loaded.");
      });
    return () => {
      cancelled = true;
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [tab.resourceId, tab.filePath]);
  if (error) {
    return <div style={{ padding: 16, fontSize: 13 }}>{error}</div>;
  }
  if (!objectUrl) {
    return <div role="status" style={{ padding: 16, fontSize: 13 }}>Loading document…</div>;
  }
  return (
    <object data={objectUrl} type="application/pdf"
            style={{ width: "100%", height: "100%", minHeight: 320 }}
            aria-label={tab.title}>
      <div style={{ padding: 16, fontSize: 13 }}>
        Inline PDF preview is unavailable in this browser — use the
        authenticated download action instead.
      </div>
    </object>
  );
}

import { fetchArtifactBlob } from "@/lib/api";
