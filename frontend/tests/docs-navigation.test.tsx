import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import DocsToc from "../src/components/DocsToc";
import { DOC_PAGES } from "../src/content/docs";

vi.mock("next/link", () => ({ default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a> }));
let host: HTMLDivElement;
let root: Root;
beforeEach(async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(<DocsToc currentSlug="user-guide" />));
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

it("filters guides, explains empty results, and restores links when cleared", async () => {
  const field = host.querySelector("input")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, "no-such-guide-xyz");
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
  expect(host.textContent).toContain("No guides found");
  expect(host.querySelectorAll('a[href^="/docs/"]').length).toBe(0);
  await act(async () => (host.querySelector('[aria-label="Clear documentation search"]') as HTMLButtonElement).click());
  expect(host.querySelectorAll('a[href^="/docs/"]').length).toBe(DOC_PAGES.length);
  expect(host.querySelector('[aria-current="page"]')?.getAttribute("href")).toBe("/docs/user-guide");
});

it("closes the mobile menu after navigating to another guide", async () => {
  await act(async () => host.querySelector("button")!.click());
  expect(host.querySelector("button")!.getAttribute("aria-expanded")).toBe("true");
  await act(async () => root.render(<DocsToc currentSlug="api" />));
  expect(host.querySelector("button")!.getAttribute("aria-expanded")).toBe("false");
});
