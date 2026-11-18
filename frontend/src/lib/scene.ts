"use client";

/**
 * Scene interaction contract (Phase 14 §14.36) — ONE clamp for the 3D
 * scene's zoom, shared by the wheel and pinch handlers so every input
 * path respects the same bounds. Extracted from the component so the
 * bounds are TESTED, not just inline magic numbers.
 */

export const SCENE_ZOOM_MIN = 0.4;
export const SCENE_ZOOM_MAX = 2.8;

/** Clamp a wheel/pinch zoom delta into the scene's bounded range,
  quantized to 2 decimals so the state stays stable across inputs. */
export function clampZoom(current: number, delta: number): number {
  const next = Math.max(SCENE_ZOOM_MIN,
    Math.min(SCENE_ZOOM_MAX, current - delta));
  return Number(next.toFixed(2));
}
