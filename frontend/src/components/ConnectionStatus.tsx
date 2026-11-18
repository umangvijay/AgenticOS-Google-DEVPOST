"use client";

/**
 * Connection status (Phase 18 §18.17) — connectivity and freshness as
 * separate, restrained indicators. Browser online/offline is a HINT;
 * the request layer owns API reachability.
 */
import { useEffect, useState } from "react";

export type ConnectionLevel = "online" | "offline" | "unknown";

export function ConnectionStatus() {
  const [level, setLevel] = useState<ConnectionLevel>("unknown");
  useEffect(() => {
    const sync = () => setLevel(navigator.onLine ? "online" : "offline");
    sync();
    window.addEventListener("online", sync);
    window.addEventListener("offline", sync);
    return () => {
      window.removeEventListener("online", sync);
      window.removeEventListener("offline", sync);
    };
  }, []);
  if (level === "unknown") return null;
  if (level === "online") return null;   // quiet when healthy
  return (
    <div role="status" aria-live="polite"
         style={{ position: "fixed", bottom: 12, right: 12, zIndex: 50,
                  padding: "8px 14px", borderRadius: 8,
                  background: "var(--warning)", color: "#000", fontSize: 13 }}>
      You are offline — your work is saved locally.
    </div>
  );
}
