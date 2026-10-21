/**
 * Which pages need a session. The proxy only checks that a session cookie exists (fast, no
 * network); the API decides whether it is valid and the client handles an expired one.
 */

// Must match the API's cookie names (secure deployments use the __Host- prefix).
export const SESSION_COOKIES = ["__Host-session", "session"] as const;

// Pages anyone can open. Everything else (org workspaces, personal settings, onboarding, the CLI
// device flow, internal tools) needs a session.
export const PUBLIC_PREFIXES = [
  "/login",
  "/auth/callback",
  "/logout",
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

// Same rule as the API: a path of printable ASCII, at most 512 characters. Whitespace and control
// characters are refused because URL parsing drops tabs and newlines ("/\t/evil.example" becomes
// "//evil.example"), and backslashes because browsers read them as slashes.
const SAFE_NEXT = /^\/(?![/\\])[\x21-\x7e]{0,511}$/;
const PROBE_ORIGIN = "https://sensei.invalid";

/** Only same-site paths are followed after sign-in (no open redirects). */
export function safeNext(value: string | null | undefined): string | null {
  if (!value || !SAFE_NEXT.test(value) || value.includes("\\")) return null;
  // Belt and braces: whatever the browser makes of it must stay on this origin.
  const parsed = new URL(value, PROBE_ORIGIN);
  if (parsed.origin !== PROBE_ORIGIN) return null;
  return `${parsed.pathname}${parsed.search}${parsed.hash}`;
}
