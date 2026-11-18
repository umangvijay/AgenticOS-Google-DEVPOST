import { act, useEffect, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LandingPage from "../src/app/page";
import type { SceneStatus } from "../src/components/CyanotypeScene";

type SceneProps = { onStatus: (status: SceneStatus) => void };
const scene = vi.hoisted(() => ({ props: null as SceneProps | null, mounts: 0 }));
vi.mock("next/dynamic", () => ({ default: () => function TestScene(props: SceneProps) {
  scene.props = props;
  const { onStatus } = props;
  useEffect(() => { scene.mounts += 1; onStatus("ready"); }, [onStatus]);
  return null;
} }));
vi.mock("@/lib/api", () => ({ API_BASE: "http://localhost/api/v1" }));
vi.mock("@/components/PublicNavbar", () => ({ default: () => null }));
vi.mock("next/link", () => ({ default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a> }));

let root: Root;
let host: HTMLDivElement;
const button = (label: string) => Array.from(host.querySelectorAll("button")).find(el => el.getAttribute("aria-label") === label || el.textContent?.trim() === label)!;
async function click(label: string) { await act(async () => button(label).click()); }

beforeEach(async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
  scene.mounts = 0;
  window.localStorage.clear();
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(<LandingPage />));
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  vi.unstubAllGlobals();
});

describe("landing controls", () => {
  it("resumes the selected speed after pause", async () => {
    await click("0.5×");
    await click("Pause scene");
    expect(button("Pause scene")).toBeUndefined();
    expect(button("Play scene").getAttribute("aria-pressed")).toBe("true");
    await click("Play scene");
    expect(button("0.5×").getAttribute("aria-pressed")).toBe("true");
  });

  it("recovers from a lost scene without hiding the hero", async () => {
    await act(async () => scene.props!.onStatus("fallback"));
    expect(host.textContent).toContain("Showing the still illustration");
    expect(host.querySelector(".scene-notice")).not.toBeNull();
    await click("Retry 3D scene");
    expect(scene.mounts).toBe(2);
    expect(button("Pause scene").disabled).toBe(false);
  });

  it("keeps the HUD panel off the page", () => {
    expect(host.querySelector(".loom-hud")).toBeNull();
    expect(host.querySelector(".draft-footer")).not.toBeNull();
    expect(host.querySelector(".scene-controls")).not.toBeNull();
  });
});
