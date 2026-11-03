import { describe, expect, it } from "vitest";

import { GROUPS, inboxHref, isApproval, KIND_GROUP, kindsOf, type InboxItem } from "./inbox";

const item = (over: Partial<InboxItem> = {}): InboxItem => ({
  id: "0199f000-0000-7000-8000-000000000101",
  kind: "approval_needed",
  title: "Fix plan to approve: KeyError",
  body: "In acme/api, risk high.",
  link: "/acme/api/fixes/1",
  risk: "high",
  read: false,
  created_at: "2026-10-18T11:55:00Z",
  ...over,
});

describe("the inbox's kinds", () => {
  it("puts every kind in one of the groups", () => {
    for (const group of Object.values(KIND_GROUP)) expect(GROUPS).toContain(group);
  });

  it("asks for a group's kinds, or every kind", () => {
    expect(kindsOf("approvals")).toEqual(["approval_needed"]);
    expect(kindsOf("failures")).toEqual(["fix_failed", "index_failed", "cost_cap"]);
    expect(kindsOf("finished")).toEqual(["pr_opened", "index_done"]);
    expect(kindsOf(null)).toBeNull();
  });

  it("opens only approvals that say where", () => {
    expect(isApproval(item())).toBe(true);
    expect(isApproval(item({ link: null }))).toBe(false);
    expect(isApproval(item({ kind: "pr_opened" }))).toBe(false);
  });

  it("follows only paths in the app", () => {
    expect(inboxHref(item())).toBe("/acme/api/fixes/1");
    expect(inboxHref(item({ link: "//evil.example/x" }))).toBeNull();
    expect(inboxHref(item({ link: "https://evil.example/x" }))).toBeNull();
    expect(inboxHref(item({ link: "/\\evil.example/x" }))).toBeNull();
    expect(inboxHref(item({ link: "/acme\tapi" }))).toBeNull();
    expect(inboxHref(item({ link: null }))).toBeNull();
  });
});
