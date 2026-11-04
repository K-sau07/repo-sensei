import { describe, expect, it } from "vitest";

import { actionLabel, actorLabel, AUDIT_ACTIONS, targetLabel, type AuditEvent } from "./audit";

const event = (over: Partial<AuditEvent>): AuditEvent => ({
  id: "e1",
  action: "repo.connected",
  actor_type: "user",
  actor_user_id: "u1",
  actor_login: "octo",
  target_type: "repo",
  target_id: "r1",
  meta: {},
  ip: null,
  created_at: "2026-11-04T10:00:00Z",
  ...over,
});

describe("the audit log's words", () => {
  it("names every action the API records, and shows a newer one as written", () => {
    expect(AUDIT_ACTIONS).toContain("org.restore");
    expect(actionLabel("repo.connected")).toBe("Connected a repository");
    expect(actionLabel("brand.new")).toBe("brand.new");
  });

  it("says who acted: a person, repo-sensei itself, a key, an app", () => {
    expect(actorLabel(event({}))).toBe("@octo");
    expect(
      actorLabel(event({ actor_login: null, actor_type: "system", actor_user_id: null })),
    ).toBe("repo-sensei");
    expect(actorLabel(event({ actor_login: null, actor_type: "api_key" }))).toBe("an API key");
    expect(actorLabel(event({ actor_login: null }))).toBe("a deleted account");
  });

  it("says what it was done to, from its details", () => {
    expect(targetLabel(event({ meta: { full_name: "acme/api" } }))).toBe("acme/api");
    expect(
      targetLabel(event({ action: "member.role_change", meta: { from: "member", to: "admin" } })),
    ).toBe("member → admin");
    expect(targetLabel(event({ meta: { count: 3 } }))).toBeNull();
    const hubot = new Map([["u2", "hubot"]]);
    const promoted = event({ action: "member.role_change", target_type: "user", target_id: "u2",
                             meta: { from: "member", to: "admin" } }); // prettier-ignore
    expect(targetLabel(promoted, hubot)).toBe("@hubot: member → admin");
  });
});
