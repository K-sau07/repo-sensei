/**
 * Organization URL names, checked in the browser before asking the API. The rules and the reserved
 * list mirror packages/core/src/sensei_core/orgs/service.py (a test keeps them identical); the API
 * stays the authority.
 */

export const SLUG_RE = /^[a-z0-9][a-z0-9-]{1,37}[a-z0-9]$/;

export const RESERVED_SLUGS = new Set([
  "admin",
  "api",
  "app",
  "auth",
  "benchmarks",
  "blog",
  "changelog",
  "dashboard",
  "device",
  "docs",
  "explore",
  "help",
  "internal",
  "invite",
  "login",
  "logout",
  "maintenance",
  "new",
  "oauth",
  "onboarding",
  "pricing",
  "privacy",
  "product",
  "s",
  "security",
  "settings",
  "signup",
  "static",
  "status",
  "support",
  "terms",
  "www",
]);

export type SlugProblem = "invalid_slug" | "slug_reserved";

export function slugProblem(slug: string): SlugProblem | null {
  if (!SLUG_RE.test(slug) || slug.includes("--")) return "invalid_slug";
  if (RESERVED_SLUGS.has(slug)) return "slug_reserved";
  return null;
}

/** "Acme Inc." → "acme-inc": lowercase, hyphens between words, nothing else. */
export function slugify(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 39)
    .replace(/-+$/g, "");
}

export const SLUG_MESSAGES: Record<string, string> = {
  invalid_slug:
    "Use 3–39 lowercase letters, digits or single hyphens, starting and ending with a letter or digit.",
  slug_reserved: "That name is reserved.",
  slug_taken: "That name is already taken.",
  check_failed: "Couldn't check that name. Try again in a moment.",
};
