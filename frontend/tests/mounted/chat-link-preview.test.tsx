/**
 * Phase 28 §28.7 — the chat link-card interaction, mounted.
 *
 * Both renderers (sent-message linkify + sanitized assistant Markdown)
 * share ChatLinkPreviewAnchor: hover intent, keyboard focus, Escape with
 * focus return, stale-response cancellation, and normal opening behavior
 * preserved.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

vi.mock("@/lib/api", () => ({
  getLinkPreview: vi.fn(),
}));

import { getLinkPreview, LinkPreview } from "@/lib/api";
import {
  ChatLinkPreviewAnchor,
  linkifyText,
  isEligibleHttpUrl,
} from "@/components/ChatLinkPreview";

const mockPreview = getLinkPreview as unknown as ReturnType<typeof vi.fn>;

function card(overrides: Partial<LinkPreview> = {}): LinkPreview {
  return {
    requested_url: "https://example.com/invoices",
    display_url: "example.com/invoices",
    final_host: "example.com",
    state: "ready",
    title: "Acme Invoices — November 2026",
    description: "Overdue ledger",
    favicon_url: null,
    thumbnail_url: null,
    ...overrides,
  };
}

beforeEach(() => {
  mockPreview.mockReset();
});
afterEach(cleanup);

describe("§28.7 ChatLinkPreviewAnchor", () => {
  it("shows a loading card on hover intent and renders metadata", async () => {
    mockPreview.mockImplementation(
      () => new Promise((resolve) => setTimeout(() => resolve(card()), 20)));
    render(<ChatLinkPreviewAnchor href="https://example.com/invoices">example.com/invoices</ChatLinkPreviewAnchor>);
    const anchor = screen.getByRole("link");
    fireEvent.mouseEnter(anchor);
    // Hover intent: the card appears with a loading state first.
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
    // A fast response may already have replaced the loading state — the
    // CONTRACT is: card visible → metadata (or explicit fallback).
    await waitFor(() => expect(screen.getByText("Acme Invoices — November 2026")).toBeTruthy());
    // An explicit Open affordance exists and points at the destination.
    const open = screen.getByText("Open").closest("a") as HTMLAnchorElement;
    expect(open.getAttribute("href")).toBe("https://example.com/invoices");
    expect(open.getAttribute("target")).toBe("_blank");
  });

  it("aborts the stale response after focus moves to another link", async () => {
    let resolveA: ((v: LinkPreview) => void) | null = null;
    mockPreview.mockImplementationOnce(
      () => new Promise<LinkPreview>((resolve) => { resolveA = resolve; }));
    // Every later link (B) resolves normally.
    mockPreview.mockResolvedValue(card({ title: "B card" }));
    render(
      <div>
        <ChatLinkPreviewAnchor href="https://a.example/one">A</ChatLinkPreviewAnchor>
        <ChatLinkPreviewAnchor href="https://b.example/two">B</ChatLinkPreviewAnchor>
      </div>);
    const a = screen.getAllByRole("link")[0];
    const b = screen.getAllByRole("link")[1];
    fireEvent.focus(a);
    await waitFor(() => expect(mockPreview).toHaveBeenCalledTimes(1));
    // Focus moves to B: the real browser fires blur on A first — blur
    // dismisses A's card and ABORTS A's in-flight metadata request.
    fireEvent.blur(a);
    fireEvent.focus(b);
    await waitFor(() => expect(mockPreview).toHaveBeenCalledTimes(2));
    // A's late response must never render anywhere.
    resolveA!(card({ title: "STALE A", requested_url: "https://a.example/one" }));
    await new Promise((r) => setTimeout(r, 10));
    expect(screen.queryByText("STALE A")).toBeNull();
  });

  it("hover race: B resolves while A is pending; B stays visible and A never replaces it", async () => {
    // Mirrors the e2e A3/A4 fixture-backed race, but without the browser:
    // hover A, move to B before A resolves, B resolves fast, A resolves
    // last. The card must show B throughout — and NOT close.
    let resolveA: ((v: LinkPreview) => void) | null = null;
    mockPreview.mockImplementationOnce(
      () => new Promise<LinkPreview>((resolve) => { resolveA = resolve; }));
    mockPreview.mockResolvedValue(card({ title: "FAST B TITLE" }));
    render(
      <div>
        <ChatLinkPreviewAnchor href="https://slow-a.example/page">A</ChatLinkPreviewAnchor>
        <ChatLinkPreviewAnchor href="https://fast-b.example/page">B</ChatLinkPreviewAnchor>
      </div>);
    const a = screen.getAllByRole("link")[0];
    const b = screen.getAllByRole("link")[1];
    // Hover A: intent (160 ms) fires A's fetch — pending.
    fireEvent.mouseEnter(a);
    await waitFor(() => expect(mockPreview).toHaveBeenCalledTimes(1));
    // Pointer travels to B: leave A (schedules the dismiss check), enter
    // B (cancels it, aborts A's fetch, starts B's intent).
    fireEvent.mouseLeave(a);
    fireEvent.mouseEnter(b);
    // B's intent fetch fires after HOVER_INTENT_MS.
    await waitFor(() => expect(mockPreview).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
    await waitFor(() => expect(screen.getByText("FAST B TITLE")).toBeTruthy());
    // A's late response arrives LAST — it must not replace B and must
    // not dismiss the open card.
    resolveA!(card({ title: "SLOW A TITLE", requested_url: "https://slow-a.example/page" }));
    await new Promise((r) => setTimeout(r, 10));
    expect(screen.getByText("FAST B TITLE")).toBeTruthy();
    expect(screen.queryByText("SLOW A TITLE")).toBeNull();
    expect(screen.queryByRole("dialog")).not.toBeNull();
  });

  it("Escape dismisses the card and returns focus to the anchor", async () => {
    mockPreview.mockResolvedValue(card());
    render(<ChatLinkPreviewAnchor href="https://example.com/x">x</ChatLinkPreviewAnchor>);
    const anchor = screen.getByRole("link");
    fireEvent.focus(anchor);
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
    fireEvent.keyDown(anchor, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(anchor);
  });

  it("clicking the anchor keeps normal opening behavior (no preventDefault)", () => {
    render(<ChatLinkPreviewAnchor href="https://example.com/x">x</ChatLinkPreviewAnchor>);
    const anchor = screen.getByRole("link") as HTMLAnchorElement;
    const click = new MouseEvent("click", { bubbles: true, cancelable: true });
    const defaultPrevented = !anchor.dispatchEvent(click);
    expect(defaultPrevented).toBe(false);
  });

  it("renders a loading card on keyboard focus without hover", async () => {
    mockPreview.mockResolvedValue(card());
    render(<ChatLinkPreviewAnchor href="https://example.com/focus">focus</ChatLinkPreviewAnchor>);
    fireEvent.focus(screen.getByRole("link"));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
  });
});

describe("§28.7 linkifyText", () => {
  function collect(text: string) {
    const parts = linkifyText(
      text,
      (href, content, key) => <a key={key} href={href}>{content}</a>,
    );
    const anchors = parts.filter((p) => (p as { type?: string }).type === "a") as {
      props: { href: string; children: React.ReactNode };
    }[];
    const textContent = parts
      .map((p) => {
        const anyP = p as { props?: { children?: unknown } };
        return typeof anyP.props?.children === "string" ? anyP.props.children : "";
      })
      .join("");
    return { anchors, textContent };
  }

  it("linkifies eligible HTTP(S) text and preserves punctuation and parens", () => {
    const { anchors, textContent } = collect(
      "See https://example.com/ledger(a), note the comma.",
    );
    expect(anchors).toHaveLength(1);
    expect(anchors[0].props.href).toBe("https://example.com/ledger(a)");
    expect(textContent).toContain(",");
  });

  it("never linkifies inside code spans", () => {
    const { anchors } = collect("run `curl https://secret.example/x` now");
    expect(anchors).toHaveLength(0);
  });

  it("preserves line breaks as plain text runs", () => {
    const { anchors, textContent } = collect("first line\nhttps://example.com/a\nlast");
    expect(anchors).toHaveLength(1);
    expect(textContent).toContain("first line");
  });
});

describe("§28.7 eligibility", () => {
  it("admits only http(s) destinations", () => {
    expect(isEligibleHttpUrl("https://example.com/x")).toBe(true);
    expect(isEligibleHttpUrl("http://example.com/x")).toBe(true);
    expect(isEligibleHttpUrl("javascript:alert(1)")).toBe(false);
    expect(isEligibleHttpUrl("data:text/html,x")).toBe(false);
    expect(isEligibleHttpUrl("mailto:a@b.c")).toBe(false);
  });
});
