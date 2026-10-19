import { http, HttpResponse } from "msw";

import type { Org } from "@/lib/api/types";
import { db } from "@/mocks/db";

import { apiError } from "./errors";

function findOrg(slug: string) {
  return db.orgs.find((org) => org.slug === slug);
}

export const orgHandlers = [
  http.get("*/api/v1/orgs", () => HttpResponse.json<Org[]>(db.orgs)),

  http.get("*/api/v1/orgs/slug-available", ({ request }) => {
    const slug = new URL(request.url).searchParams.get("slug") ?? "";
    const available = !findOrg(slug);
    return HttpResponse.json({ slug, available, reason: available ? null : "slug_taken" });
  }),

  http.post("*/api/v1/orgs", async ({ request }) => {
    const body = (await request.json()) as { name: string; slug: string };
    if (findOrg(body.slug)) return apiError(409, "slug_taken", "That name is already taken.");
    const org: Org = {
      id: crypto.randomUUID(),
      slug: body.slug,
      name: body.name,
      avatar_url: null,
      plan: "free",
      role: "owner",
    };
    db.orgs.push(org);
    db.me.orgs.push({ slug: org.slug, name: org.name, role: "owner" });
    return HttpResponse.json<Org>(org, { status: 201 });
  }),

  http.get<{ org: string }>("*/api/v1/orgs/:org", ({ params }) => {
    const org = findOrg(params.org);
    return org
      ? HttpResponse.json<Org>(org)
      : apiError(404, "org_not_found", "Organization not found.");
  }),
];
