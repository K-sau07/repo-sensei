import { describe, expect, it } from "vitest";

import type { Member, Role } from "@/lib/api/types";

import { parseInvitee } from "./invite-dialog";
import { memberPermissions } from "./members-settings";
import { assignableRoles } from "./roles";

const member = (user_id: string, role: Role): Member => ({
  user_id,
  role,
  login: user_id,
  name: null,
  avatar_url: null,
  joined_at: "2026-10-18T12:00:00Z",
});

describe("parseInvitee", () => {
  it.each([
    ["friend@example.com", { email: "friend@example.com" }],
    ["  friend@example.com ", { email: "friend@example.com" }],
    ["@octo", { github_login: "octo" }],
    ["octo-cat", { github_login: "octo-cat" }],
    ["", null],
    ["not a login!", null],
    ["-starts-with-hyphen", null],
  ])("%s", (value, expected) => {
    expect(parseInvitee(value)).toEqual(expected);
  });
});

describe("member permissions (same rules as the API)", () => {
  it.each([
    // my role, their role, can manage
    ["owner", "owner", true],
    ["owner", "admin", true],
    ["admin", "admin", true],
    ["admin", "member", true],
    ["admin", "owner", false],
    ["member", "viewer", false],
    ["viewer", "member", false],
  ] as const)("a %s managing a %s: %s", (mine, theirs, allowed) => {
    const result = memberPermissions(mine, "me", member("them", theirs));
    expect(result.canChangeRole).toBe(allowed);
    expect(result.canRemove).toBe(allowed);
  });

  it("you leave rather than remove yourself", () => {
    expect(memberPermissions("owner", "me", member("me", "owner"))).toEqual({
      canChangeRole: false,
      canRemove: false,
      canLeave: true,
    });
  });

  it("only owners can hand out the owner role", () => {
    expect(assignableRoles("owner")).toContain("owner");
    expect(assignableRoles("admin")).not.toContain("owner");
  });
});
