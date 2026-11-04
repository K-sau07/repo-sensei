import { describe, expect, it } from "vitest";

import type { Me } from "@/lib/api/types";

import { handoffPath } from "./new-repo-handoff";

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

const FLASK = "https://github.com/pallets/flask";

describe("handoffPath", () => {
  it("opens the last org's URL tab with the repository filled in", () => {
    expect(handoffPath(me({ last_org_slug: "acme" }), FLASK)).toBe(
      `/acme/repos/new?tab=url&url=${encodeURIComponent(FLASK)}`,
    );
  });
  it("passes over an org they can't import into (members don't)", () => {
    expect(handoffPath(me(), null)).toBe("/acme/repos/new?tab=url"); // last used: beta, a member
  });
  it("falls back to the first org they may import into", () => {
    expect(handoffPath(me({ last_org_slug: "gone" }), null)).toBe("/acme/repos/new?tab=url");
  });
  it("starts onboarding when they may import into none", () => {
    const member = me({ orgs: [{ slug: "beta", name: "Beta", role: "member" }] });
    expect(handoffPath(member, null)).toBe("/onboarding");
  });
  it("keeps the repository through onboarding without an org", () => {
    expect(handoffPath(me({ orgs: [], last_org_slug: null }), FLASK)).toBe(
      `/onboarding?url=${encodeURIComponent(FLASK)}`,
    );
  });
});
