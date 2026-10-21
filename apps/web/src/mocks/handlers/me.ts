import { http, HttpResponse } from "msw";

import type { Me, MyOrg, SessionInfo } from "@/lib/api/types";
import { db, saveDb, type MockUser } from "@/mocks/db";
import { mockScenario } from "@/mocks/scenario";

import { apiError } from "./errors";
import { currentUser } from "./session";

export function meOf(user: MockUser): Me {
  const orgs: MyOrg[] =
    mockScenario() === "no-orgs"
      ? []
      : db.orgs.flatMap((org) => {
          const membership = db.members[org.slug]?.find((m) => m.user_id === user.id);
          return membership ? [{ slug: org.slug, name: org.name, role: membership.role }] : [];
        });
  const last = db.lastOrg[user.id] ?? null;
  return {
    id: user.id,
    login: user.login,
    name: user.name,
    email: user.email,
    avatar_url: null,
    is_staff: false,
    orgs,
    last_org_slug: orgs.some((org) => org.slug === last) ? last : null,
  };
}

export const meHandlers = [
  http.get("*/api/v1/me", () => {
    const user = currentUser();
    return user instanceof Response ? user : HttpResponse.json<Me>(meOf(user));
  }),

  http.patch("*/api/v1/me", async ({ request }) => {
    const user = currentUser();
    if (user instanceof Response) return user;
    const body = (await request.json()) as { last_org_slug?: string | null };
    const slug = body.last_org_slug ?? null;
    if (slug && !meOf(user).orgs.some((org) => org.slug === slug)) {
      return apiError(404, "org_not_found", "Organization not found.");
    }
    db.lastOrg[user.id] = slug;
    saveDb();
    return HttpResponse.json<Me>(meOf(user));
  }),

  http.get("*/api/v1/me/sessions", () => {
    const user = currentUser();
    return user instanceof Response ? user : HttpResponse.json<SessionInfo[]>(db.sessions);
  }),

  http.delete<{ id: string }>("*/api/v1/me/sessions/:id", ({ params }) => {
    const before = db.sessions.length;
    db.sessions = db.sessions.filter((session) => session.id !== params.id);
    saveDb();
    return db.sessions.length === before
      ? apiError(404, "session_not_found", "Session not found.")
      : new HttpResponse(null, { status: 204 });
  }),

  http.post("*/api/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),

  http.post("*/api/v1/auth/logout-all", () => {
    db.sessions = [];
    saveDb();
    return new HttpResponse(null, { status: 204 });
  }),
];
