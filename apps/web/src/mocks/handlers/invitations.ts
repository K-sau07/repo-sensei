import { http, HttpResponse } from "msw";

import type { Invitation, InvitationCreated, InvitationPreview, Org, Role } from "@/lib/api/types";
import { atLeast, db, NOW, saveDb, type MockInvitation } from "@/mocks/db";

import { apiError } from "./errors";
import { orgAccess } from "./orgs";
import { currentUser, insufficientRole } from "./session";

function publicShape(invitation: MockInvitation): Invitation {
  const { id, email, github_login, role, expires_at, created_at } = invitation;
  return { id, email, github_login, role, expires_at, created_at };
}

/** Trimmed text, or null when nothing is left (the API treats "" as not given). */
function blankToNull(value: string | null | undefined): string | null {
  const trimmed = value?.trim() ?? "";
  return trimmed === "" ? null : trimmed;
}

const sameText = (a: string | null, b: string | null) =>
  a !== null && b !== null && a.toLowerCase() === b.toLowerCase();

const notFound = () => apiError(404, "invitation_not_found", "This invitation link is not valid.");

export const invitationHandlers = [
  http.get<{ org: string }>("*/api/v1/orgs/:org/invitations", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    if (!atLeast(access.role, "admin")) return insufficientRole("admin", access.role);
    return HttpResponse.json<Invitation[]>(
      db.invitations.filter((i) => i.org === params.org && i.status === "pending").map(publicShape),
    );
  }),

  http.post<{ org: string }>("*/api/v1/orgs/:org/invitations", async ({ params, request }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    if (!atLeast(access.role, "admin")) return insufficientRole("admin", access.role);
    const body = (await request.json()) as {
      email?: string | null;
      github_login?: string | null;
      role?: Role;
    };
    const role = body.role ?? "member";
    if (role === "owner" && access.role !== "owner") return insufficientRole("owner", access.role);
    const email = blankToNull(body.email);
    const githubLogin = blankToNull(body.github_login?.replace(/^\s*@/, ""));
    if (!email && !githubLogin) {
      return apiError(422, "invite_target_required", "Invite by email or GitHub username.");
    }
    const invitation: MockInvitation = {
      id: crypto.randomUUID(),
      org: params.org,
      token: `mock-${crypto.randomUUID()}`,
      status: "pending",
      invited_by: access.user.id,
      email,
      github_login: githubLogin,
      role,
      expires_at: "2026-10-25T12:00:00Z",
      created_at: NOW,
    };
    db.invitations.push(invitation);
    saveDb();
    return HttpResponse.json<InvitationCreated>(
      { ...publicShape(invitation), link: `${location.origin}/invite/${invitation.token}` },
      { status: 201 },
    );
  }),

  http.delete<{ org: string; id: string }>("*/api/v1/orgs/:org/invitations/:id", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    if (!atLeast(access.role, "admin")) return insufficientRole("admin", access.role);
    const invitation = db.invitations.find((i) => i.id === params.id && i.org === params.org);
    if (!invitation) return notFound();
    if (invitation.status === "pending") invitation.status = "revoked";
    saveDb();
    return new HttpResponse(null, { status: 204 });
  }),

  http.get<{ token: string }>("*/api/v1/invitations/:token", ({ params }) => {
    const invitation = db.invitations.find((i) => i.token === params.token);
    const org = invitation && db.orgs.find((o) => o.slug === invitation.org);
    if (!invitation || !org) return notFound();
    const inviter = db.users.find((u) => u.id === invitation.invited_by);
    return HttpResponse.json<InvitationPreview>({
      org_name: org.name,
      org_slug: org.slug,
      inviter_login: inviter?.login ?? null,
      role: invitation.role,
      status: invitation.status,
    });
  }),

  http.post<{ token: string }>("*/api/v1/invitations/:token/accept", ({ params }) => {
    const user = currentUser();
    if (user instanceof Response) return user;
    const invitation = db.invitations.find((i) => i.token === params.token);
    const org = invitation && db.orgs.find((o) => o.slug === invitation.org);
    if (!invitation || !org) return notFound();
    if (invitation.status !== "pending") {
      return apiError(
        409,
        `invitation_${invitation.status}`,
        `This invitation was ${invitation.status}.`,
      );
    }
    const matches =
      sameText(invitation.email, user.email) || sameText(invitation.github_login, user.login);
    if (!matches) {
      return apiError(
        403,
        "invitation_wrong_account",
        "This invitation was sent to a different account.",
      );
    }
    const members = (db.members[org.slug] ??= []);
    let membership = members.find((m) => m.user_id === user.id);
    if (!membership) {
      membership = { user_id: user.id, role: invitation.role, joined_at: NOW };
      members.push(membership);
    }
    invitation.status = "accepted";
    db.lastOrg[user.id] = org.slug;
    saveDb();
    return HttpResponse.json<Org>({ ...org, role: membership.role });
  }),
];
