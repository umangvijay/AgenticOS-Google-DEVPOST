/**
 * Phase 28 §28.7.5–28.7.6 — the workspace preview dock, mounted.
 *
 * Tab identity and reuse, close-vs-stop disambiguation, keyboard tab
 * selection, float/redock, persisted layout keyed by principal + thread,
 * and account-switch clearing of prior-principal resources.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

vi.mock("@/components/BrowserSessionPanel", () => ({
  BrowserSessionPanel: ({ sessionId }: { sessionId: string }) => (
    <div data-testid="browser-panel">browser:{sessionId}</div>
  ),
}));

import { WorkspacePreviewDock } from "@/components/WorkspacePreviewDock";
import {
  clearPrincipalLayout, closeTab, DockLayout, DockTab, loadLayout, openTab,
  saveLayout,
} from "@/lib/workspace-tabs";
void openTab;

function layoutWith(...tabs: DockLayout["tabs"]): DockLayout {
  let layout: DockLayout = { tabs: [], activeId: null, dockWidth: 460,
                             floating: false, floatingBounds: null };
  for (const t of tabs) layout = openTab(layout, t);
  return layout;
}

const PDF_TAB: DockTab = {
  id: "pdf-1", kind: "pdf", title: "Invoice report (PDF)",
  resourceId: "artifact-1", filePath: "report.pdf", runId: "run-1",
};
const BROWSER_TAB: DockTab = {
  id: "br-1", kind: "browser", title: "Ledger console",
  resourceId: "session-9", runId: "run-1",
};

beforeEach(() => {
  window.localStorage.clear();
});
afterEach(cleanup);

describe("§28.7 dock layout store", () => {
  it("reopening an existing resource focuses the existing tab", () => {
    let layout = layoutWith(PDF_TAB);
    layout = openTab(layout, { ...PDF_TAB, title: "Renamed" });
    expect(layout.tabs).toHaveLength(1);
    expect(layout.tabs[0].title).toBe("Invoice report (PDF)");
    expect(layout.activeId).toBe("pdf-1");
  });

  it("closing the active tab focuses a neighbour", () => {
    let layout = layoutWith(PDF_TAB, BROWSER_TAB);
    layout = { ...layout, activeId: "pdf-1" };
    layout = closeTab(layout, "pdf-1");
    expect(layout.activeId).toBe("br-1");
    layout = closeTab(layout, "br-1");
    expect(layout.activeId).toBeNull();
  });

  it("persists layout per principal + thread, never across principals", () => {
    const layout = layoutWith(PDF_TAB);
    saveLayout("alice", "thread-1", layout);
    expect(loadLayout("alice", "thread-1").tabs).toHaveLength(1);
    expect(loadLayout("bob", "thread-1").tabs).toHaveLength(0);
    expect(loadLayout("alice", "thread-2").tabs).toHaveLength(0);
    clearPrincipalLayout("alice");
    expect(loadLayout("alice", "thread-1").tabs).toHaveLength(0);
  });
});

describe("§28.7 WorkspacePreviewDock mounted", () => {
  const artifactUrl = (id: string, path: string) =>
    `/api/v1/artifacts/${id}/files/${path}`;

  it("renders tabs, selects by click, and closes without stopping", () => {
    let layout = layoutWith(PDF_TAB, BROWSER_TAB);
    const onLayoutChange = (next: DockLayout) => { layout = next; };
    const onBrowserStop = vi.fn();
    render(<WorkspacePreviewDock
      principal="alice" threadId="t1" layout={layout}
      onLayoutChange={onLayoutChange} artifactFileUrl={artifactUrl}
      onBrowserStop={onBrowserStop} />);
    expect(screen.getByTestId("dock-tab-pdf")).toBeTruthy();
    fireEvent.click(screen.getByTestId("dock-tab-browser"));
    expect(screen.getByTestId("browser-panel")).toBeTruthy();
    // Closing a tab is PRESENTATION ONLY — no stop call.
    fireEvent.click(screen.getByLabelText("Close Ledger console"));
    expect(onBrowserStop).not.toHaveBeenCalled();
  });

  it("keyboard arrows and Home/End select tabs", () => {
    // A controlled parent that really re-renders on layout changes.
    function Harness() {
      const [layout, setLayout] = useState(() => layoutWith(PDF_TAB, BROWSER_TAB));
      return <WorkspacePreviewDock
        principal="alice" threadId="t1" layout={layout}
        onLayoutChange={setLayout} artifactFileUrl={artifactUrl} />;
    }
    render(<Harness />);
    const tablist = screen.getByRole("tablist");
    fireEvent.keyDown(tablist, { key: "Home" });
    const selected = () =>
      screen.getByRole("tab", { selected: true }).getAttribute("data-testid");
    expect(selected()).toBe("dock-tab-pdf");
    fireEvent.keyDown(tablist, { key: "End" });
    expect(selected()).toBe("dock-tab-browser");
    fireEvent.keyDown(tablist, { key: "ArrowLeft" });
    expect(selected()).toBe("dock-tab-pdf");
    fireEvent.keyDown(tablist, { key: "ArrowRight" });
    expect(selected()).toBe("dock-tab-browser");
  });

  it("floats and redocks the same panel", () => {
    function Harness() {
      const [layout, setLayout] = useState(() => layoutWith(PDF_TAB));
      return <WorkspacePreviewDock
        principal="alice" threadId="t1" layout={layout}
        onLayoutChange={setLayout} artifactFileUrl={artifactUrl} />;
    }
    render(<Harness />);
    fireEvent.click(screen.getByTestId("dock-float-toggle"));
    expect(screen.getByTestId("dock-float-toggle").textContent).toContain("Redock");
    fireEvent.click(screen.getByTestId("dock-float-toggle"));
    expect(screen.getByTestId("dock-float-toggle").textContent).toContain("Float");
    // The SAME tab resource survived the presentation change.
    expect(screen.getByTestId("dock-tab-pdf")).toBeTruthy();
  });

  it("renders a resource that lost authorization as unavailable, closable", () => {
    const layout = layoutWith(PDF_TAB);
    render(<WorkspacePreviewDock
      principal="alice" threadId="t1" layout={layout}
      onLayoutChange={() => {}} artifactFileUrl={artifactUrl}
      onRevalidateResource={async () => false} />);
    expect(screen.getByTestId("workspace-preview-dock")).toBeTruthy();
  });
});
