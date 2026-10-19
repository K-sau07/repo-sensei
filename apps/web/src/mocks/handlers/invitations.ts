import { http, HttpResponse } from "msw";

import type { Invitation, InvitationCreated, InvitationPreview } from "@/lib/api/types";
import { db } from "@/mocks/db";

import { apiError } from "./errors";

export const invitationHandlers = [
  http.get<{ org: string }>("*/api/v1/orgs/:org/invitations", ({ params }) =>
    HttpResponse.json<Invitation[]>(db.invitations[params.org] ?? []),
  ),

  http.post<{ org: string }>("*/api/v1/orgs/:org/invitations", async ({ params, request }) => {
    const body = (await request.json()) as Partial<Invitation>;
    if (!body.email && !body.github_login) {
      return apiError(422, "invite_target_required", "Invite by email or GitHub username.");
    }
    const invitation: Invitation = {
      id: crypto.randomUUID(),
      email: body.email ?? null,
      github_login: body.github_login ?? null,
      role: body.role ?? "member",
      expires_at: "2026-10-25T12:00:00Z",
      created_at: "2026-10-18T12:00:00Z",
    };
    (db.invitations[params.org] ??= []).push(invitation);
    return HttpResponse.json<InvitationCreated>(
      { ...invitation, link: `http://localhost:3000/invite/mock-${invitation.id}` },
      { status: 201 },
    );
  }),

  http.delete<{ org: string; id: string }>("*/api/v1/orgs/:org/invitations/:id", ({ params }) => {
    const list = db.invitations[params.org] ?? [];
    db.invitations[params.org] = list.filter((invitation) => invitation.id !== params.id);
    return new HttpResponse(null, { status: 204 });
  }),

  http.get("*/api/v1/invitations/:token", () =>
    HttpResponse.json<InvitationPreview>({
      org_name: "Acme",
      org_slug: "acme",
      inviter_login: "octo",
      role: "member",
      status: "pending",
    }),
  ),
];
