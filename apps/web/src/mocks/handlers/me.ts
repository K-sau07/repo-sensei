import { http, HttpResponse } from "msw";

import type { Me, MyOrg, SessionInfo } from "@/lib/api/types";
import { db, roleIn, saveDb, type MockUser } from "@/mocks/db";
import { mockScenario } from "@/mocks/scenario";

import { apiError } from "./errors";
import { currentUser } from "./session";

export function meOf(user: MockUser): Me {
  const orgs: MyOrg[] =
    mockScenario() === "no-orgs"
      ? []
      : db.orgs.flatMap((org) => {
          const role = roleIn(org.slug, user.id);
          return role ? [{ slug: org.slug, name: org.name, role }] : [];
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

  http.delete("*/api/v1/me", async ({ request }) => {
    const user = currentUser();
    if (user instanceof Response) return user;
    const body = (await request.json()) as { confirm?: string };
    if ((body.confirm ?? "").trim().toLowerCase() !== user.login.toLowerCase()) {
      return apiError(422, "confirmation_mismatch", "Type your GitHub login to confirm.");
    }
    const sole = db.orgs.filter((org) => {
      const owners = (db.members[org.slug] ?? []).filter((m) => m.role === "owner");
      return owners.length === 1 && owners[0]?.user_id === user.id;
    });
    if (sole.length > 0) {
      return apiError(
        409,
        "sole_owner",
        "You're the only owner of an organization: make someone else an owner, or delete it, first.",
        { orgs: sole.map((org) => org.slug) },
      );
    }
    for (const slug of Object.keys(db.members)) {
      db.members[slug] = (db.members[slug] ?? []).filter((m) => m.user_id !== user.id);
    }
    user.deleted = true;
    db.sessions = [];
    saveDb();
    return new HttpResponse(null, { status: 204 });
  }),

  http.post("*/api/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),

  http.post("*/api/v1/auth/logout-all", () => {
    db.sessions = [];
    saveDb();
    return new HttpResponse(null, { status: 204 });
  }),
];
