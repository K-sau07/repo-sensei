import { describe, expect, it } from "vitest";

import { describeDevice } from "@/components/settings/sessions-view";
import type { Me } from "@/lib/api/types";

import { loginUrl } from "./login-panel";
import { destinationAfterSignIn } from "./sign-in-callback";

const me = (overrides: Partial<Me> = {}): Me => ({
  id: "u1",
  login: "octo",
  name: "Octo",
  email: null,
  avatar_url: null,
  is_staff: false,
  last_org_slug: "beta",
  orgs: [
    { slug: "acme", name: "Acme", role: "owner" },
    { slug: "beta", name: "Beta", role: "member" },
  ],
  ...overrides,
});

describe("destinationAfterSignIn", () => {
  it("prefers where you were going", () => {
    expect(destinationAfterSignIn(me(), "/acme/inbox")).toBe("/acme/inbox");
  });
  it("ignores unsafe or empty next", () => {
    expect(destinationAfterSignIn(me(), "//evil.example")).toBe("/beta");
    expect(destinationAfterSignIn(me(), "/")).toBe("/beta");
  });
  it("falls back to the first org when the last one is gone", () => {
    expect(destinationAfterSignIn(me({ last_org_slug: "deleted" }), null)).toBe("/acme");
  });
  it("starts onboarding without any org", () => {
    expect(destinationAfterSignIn(me({ orgs: [], last_org_slug: null }), null)).toBe("/onboarding");
  });
});

describe("loginUrl", () => {
  it("passes a safe next through", () => {
    expect(loginUrl("/acme")).toBe("/api/v1/auth/github/login?next=%2Facme");
  });
  it("drops an unsafe one", () => {
    expect(loginUrl("//evil")).toBe("/api/v1/auth/github/login");
  });
});

describe("describeDevice", () => {
  it.each([
    [
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/156.0 Safari/537.36",
      "Chrome on macOS",
      false,
    ],
    [
      "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Version/18.0 Mobile Safari/604.1",
      "Safari on iOS",
      true,
    ],
    [
      "Mozilla/5.0 (Windows NT 10.0) Chrome/156.0 Safari/537.36 Edg/156.0",
      "Edge on Windows",
      false,
    ],
    ["Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Firefox/140.0", "Firefox on Linux", false],
    [null, "Unknown device", false],
  ])("%s", (userAgent, label, mobile) => {
    expect(describeDevice(userAgent)).toEqual({ label, mobile });
  });
});
