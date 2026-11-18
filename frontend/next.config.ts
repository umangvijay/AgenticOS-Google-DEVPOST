import type { NextConfig } from "next";
import fs from "fs";
import path from "path";
import withBundleAnalyzer from "@next/bundle-analyzer";

function loadRootEnv() {
  const envPath = path.join(__dirname, "..", ".env");
  if (!fs.existsSync(envPath)) return;
  for (const line of fs.readFileSync(envPath, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 1) continue;
    const key = trimmed.slice(0, eq).trim();
    let val = trimmed.slice(eq + 1).trim();
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    if (process.env[key] === undefined) process.env[key] = val;
  }
}
loadRootEnv();

// The CSP must follow the API URL, not a second hardcoded copy of it —
// a mismatched port here makes the browser block every API fetch with an
// opaque "Failed to fetch" while the server logs nothing.
function apiOriginsForCsp(): string[] {
  const apiBase =
    process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
  try {
    const u = new URL(apiBase);
    const origins = [u.origin];
    // localhost and 127.0.0.1 are distinct origins; allow both spellings so
    // the page works no matter which host the dev server was opened through.
    if (u.hostname === "localhost") {
      origins.push(`http://127.0.0.1${u.port ? ":" + u.port : ""}`);
    } else if (u.hostname === "127.0.0.1") {
      origins.push(`http://localhost${u.port ? ":" + u.port : ""}`);
    }
    return origins;
  } catch {
    return ["http://localhost:8000", "http://127.0.0.1:8000"];
  }
}
const API_ORIGINS = apiOriginsForCsp();

const nextConfig: NextConfig = {
  output: "standalone",
  compress: true,
  devIndicators: false,
  // Don't advertise the framework version (audit: x-powered-by leak)
  poweredByHeader: false,
  experimental: {
    optimizePackageImports: ["lucide-react", "radash", "@xyflow/react"],
    optimizeCss: true,
  },
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
          { key: "X-DNS-Prefetch-Control", value: "off" },
          // Match the API's header stack (audit P1.8: frontend lacked HSTS/CSP).
          // script-src keeps 'unsafe-inline': this app's marketing pages are
          // statically prerendered, so nonce-based CSP (middleware) cannot
          // stamp its scripts — tried in Round 8 and reverted for exactly
          // that reason. Hardening requires giving up static prerendering.
          {
            key: "Strict-Transport-Security",
            value: "max-age=63072000; includeSubDomains",
          },
          {
            key: "Cross-Origin-Opener-Policy",
            value: "same-origin-allow-popups",
          },
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              // 'unsafe-eval' is dev-only: react-refresh evaluates strings.
              // (This gap is what broke the workspace e2e spec since Round 3.)
              `script-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/client${process.env.NODE_ENV !== "production" ? " 'unsafe-eval'" : ""}`,
              "frame-src 'self' https://accounts.google.com/gsi/",
              "style-src 'self' 'unsafe-inline'",
              "img-src 'self' data: blob:",
              "font-src 'self' data:",
              // API origins come from NEXT_PUBLIC_API_URL so the CSP can never
              // drift from the URL the client actually calls; *.run.app and
              // accounts.google.com cover the Cloud Run deploy and Google OAuth.
              `connect-src 'self' ${API_ORIGINS.join(" ")} https://*.run.app https://accounts.google.com`,
              "frame-ancestors 'none'",
              "base-uri 'self'",
              "form-action 'self'",
            ].join("; "),
          },
        ],
      },
      {
        source: "/images/(.*)",
        headers: [
          {
            key: "Cache-Control",
            value: "public, max-age=31536000, immutable",
          },
        ],
      },
    ];
  },
  webpack: (config, { dev, isServer }) => {
    // Forcefully disable core-js polyfills that Next.js aggressively injects
    if (!dev && !isServer) {
      config.resolve.alias = {
        ...config.resolve.alias,
        "core-js": false,
        "@swc/helpers": false,
        "next/dist/client/polyfills": false,
        "next/dist/build/polyfills/polyfill-nomodule": false,
        "next/dist/build/polyfills/polyfills": false,
      };
    }
    return config;
  },
};

const analyzer = withBundleAnalyzer({
  enabled: process.env.ANALYZE === "true",
});

export default analyzer(nextConfig);
