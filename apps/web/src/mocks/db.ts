/**
 * In-memory data behind the mock API. Shapes come from the generated OpenAPI types, so the mocks
 * fail to compile if the API changes. `resetMockDb()` restores the seed (call it between tests).
 */
import type { Invitation, Me, Member, Org, Role, SessionInfo } from "@/lib/api/types";

export interface MockDb {
  me: Me;
  orgs: Org[];
  members: Record<string, Member[]>;
  invitations: Record<string, Invitation[]>;
  sessions: SessionInfo[];
}

const NOW = "2026-10-18T12:00:00Z";

function seed(): MockDb {
  const owner: Member = {
    user_id: "0199f000-0000-7000-8000-000000000001",
    login: "octo",
    name: "Octo Cat",
    avatar_url: null,
    role: "owner",
    joined_at: NOW,
  };
  return {
    me: {
      id: owner.user_id,
      login: owner.login,
      name: owner.name,
      email: "octo@example.com",
      avatar_url: null,
      is_staff: false,
      last_org_slug: "acme",
      orgs: [{ slug: "acme", name: "Acme", role: "owner" }],
    },
    orgs: [
      {
        id: "0199f000-0000-7000-8000-0000000000a1",
        slug: "acme",
        name: "Acme",
        avatar_url: null,
        plan: "free",
        role: "owner",
      },
    ],
    members: {
      acme: [
        owner,
        {
          user_id: "0199f000-0000-7000-8000-000000000002",
          login: "hubot",
          name: "Hubot",
          avatar_url: null,
          role: "member" satisfies Role,
          joined_at: NOW,
        },
      ],
    },
    sessions: [
      {
        id: "0199f000-0000-7000-8000-0000000000c1",
        current: true,
        ip: "127.0.0.1",
        user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/156.0",
        created_at: "2026-10-18T10:00:00Z",
        last_seen_at: "2026-10-18T12:00:00Z",
      },
      {
        id: "0199f000-0000-7000-8000-0000000000c2",
        current: false,
        ip: "203.0.113.7",
        user_agent:
          "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
        created_at: "2026-10-12T08:00:00Z",
        last_seen_at: "2026-10-17T21:30:00Z",
      },
    ],
    invitations: {
      acme: [
        {
          id: "0199f000-0000-7000-8000-0000000000b1",
          email: "friend@example.com",
          github_login: null,
          role: "member",
          expires_at: "2026-10-25T12:00:00Z",
          created_at: NOW,
        },
      ],
    },
  };
}

export let db: MockDb = seed();

export function resetMockDb(): void {
  db = seed();
}
