/**
 * The mock API's data: users, orgs, memberships and invitations, with the same rules as the real
 * API. In the browser it persists in localStorage, so multi-step flows (invite, switch user,
 * accept) survive page loads; `resetMockDb()` restores the seed.
 */
import type { Invitation, Member, Org, Role, SessionInfo } from "@/lib/api/types";

export interface MockUser {
  id: string;
  login: string;
  name: string | null;
  email: string | null;
}

export interface MockMembership {
  user_id: string;
  role: Role;
  joined_at: string;
}

export interface MockInvitation extends Invitation {
  org: string;
  token: string;
  status: "pending" | "accepted" | "revoked" | "expired";
  invited_by: string;
}

export interface MockDb {
  users: MockUser[];
  orgs: Omit<Org, "role">[];
  members: Record<string, MockMembership[]>;
  invitations: MockInvitation[];
  sessions: SessionInfo[];
  lastOrg: Record<string, string | null>;
}

const STORAGE_KEY = "sensei.mock.db.v2";
export const NOW = "2026-10-18T12:00:00Z";
export const OCTO = "0199f000-0000-7000-8000-000000000001";
export const HUBOT = "0199f000-0000-7000-8000-000000000002";
export const FRIEND = "0199f000-0000-7000-8000-000000000003";

function seed(): MockDb {
  return {
    users: [
      { id: OCTO, login: "octo", name: "Octo Cat", email: "octo@example.com" },
      { id: HUBOT, login: "hubot", name: "Hubot", email: "hubot@example.com" },
      { id: FRIEND, login: "friend", name: "A Friend", email: "friend@example.com" },
    ],
    orgs: [
      {
        id: "0199f000-0000-7000-8000-0000000000a1",
        slug: "acme",
        name: "Acme",
        avatar_url: null,
        plan: "free",
      },
    ],
    members: {
      acme: [
        { user_id: OCTO, role: "owner", joined_at: NOW },
        { user_id: HUBOT, role: "member", joined_at: NOW },
      ],
    },
    invitations: [
      {
        id: "0199f000-0000-7000-8000-0000000000b1",
        org: "acme",
        token: "seed-pending-token",
        status: "pending",
        invited_by: OCTO,
        email: "someone@example.com",
        github_login: null,
        role: "member",
        expires_at: "2026-10-25T12:00:00Z",
        created_at: NOW,
      },
      {
        id: "0199f000-0000-7000-8000-0000000000b2",
        org: "acme",
        token: "seed-expired-token",
        status: "expired",
        invited_by: OCTO,
        email: "late@example.com",
        github_login: null,
        role: "member",
        expires_at: "2026-10-01T12:00:00Z",
        created_at: "2026-09-24T12:00:00Z",
      },
      {
        id: "0199f000-0000-7000-8000-0000000000b3",
        org: "acme",
        token: "seed-accepted-token",
        status: "accepted",
        invited_by: OCTO,
        email: "hubot@example.com",
        github_login: null,
        role: "member",
        expires_at: "2026-10-25T12:00:00Z",
        created_at: NOW,
      },
    ],
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
    lastOrg: { [OCTO]: "acme" },
  };
}

function load(): MockDb {
  if (typeof localStorage === "undefined") return seed();
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as MockDb) : seed();
  } catch {
    return seed();
  }
}

export let db: MockDb = load();

/** Call after every change. */
export function saveDb(): void {
  if (typeof localStorage !== "undefined") localStorage.setItem(STORAGE_KEY, JSON.stringify(db));
}

export function resetMockDb(): void {
  if (typeof localStorage !== "undefined") localStorage.removeItem(STORAGE_KEY);
  db = seed();
}

export function membersOf(slug: string): Member[] {
  return (db.members[slug] ?? []).map((membership) => {
    const user = db.users.find((u) => u.id === membership.user_id);
    return {
      user_id: membership.user_id,
      login: user?.login ?? "unknown",
      name: user?.name ?? null,
      avatar_url: null,
      role: membership.role,
      joined_at: membership.joined_at,
    };
  });
}

export function roleIn(slug: string, userId: string): Role | null {
  return db.members[slug]?.find((m) => m.user_id === userId)?.role ?? null;
}

const RANK: Record<Role, number> = { viewer: 0, member: 1, admin: 2, owner: 3 };
export const atLeast = (role: Role | null, minimum: Role) =>
  role !== null && RANK[role] >= RANK[minimum];
