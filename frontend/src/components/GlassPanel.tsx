"use client";

/**
 * GlassPanel — dynamic glassmorphic surface with cursor-reactive illumination.
 * A soft radial highlight follows the pointer and refracts through the glass,
 * Apple/Z.ai style. Falls back to static glass on touch devices.
 * Wraps Framer Motion spring presets for entrances and hover compressions.
 */

import { useCallback, useRef, ReactNode, CSSProperties } from "react";
import { motion, useReducedMotion, HTMLMotionProps } from "framer-motion";

export const springSoft = { type: "spring" as const, stiffness: 220, damping: 26, mass: 0.9 };
export const springSnap = { type: "spring" as const, stiffness: 420, damping: 30 };

interface GlassPanelProps extends HTMLMotionProps<"div"> {
  children: ReactNode;
  /** Light intensity of the cursor highlight (0–1). */
  glow?: number;
  /** Render as a clickable/pressable surface with compression feedback. */
  interactive?: boolean;
  className?: string;
  style?: CSSProperties;
}

export default function GlassPanel({
  children, glow = 0.09, interactive = false, className = "", style, ...rest
}: GlassPanelProps) {
  const ref = useRef<HTMLDivElement>(null);
  const reducedMotion = useReducedMotion();

  const onMove = useCallback((e: React.MouseEvent<HTMLDivElement>) => {
    const el = ref.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    el.style.setProperty("--mx", `${e.clientX - rect.left}px`);
    el.style.setProperty("--my", `${e.clientY - rect.top}px`);
  }, []);

  return (
    <motion.div
      ref={ref}
      onMouseMove={onMove}
      initial={reducedMotion ? false : { opacity: 0, y: 14, scale: 0.99 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={springSoft}
      whileHover={interactive && !reducedMotion ? { y: -2 } : undefined}
      whileTap={interactive && !reducedMotion ? { scale: 0.985 } : undefined}
      className={`glass-dynamic ${interactive ? "glass-dynamic-interactive" : ""} ${className}`}
      style={{ ...(style || {}), "--glow": glow } as CSSProperties & Record<string, unknown>}
      {...rest}
    >
      <span className="glass-specular" aria-hidden />
      <span className="glass-illumination" aria-hidden />
      <div className="glass-content">{children}</div>
    </motion.div>
  );
}

/** Ambient light orbs that drift behind glass surfaces. Mount once per page. */
export function AmbientOrbs() {
  return (
    <div className="ambient-orbs" aria-hidden>
      <span className="orb orb-a" />
      <span className="orb orb-b" />
      <span className="orb orb-c" />
    </div>
  );
}
