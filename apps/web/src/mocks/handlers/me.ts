import { http, HttpResponse } from "msw";

import type { Me, SessionInfo } from "@/lib/api/types";
import { db } from "@/mocks/db";
import { mockScenario } from "@/mocks/scenario";

import { apiError } from "./errors";

function signedOutError() {
  const scenario = mockScenario();
  if (scenario === "signed-out") return apiError(401, "unauthenticated", "Sign in to continue.");
  if (scenario === "expired") {
    return apiError(401, "session_expired", "Your session has expired. Please sign in again.");
  }
  return null;
}

export const meHandlers = [
  http.get("*/api/v1/me", () => {
    const error = signedOutError();
    if (error) return error;
    const me = mockScenario() === "no-orgs" ? { ...db.me, orgs: [], last_org_slug: null } : db.me;
    return HttpResponse.json<Me>(me);
  }),

  http.patch("*/api/v1/me", async ({ request }) => {
    const body = (await request.json()) as { last_org_slug?: string | null };
    const slug = body.last_org_slug ?? null;
    if (slug && !db.me.orgs.some((org) => org.slug === slug)) {
      return apiError(404, "org_not_found", "Organization not found.");
    }
    db.me = { ...db.me, last_org_slug: slug };
    return HttpResponse.json<Me>(db.me);
  }),

  http.get(
    "*/api/v1/me/sessions",
    () => signedOutError() ?? HttpResponse.json<SessionInfo[]>(db.sessions),
  ),

  http.delete<{ id: string }>("*/api/v1/me/sessions/:id", ({ params }) => {
    const before = db.sessions.length;
    db.sessions = db.sessions.filter((session) => session.id !== params.id);
    return db.sessions.length === before
      ? apiError(404, "session_not_found", "Session not found.")
      : new HttpResponse(null, { status: 204 });
  }),

  http.post("*/api/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),

  http.post("*/api/v1/auth/logout-all", () => {
    db.sessions = [];
    return new HttpResponse(null, { status: 204 });
  }),
];
