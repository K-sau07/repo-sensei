/**
 * The mock audit log (10.7.2): a few days of what an org records, newest first, filtered by
 * action and actor and paged by `before` as the API pages it.
 */
import { http, HttpResponse } from "msw";

import type { AuditEvent } from "@/lib/orgs/audit";
import { atLeast, HUBOT, OCTO } from "@/mocks/db";

import { orgAccess } from "./orgs";
import { insufficientRole } from "./session";

function seeded(): AuditEvent[] {
  const at = (hours: number) => new Date(Date.parse("2026-10-18T12:00:00Z") - hours * 3600_000);
  const rows: [string, string | null, string | null, Record<string, unknown>][] = [
    ["repo.settings_update", OCTO, "octo", { before: {}, after: {} }],
    ["member.role_change", OCTO, "octo", { from: "member", to: "admin" }],
    ["api_key.create", HUBOT, "hubot", { name: "ci" }],
    ["repo.index_started", null, null, { full_name: "acme-gh/api" }],
    ["repo.connected", OCTO, "octo", { full_name: "acme-gh/api" }],
    ["invitation.create", OCTO, "octo", { email: "friend@example.com", role: "member" }],
    ["org.create", OCTO, "octo", { slug: "acme", name: "Acme" }],
  ];
  return rows.map(([action, user, login, meta], i) => ({
    id: `0199f000-0000-7000-8000-00000000a${String(i).padStart(3, "0")}`,
    action,
    actor_type: user === null ? "system" : "user",
    actor_user_id: user,
    actor_login: login,
    target_type: null,
    target_id: null,
    meta,
    ip: user === null ? null : "203.0.113.7",
    created_at: at(i * 7).toISOString(),
  }));
}

export const auditHandlers = [
  http.get<{ org: string }>("*/api/v1/orgs/:org/audit", ({ params, request }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    if (!atLeast(access.role, "admin")) return insufficientRole("admin", access.role);
    const query = new URL(request.url).searchParams;
    const limit = Number(query.get("limit") ?? 50);
    const action = query.get("action");
    const actor = query.get("actor");
    const before = query.get("before");
    let rows = seeded().filter(
      (e) => (!action || e.action === action) && (!actor || e.actor_user_id === actor),
    );
    if (before) rows = rows.slice(rows.findIndex((e) => e.id === before) + 1);
    const items = rows.slice(0, limit);
    return HttpResponse.json({
      items,
      next_cursor: items.length === limit ? (items.at(-1)?.id ?? null) : null,
    });
  }),
];
