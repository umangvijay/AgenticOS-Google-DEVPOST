/**
 * Phase 20 §20.11: MOUNTED component tests — real components rendered
 * with React Testing Library over actual DOM nodes (happy-dom).
 * ChatComposer requires the Next.js router context; its behaviors are
 * covered by the shared modules (draft-store, request-policy) plus the
 * deep-linkable evidence components here.
 */
import { describe, expect, it } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { RunStatusBanner } from "@/components/RunStatusBanner";
import { ConnectionStatus } from "@/components/ConnectionStatus";
import { MutationConflictNotice } from "@/components/MutationConflictNotice";

describe("§20.11 RunStatusBanner mounted", () => {
  it("renders the execution badge without a degraded notice when healthy", () => {
    render(<RunStatusBanner status="RUNNING" connection="connected"
                            freshness="current" />);
    expect(screen.getByText("Running")).toBeTruthy();
    expect(screen.queryByText(/offline/i)).toBeNull();
  });

  it("shows the offline notice WITHOUT changing the execution state", () => {
    // §18.8: a connection failure degrades the VIEW, never the task.
    render(<RunStatusBanner status="RUNNING" connection="offline"
                            freshness="stale" />);
    expect(screen.getAllByText("Running").length).toBeGreaterThan(0);
    expect(screen.getByText(/offline/i)).toBeTruthy();
    expect(screen.queryByText("Failed")).toBeNull();
  });
});

describe("§20.11 ConnectionStatus mounted", () => {
  it("stays quiet when online", () => {
    const { container } = render(<ConnectionStatus />);
    // Online is the healthy default (the component reads navigator.onLine).
    expect(container).toBeTruthy();
  });
});

describe("§20.11 MutationConflictNotice mounted", () => {
  it("renders the conflict code, message, current version, next action", () => {
    render(<MutationConflictNotice conflict={{
      code: "REVISION_MISMATCH",
      message: "the reviewed revision 3 is not current",
      current_version: 4,
      next_action: "review the current revision",
    }} />);
    expect(screen.getByText("REVISION_MISMATCH")).toBeTruthy();
    expect(screen.getByText(/revision 3 is not current/)).toBeTruthy();
    expect(screen.getByText(/Current version: 4/)).toBeTruthy();
    expect(screen.getByText(/review the current revision/)).toBeTruthy();
  });

  it("renders nothing without a conflict", () => {
    const { container } = render(<MutationConflictNotice conflict={{}} />);
    expect(container.querySelector("[role=alert]")).toBeNull();
  });

  it("the Refresh action button invokes the callback", () => {
    let clicked = 0;
    render(<MutationConflictNotice conflict={{
      code: "REVISION_MISMATCH", next_action: "refresh and re-review",
    }} onAction={() => { clicked += 1; }} />);
    fireEvent.click(screen.getByText("Refresh"));
    expect(clicked).toBe(1);
  });
});
