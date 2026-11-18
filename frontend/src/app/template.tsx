import type { ReactNode } from "react";
import "./page-transition.css";

/**
 * App Router template: re-mounts on every navigation, giving all pages a
 * consistent fade-up entrance (Apple-style, respects reduced motion).
 */
export default function Template({ children }: { children: ReactNode }) {
  return <div className="route-transition">{children}</div>;
}
