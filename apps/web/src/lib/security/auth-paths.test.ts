import { describe, expect, it } from "vitest";

import { isPublicPath, loginRedirect, safeNext } from "./auth-paths";
import { contentSecurityPolicy, newNonce } from "./csp";

describe("isPublicPath", () => {
  it.each(["/", "/login", "/auth/callback", "/invite/abc", "/s/xyz", "/docs/getting-started"])(
    "%s is public",
    (path) => {
      expect(isPublicPath(path)).toBe(true);
    },
  );

  it.each(["/acme", "/acme/inbox", "/settings/profile", "/onboarding", "/internal", "/logins"])(
    "%s needs a session",
    (path) => {
      expect(isPublicPath(path)).toBe(false);
    },
  );
});

describe("next paths", () => {
  it("keeps the full path and query", () => {
    expect(loginRedirect("/acme/inbox", "?tab=x")).toBe("/login?next=%2Facme%2Finbox%3Ftab%3Dx");
  });

  it.each([
    ["/acme/inbox", "/acme/inbox"],
    ["/acme/langflow?tab=ask#q1", "/acme/langflow?tab=ask#q1"],
    ["/acme/%2F%2Fevil.example", "/acme/%2F%2Fevil.example"],
    ["//evil.example", null],
    ["/\\evil.example", null],
    ["https://evil.example", null],
    ["javascript:alert(1)", null],
    // URL parsing drops tabs and newlines, turning these into "//evil.example".
    ["/\t/evil.example", null],
    ["/\n/evil.example", null],
    ["/\r/evil.example", null],
    ["/ /evil.example", null],
    ["/acme\u0000", null],
    ["/acme/\u00e9", null],
    [`/${"a".repeat(512)}`, null],
    ["relative/path", null],
    ["", null],
    [null, null],
  ])("safeNext(%j) is %j", (value, expected) => {
    expect(safeNext(value)).toBe(expected);
  });
});

describe("content security policy", () => {
  it("allows only nonce'd scripts in production", () => {
    const policy = contentSecurityPolicy("abc123", { dev: false });
    expect(policy).toContain("script-src 'self' 'nonce-abc123' 'strict-dynamic';");
    expect(policy).not.toContain("unsafe-eval");
    expect(policy).toContain("frame-ancestors 'none'");
    expect(policy).toContain("object-src 'none'");
    expect(policy).toContain("upgrade-insecure-requests");
  });

  it("adds what the dev server needs only in development", () => {
    const policy = contentSecurityPolicy("abc", { dev: true });
    expect(policy).toContain("'unsafe-eval'");
    expect(policy).toContain("connect-src 'self' ws:");
  });

  it("makes a fresh, unguessable nonce each time", () => {
    const nonces = new Set(Array.from({ length: 50 }, newNonce));
    expect(nonces.size).toBe(50);
    expect([...nonces][0]).toMatch(/^[A-Za-z0-9+/]{22}==$/);
  });
});
