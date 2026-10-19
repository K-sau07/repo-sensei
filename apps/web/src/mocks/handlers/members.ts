import { http, HttpResponse } from "msw";

import type { Member } from "@/lib/api/types";
import { db } from "@/mocks/db";

import { apiError } from "./errors";

export const memberHandlers = [
  http.get<{ org: string }>("*/api/v1/orgs/:org/members", ({ params }) => {
    const members = db.members[params.org];
    return members
      ? HttpResponse.json<Member[]>(members)
      : apiError(404, "org_not_found", "Organization not found.");
  }),

  http.patch<{ org: string; userId: string }>(
    "*/api/v1/orgs/:org/members/:userId",
    async ({ params, request }) => {
      const body = (await request.json()) as { role: Member["role"] };
      const member = db.members[params.org]?.find((m) => m.user_id === params.userId);
      if (!member) return apiError(404, "member_not_found", "Member not found.");
      member.role = body.role;
      return HttpResponse.json<Member>(member);
    },
  ),

  http.delete<{ org: string; userId: string }>(
    "*/api/v1/orgs/:org/members/:userId",
    ({ params }) => {
      const members = db.members[params.org] ?? [];
      db.members[params.org] = members.filter((m) => m.user_id !== params.userId);
      return new HttpResponse(null, { status: 204 });
    },
  ),
];
