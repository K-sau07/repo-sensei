/**
 * Which pages need a session. The proxy only checks that a session cookie exists (fast, no
 * network); the API decides whether it is valid and the client handles an expired one.
 */

// Must match the API's cookie names (secure deployments use the __Host- prefix).
export const SESSION_COOKIES = ["__Host-session", "session"] as const;

// Pages anyone can open. Everything else (org workspaces, personal settings, onboarding, the CLI
// device flow, internal tools) needs a session.
const PUBLIC_PREFIXES = [
  "/login",
  "/auth/callback",
  "/invite",
  "/explore",
  "/product",
  "/benchmarks",
  "/pricing",
  "/changelog",
  "/blog",
  "/docs",
  "/security",
  "/privacy",
  "/terms",
  "/s",
  "/maintenance",
];

export function isPublicPath(pathname: string): boolean {
  if (pathname === "/") return true;
  return PUBLIC_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

/** Where a signed-out visitor goes instead, remembering what they asked for. */
export function loginRedirect(pathname: string, search: string): string {
  return `/login?next=${encodeURIComponent(`${pathname}${search}`)}`;
}

/** Only same-site paths are followed after sign-in (no open redirects). */
export function safeNext(value: string | null | undefined): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return null;
  }
  return value;
}
