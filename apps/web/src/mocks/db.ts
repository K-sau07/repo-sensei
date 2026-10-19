/**
 * In-memory data behind the mock API. Shapes come from the generated OpenAPI types, so the mocks
 * fail to compile if the API changes. `resetMockDb()` restores the seed (call it between tests).
 */
import type { Invitation, Me, Member, Org, Role } from "@/lib/api/types";

export interface MockDb {
  me: Me;
  orgs: Org[];
  members: Record<string, Member[]>;
  invitations: Record<string, Invitation[]>;
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
