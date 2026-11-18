"use client";

/**
 * Motion policy (Phase 19 §19.6/§19.8): ONE source of truth for whether
 * motion is allowed, applied consistently across canvas, tilt, hover,
 * and page transitions — and a clear illustration-vs-telemetry rule.
 */

export type MotionPreference = "full" | "reduced" | "static";

export function motionPreference(): MotionPreference {
  if (typeof window === "undefined" || !window.matchMedia) return "full";
  // User-level preference wins (§19.6).
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return "static";
  }
  // Data-saver / low-end devices get the reduced tier (no decorative
  // animation), matching the global .reduce-data class.
  if (document.documentElement.classList.contains("reduce-data")) {
    return "reduced";
  }
  return "full";
}

/** Should DECORATIVE animation run (scene, tilt, floating effects)? */
export function decorativeMotionAllowed(): boolean {
  return motionPreference() === "full";
}

/** Should ESSENTIAL state transitions run even in reduced mode? Opacity
  fades that communicate a state CHANGE (not decoration) stay on in the
  `reduced` tier and off only in `static`. */
export function essentialTransitionsAllowed(): boolean {
  return motionPreference() !== "static";
}
