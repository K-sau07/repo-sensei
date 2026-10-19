import { http, HttpResponse } from "msw";

import type { Member, Role } from "@/lib/api/types";
import { atLeast, db, membersOf, saveDb } from "@/mocks/db";

import { apiError } from "./errors";
import { orgAccess } from "./orgs";
import { insufficientRole } from "./session";

const ownerCount = (slug: string) =>
  (db.members[slug] ?? []).filter((m) => m.role === "owner").length;

const lastOwner = () => apiError(409, "last_owner", "An organization needs at least one owner.");

export const memberHandlers = [
  http.get<{ org: string }>("*/api/v1/orgs/:org/members", ({ params }) => {
    const access = orgAccess(params.org);
    return access instanceof Response ? access : HttpResponse.json<Member[]>(membersOf(params.org));
  }),

  http.patch<{ org: string; userId: string }>(
    "*/api/v1/orgs/:org/members/:userId",
    async ({ params, request }) => {
      const access = orgAccess(params.org);
      if (access instanceof Response) return access;
      if (!atLeast(access.role, "admin")) return insufficientRole("admin", access.role);
      const { role } = (await request.json()) as { role: Role };
      const membership = db.members[params.org]?.find((m) => m.user_id === params.userId);
      if (!membership) return apiError(404, "member_not_found", "Member not found.");
      if ((role === "owner" || membership.role === "owner") && access.role !== "owner") {
        return insufficientRole("owner", access.role);
      }
      if (membership.role === "owner" && role !== "owner" && ownerCount(params.org) <= 1) {
        return lastOwner();
      }
      membership.role = role;
      saveDb();
      const member = membersOf(params.org).find((m) => m.user_id === params.userId);
      return member
        ? HttpResponse.json<Member>(member)
        : apiError(404, "member_not_found", "Member not found.");
    },
  ),

  http.delete<{ org: string; userId: string }>(
    "*/api/v1/orgs/:org/members/:userId",
    ({ params }) => {
      const access = orgAccess(params.org);
      if (access instanceof Response) return access;
      if (!atLeast(access.role, "admin")) return insufficientRole("admin", access.role);
      const membership = db.members[params.org]?.find((m) => m.user_id === params.userId);
      if (!membership) return apiError(404, "member_not_found", "Member not found.");
      if (membership.role === "owner") {
        if (access.role !== "owner") return insufficientRole("owner", access.role);
        if (ownerCount(params.org) <= 1) return lastOwner();
      }
      db.members[params.org] = (db.members[params.org] ?? []).filter(
        (m) => m.user_id !== params.userId,
      );
      saveDb();
      return new HttpResponse(null, { status: 204 });
    },
  ),

  http.post<{ org: string }>("*/api/v1/orgs/:org/leave", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    if (access.role === "owner" && ownerCount(params.org) <= 1) return lastOwner();
    db.members[params.org] = (db.members[params.org] ?? []).filter(
      (m) => m.user_id !== access.user.id,
    );
    saveDb();
    return new HttpResponse(null, { status: 204 });
  }),
];
