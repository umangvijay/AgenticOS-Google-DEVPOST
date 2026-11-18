/**
 * Phase 19 §19.10–§19.15: the design-system and interaction contracts —
 * scene interaction, graph accessibility/state, and the shared
 * components.
 */
import { describe, expect, it } from "vitest";
import { clampZoom, SCENE_ZOOM_MIN, SCENE_ZOOM_MAX } from "@/lib/scene";
import { readFileSync } from "node:fs";

describe("§19.15 scene interaction policy", () => {
  const sceneSrc = readFileSync("src/components/CyanotypeScene.tsx", "utf8");

  it("no touch listeners intercept page gestures", () => {
    // The pinch-zoom interception is REMOVED — touch devices keep
    // native page scrolling and browser zoom.
    expect(sceneSrc.includes("onTouchMove")).toBe(false);
    expect(sceneSrc.includes("addEventListener(\"touchstart\"")).toBe(false);
    // Desktop wheel zoom remains explicit.
    expect(sceneSrc.includes("addEventListener(\"wheel\"")).toBe(true);
  });

  it("wheel zoom stays bounded by the shared clamp", () => {
    expect(SCENE_ZOOM_MIN).toBe(0.4);
    expect(SCENE_ZOOM_MAX).toBe(2.8);
    expect(clampZoom(1, -10)).toBe(SCENE_ZOOM_MAX);
  });

  it("the scene is labeled decorative, not telemetry", () => {
    expect(sceneSrc).toContain("decorative only, not live execution status");
  });
});

describe("§19.18 graph accessibility and state", () => {
  it("CoordinationGraph list items expose BUTTON semantics", () => {
    const src = readFileSync("src/components/CoordinationGraph.tsx", "utf8");
    expect(src).toContain('type="button"');
    expect(src).toContain("aria-pressed");
    // The bare clickable <li onClick> is gone.
    expect(src.includes("<li key={d.contract_id} onClick=")).toBe(false);
  });

  it("WorkflowGraph stores selected task IDENTITY, not a snapshot", () => {
    const src = readFileSync("src/components/WorkflowGraph.tsx", "utf8");
    expect(src).toContain("selectedTaskId");
    expect(src.includes("useState<Task | null>(null)")).toBe(false);
  });
});

describe("§18.8/§19.7 run events are scoped", () => {
  it("substring completion matching is gone from useRunEvents", () => {
    const src = readFileSync("src/hooks/useRunEvents.ts", "utf8");
    expect(src.includes('event.type?.includes("COMPLETED")')).toBe(false);
    expect(src).toContain("WORKFLOW_COMPLETED");
  });
});
