"use client";
import { useState, useEffect } from "react";
import { throttle } from "radash";

/**
 * Connectivity vs capability (Phase 18 §18.17, finding F7) — SEPARATE
 * dimensions tracked independently:
 * - online/offline hint (navigator.onLine + online/offline events,
 *   which fire on ALL browsers — the Network Information API is NOT
 *   required);
 * - data-saving preference / connection quality (capability);
 * - API reachability is a SEPARATE concern owned by the request layer
 *   (browser online status alone does not prove the API is reachable).
 */

interface NetworkInformation extends EventTarget {
  effectiveType?: string;
  saveData?: boolean;
}

type NavigatorWithNetwork = Navigator & {
  connection?: NetworkInformation;
  mozConnection?: NetworkInformation;
  webkitConnection?: NetworkInformation;
};

export function useNetworkStatus() {
  // §18.17: the ONLINE/OFFLINE hint — its own state, listening to the
  // universal online/offline events.
  const [online, setOnline] = useState(
    typeof navigator === "undefined" ? true : navigator.onLine);
  const [isLowEnd, setIsLowEnd] = useState(false);

  useEffect(() => {
    const goOnline = () => setOnline(true);
    const goOffline = () => setOnline(false);
    window.addEventListener("online", goOnline);
    window.addEventListener("offline", goOffline);
    // (set-state-in-effect) no synchronous set here: the initializer
    // already read navigator.onLine, and the online/offline EVENTS cover
    // every later transition — including a suspended tab's return, which
    // re-fires them. Re-reading the same value here would be a no-op
    // setState inside the effect.

    // Capability tracking (unchanged contract).
    const checkNetwork = throttle({ interval: 2000 }, () => {
      const networkNavigator = navigator as NavigatorWithNetwork;
      const connection = networkNavigator.connection
        || networkNavigator.mozConnection
        || networkNavigator.webkitConnection;
      if (connection) {
        const isSaveData = connection.saveData === true;
        const isSlow = ["slow-2g", "2g", "3g"].includes(connection.effectiveType);
        if (isSaveData || isSlow) {
          setIsLowEnd(true);
          document.documentElement.classList.add("reduce-data");
        } else {
          setIsLowEnd(false);
          document.documentElement.classList.remove("reduce-data");
        }
      }
    });
    checkNetwork();
    const networkNavigator = navigator as NavigatorWithNetwork;
    const connection = networkNavigator.connection
      || networkNavigator.mozConnection
      || networkNavigator.webkitConnection;
    if (connection) {
      connection.addEventListener("change", checkNetwork);
    }
    return () => {
      window.removeEventListener("online", goOnline);
      window.removeEventListener("offline", goOffline);
      if (connection) {
        connection.removeEventListener("change", checkNetwork);
      }
    };
  }, []);

  return {
    isLowEnd,
    /** §18.17: the browser online/offline hint — consumed by
      useRunEvents so offline suspends reconnect activity and preserves
      work (the old contract measured only quality). */
    online,
    isOffline: !online,
  };
}
