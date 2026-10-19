import { http, HttpResponse } from "msw";

import type { Me, SessionInfo } from "@/lib/api/types";
import { db } from "@/mocks/db";

import { apiError } from "./errors";

export const meHandlers = [
  http.get("*/api/v1/me", () => HttpResponse.json<Me>(db.me)),

  http.patch("*/api/v1/me", async ({ request }) => {
    const body = (await request.json()) as { last_org_slug?: string | null };
    const slug = body.last_org_slug ?? null;
    if (slug && !db.me.orgs.some((org) => org.slug === slug)) {
      return apiError(404, "org_not_found", "Organization not found.");
    }
    db.me = { ...db.me, last_org_slug: slug };
    return HttpResponse.json<Me>(db.me);
  }),

  http.get("*/api/v1/me/sessions", () =>
    HttpResponse.json<SessionInfo[]>([
      {
        id: "0199f000-0000-7000-8000-0000000000c1",
        current: true,
        ip: "127.0.0.1",
        user_agent: "Mozilla/5.0",
        created_at: "2026-10-18T10:00:00Z",
        last_seen_at: "2026-10-18T12:00:00Z",
      },
    ]),
  ),

  http.post("*/api/v1/auth/logout", () => new HttpResponse(null, { status: 204 })),
];
