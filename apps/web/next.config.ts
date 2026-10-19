import type { NextConfig } from "next";

const isDev = process.env.NODE_ENV === "development";

// The browser always calls the API on its own origin. Locally Next forwards /api/* to FastAPI;
// in deployed environments the load balancer routes /api/* and this stays unset.
const apiProxyOrigin =
  process.env.SENSEI_API_PROXY_ORIGIN ?? (isDev ? "http://localhost:8000" : "");

// Baseline policy. A nonce-based script policy replaces 'unsafe-inline' once the proxy sets nonces.
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https://avatars.githubusercontent.com",
  "font-src 'self'",
  `connect-src 'self'${isDev ? " ws:" : ""}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
  ...(isDev ? [] : ["upgrade-insecure-requests"]),
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
  },
  ...(isDev
    ? []
    : [
        { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains; preload" },
      ]),
];

const nextConfig: NextConfig = {
  // Always defined, so Next inlines it and dead-code elimination drops mock mode from real builds.
  env: { NEXT_PUBLIC_MOCKS: process.env.NEXT_PUBLIC_MOCKS === "1" ? "1" : "0" },
  poweredByHeader: false,
  // Keep the dev badge clear of the sidebar's account menu (bottom-left).
  devIndicators: { position: "bottom-right" },
  agentRules: false,
  reactStrictMode: true,
  reactCompiler: true,
  cacheComponents: true,
  partialPrefetching: true,
  typedRoutes: true,
  turbopack: {
    resolveAlias: {
      // Mock mode (NEXT_PUBLIC_MOCKS=1) imports msw/browser in a client component; the server
      // render of that component gets a stub because MSW has no server build of that entry.
      "msw/browser": { browser: "msw/browser", default: "./src/mocks/worker-ssr-stub.ts" },
    },
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  headers: () => Promise.resolve([{ source: "/:path*", headers: securityHeaders }]),
  rewrites: () =>
    Promise.resolve(
      apiProxyOrigin
        ? [{ source: "/api/:path*", destination: `${apiProxyOrigin}/api/:path*` }]
        : [],
    ),
};

export default nextConfig;
