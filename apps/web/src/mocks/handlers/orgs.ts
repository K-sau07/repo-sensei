import { http, HttpResponse } from "msw";

import type { Org, Role } from "@/lib/api/types";
import { slugProblem, SLUG_MESSAGES } from "@/lib/orgs/slug";
import { atLeast, db, NOW, roleIn, saveDb } from "@/mocks/db";

import { apiError } from "./errors";
import { currentUser, insufficientRole } from "./session";

function withRole(slug: string, role: Role): Org | null {
  const org = db.orgs.find((o) => o.slug === slug);
  return org ? { ...org, role } : null;
}

/** The caller's view of an org, or the 404 a non-member gets (existence isn't leaked). */
export function orgAccess(slug: string) {
  const user = currentUser();
  if (user instanceof Response) return user;
  const role = roleIn(slug, user.id);
  const org = role ? withRole(slug, role) : null;
  if (!org || !role) return apiError(404, "org_not_found", "Organization not found.");
  return { user, role, org };
}

export const orgHandlers = [
  http.get("*/api/v1/orgs", () => {
    const user = currentUser();
    if (user instanceof Response) return user;
    return HttpResponse.json<Org[]>(
      db.orgs.flatMap((org) => {
        const role = roleIn(org.slug, user.id);
        return role ? [{ ...org, role }] : [];
      }),
    );
  }),

  http.get("*/api/v1/orgs/slug-available", ({ request }) => {
    const slug = (new URL(request.url).searchParams.get("slug") ?? "").trim().toLowerCase();
    const problem = slugProblem(slug);
    if (problem) return HttpResponse.json({ slug, available: false, reason: problem });
    const taken = db.orgs.some((org) => org.slug === slug);
    return HttpResponse.json({ slug, available: !taken, reason: taken ? "slug_taken" : null });
  }),

  http.post("*/api/v1/orgs", async ({ request }) => {
    const user = currentUser();
    if (user instanceof Response) return user;
    const body = (await request.json()) as { name: string; slug: string };
    const slug = body.slug.trim().toLowerCase();
    const problem = slugProblem(slug);
    if (problem) return apiError(422, problem, SLUG_MESSAGES[problem] ?? "Invalid name.");
    if (!body.name.trim()) return apiError(422, "invalid_name", "Give the organization a name.");
    if (db.orgs.some((org) => org.slug === slug)) {
      return apiError(409, "slug_taken", "That name is already taken.");
    }
    const org = {
      id: crypto.randomUUID(),
      slug,
      name: body.name.trim(),
      avatar_url: null,
      plan: "free",
    };
    db.orgs.push(org);
    db.members[slug] = [{ user_id: user.id, role: "owner", joined_at: NOW }];
    db.lastOrg[user.id] = slug;
    saveDb();
    return HttpResponse.json<Org>({ ...org, role: "owner" }, { status: 201 });
  }),

  http.get<{ org: string }>("*/api/v1/orgs/:org", ({ params }) => {
    const access = orgAccess(params.org);
    return access instanceof Response ? access : HttpResponse.json<Org>(access.org);
  }),

  http.patch<{ org: string }>("*/api/v1/orgs/:org", async ({ params, request }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    if (!atLeast(access.role, "admin")) return insufficientRole("admin", access.role);
    const body = (await request.json()) as { name?: string | null; slug?: string | null };
    const org = db.orgs.find((o) => o.slug === params.org);
    if (!org) return apiError(404, "org_not_found", "Organization not found.");
    if (body.name !== undefined && body.name !== null) {
      if (!body.name.trim()) return apiError(422, "invalid_name", "Give the organization a name.");
      org.name = body.name.trim();
    }
    if (body.slug !== undefined && body.slug !== null) {
      const slug = body.slug.trim().toLowerCase();
      if (slug !== org.slug) {
        const problem = slugProblem(slug);
        if (problem) return apiError(422, problem, SLUG_MESSAGES[problem] ?? "Invalid name.");
        if (db.orgs.some((o) => o.slug === slug)) {
          return apiError(409, "slug_taken", "That name is already taken.");
        }
        const { [org.slug]: moved = [], ...others } = db.members;
        db.members = { ...others, [slug]: moved };
        for (const invitation of db.invitations)
          if (invitation.org === org.slug) invitation.org = slug;
        for (const [userId, last] of Object.entries(db.lastOrg)) {
          if (last === org.slug) db.lastOrg[userId] = slug;
        }
        org.slug = slug;
      }
    }
    saveDb();
    return HttpResponse.json<Org>({ ...org, role: access.role });
  }),
];
