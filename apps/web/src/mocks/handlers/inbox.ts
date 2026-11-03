import { http, HttpResponse, sse } from "msw";

import type { InboxItem, InboxPage, NotificationKind, Preferences } from "@/lib/api/inbox";
import { atLeast, db, OCTO, roleIn, saveDb } from "@/mocks/db";

import { apiError } from "./errors";
import { orgAccess } from "./orgs";
import { currentUser, insufficientRole } from "./session";

const FIX = (n: number) =>
  `/acme/api/fixes/0199f000-0000-7000-8000-00000000f${String(n).padStart(3, "0")}`;
const id = (n: number) => `0199f000-0000-7000-8000-0000000001${String(n).padStart(2, "0")}`;

const LABELS: Record<NotificationKind, string> = {
  approval_needed: "Approvals needed",
  pr_opened: "Pull requests opened",
  fix_failed: "Fixes and changes that stopped",
  index_done: "Indexing done",
  index_failed: "Indexing failed",
  cost_cap: "Spend near the cap",
};
const KINDS = Object.keys(LABELS) as NotificationKind[];

/** Octo's inbox in Acme: two plans waiting (one high-risk), a fix that couldn't be
 * reproduced, a pull request opened, the repository indexed, the spend warning. */
function seed(): InboxItem[] {
  const item = (n: number, rest: Omit<InboxItem, "id">): InboxItem => ({ id: id(n), ...rest });
  return [
    item(6, {
      kind: "approval_needed",
      title: "Fix plan to approve: Refunds are applied twice when the request is retried",
      body: "In acme/api, risk high.",
      link: FIX(5),
      risk: "high",
      read: false,
      created_at: "2026-10-18T11:55:00Z",
    }),
    item(5, {
      kind: "approval_needed",
      title: "Fix plan to approve: Items page crashes when an account has no items",
      body: "In acme/api, risk low.",
      link: FIX(1),
      risk: "low",
      read: false,
      created_at: "2026-10-18T11:30:00Z",
    }),
    item(4, {
      kind: "fix_failed",
      title:
        "Fix couldn't be reproduced: Login redirects to a blank page after the session expires",
      body: "In acme/api. No reproducing test: it needs a real browser to expire the cookie.",
      link: FIX(3),
      risk: null,
      read: false,
      created_at: "2026-10-17T17:00:00Z",
    }),
    item(3, {
      kind: "pr_opened",
      title: "Draft pull request opened: Invoice totals are a cent short with a discount",
      body: "In acme/api: https://github.com/acme/api/pull/42",
      link: FIX(4),
      risk: null,
      read: true,
      created_at: "2026-10-16T10:00:00Z",
    }),
    item(2, {
      kind: "index_done",
      title: "acme/api is indexed",
      body: "Its code browser, search and Ask are ready.",
      link: "/acme/api",
      risk: null,
      read: true,
      created_at: "2026-10-15T09:00:00Z",
    }),
    item(1, {
      kind: "cost_cap",
      title: "Agent spend is at 82% of today's cap",
      body: "$41.00 of $50.00 so far today (UTC). Past the cap, no new agent run starts until midnight UTC.",
      link: null,
      risk: null,
      read: true,
      created_at: "2026-10-14T15:00:00Z",
    }),
  ];
}

export function inboxOf(org: string, userId: string): InboxItem[] {
  db.inbox ??= {};
  const key = `${org}:${userId}`;
  db.inbox[key] ??= org === "acme" && userId === OCTO ? seed() : [];
  return db.inbox[key] ?? [];
}

function preferencesOf(userId: string): Preferences {
  db.notificationPrefs ??= {};
  const saved = db.notificationPrefs[userId] ?? {};
  const email = db.users.find((user) => user.id === userId)?.email ?? null;
  return {
    email,
    kinds: KINDS.map((kind) => ({
      kind,
      label: LABELS[kind],
      in_app: saved[kind]?.in_app ?? true,
      email: saved[kind]?.email ?? kind !== "index_done",
    })),
  };
}

const inboxEvents =
  typeof EventSource === "undefined"
    ? [] // Node (unit tests): no EventSource, so no SSE handler either
    : [
        sse<{ message: string }, { org: string }>(
          "*/api/v1/orgs/:org/inbox/events",
          ({ client, params, request }) => {
            const user = currentUser();
            if (user instanceof Response) return;
            // What arrives from now on (anything added to the mock inbox), as the real stream.
            let known = new Set(inboxOf(params.org, user.id).map((item) => item.id));
            const timer = setInterval(() => {
              for (const item of inboxOf(params.org, user.id)) {
                if (known.has(item.id)) continue;
                client.send({
                  data: JSON.stringify({ type: "inbox.new", kind: item.kind, key: item.id,
                                         title: item.title, link: item.link }), // prettier-ignore
                });
              }
              known = new Set(inboxOf(params.org, user.id).map((item) => item.id));
            }, 500);
            request.signal.addEventListener("abort", () => {
              clearInterval(timer);
            });
          },
        ),
      ];

export const inboxHandlers = [
  ...inboxEvents,

  http.get<{ org: string }>("*/api/v1/orgs/:org/inbox", ({ params, request }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const query = new URL(request.url).searchParams;
    const limit = Math.min(100, Number(query.get("limit") ?? 50));
    const before = query.get("before");
    const kinds = query.getAll("kinds");
    const all = inboxOf(params.org, access.user.id);
    const shown = all
      .filter((item) => query.get("unread") !== "true" || !item.read)
      .filter((item) => kinds.length === 0 || kinds.includes(item.kind))
      .filter((item) => !before || item.id < before)
      .sort((a, b) => b.id.localeCompare(a.id));
    const page = shown.slice(0, limit);
    return HttpResponse.json<InboxPage>({
      items: page,
      unread: all.filter((item) => !item.read).length,
      next_cursor: page.length === limit ? (page.at(-1)?.id ?? null) : null,
    });
  }),

  http.post<{ org: string }>("*/api/v1/orgs/:org/inbox/read", async ({ params, request }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    const body = (await request.json()) as { ids?: string[] | null; kinds?: string[] | null };
    const all = inboxOf(params.org, access.user.id);
    for (const item of all) {
      const picked = body.ids
        ? body.ids.includes(item.id)
        : !body.kinds?.length || body.kinds.includes(item.kind);
      if (picked) item.read = true;
    }
    saveDb();
    return HttpResponse.json({ unread: all.filter((item) => !item.read).length });
  }),

  http.get("*/api/v1/me/notifications", () => {
    const user = currentUser();
    return user instanceof Response ? user : HttpResponse.json(preferencesOf(user.id));
  }),

  http.patch("*/api/v1/me/notifications", async ({ request }) => {
    const user = currentUser();
    if (user instanceof Response) return user;
    const body = (await request.json()) as {
      kinds: Partial<Record<NotificationKind, { in_app?: boolean | null; email?: boolean | null }>>;
    };
    const current = preferencesOf(user.id);
    db.notificationPrefs ??= {};
    const saved: Partial<Record<NotificationKind, { in_app: boolean; email: boolean }>> = {};
    for (const kind of current.kinds) {
      const patch = body.kinds[kind.kind] ?? {};
      saved[kind.kind] = {
        in_app: patch.in_app ?? kind.in_app,
        email: patch.email ?? kind.email,
      };
    }
    db.notificationPrefs[user.id] = saved;
    saveDb();
    return HttpResponse.json(preferencesOf(user.id));
  }),

  // ------------------------------------------------------------ the org's Slack (admins)

  http.get<{ org: string }>("*/api/v1/orgs/:org/integrations/slack", ({ params }) => {
    const admin = slackAdmin(params.org);
    if (admin) return admin;
    const found = db.slack?.[params.org];
    if (!found) return apiError(404, "slack_not_connected", "Slack isn't connected.");
    return HttpResponse.json(slackOut(found));
  }),

  http.put<{ org: string }>(
    "*/api/v1/orgs/:org/integrations/slack",
    async ({ params, request }) => {
      const admin = slackAdmin(params.org);
      if (admin) return admin;
      const body = (await request.json()) as {
        webhook_url?: string | null;
        channel?: string;
        kinds?: NotificationKind[];
      };
      const url = body.webhook_url?.trim();
      if (url && !SLACK_WEBHOOK.test(url)) {
        return apiError(422, "slack_webhook",
        "That isn't a Slack incoming webhook (https://hooks.slack.com/services/…)."); // prettier-ignore
      }
      const found = db.slack?.[params.org];
      if (!found && !url) {
        return apiError(422, "slack_webhook", "Connecting Slack needs its incoming webhook's URL.");
      }
      db.slack ??= {};
      const saved = {
        webhook_url: url ?? found?.webhook_url ?? "",
        channel: (body.channel ?? "").trim(),
        kinds: [...new Set(body.kinds ?? [])].sort(),
      };
      db.slack[params.org] = saved;
      saveDb();
      return HttpResponse.json(slackOut(saved));
    },
  ),

  http.delete<{ org: string }>("*/api/v1/orgs/:org/integrations/slack", ({ params }) => {
    const admin = slackAdmin(params.org);
    if (admin) return admin;
    if (db.slack)
      db.slack = Object.fromEntries(Object.entries(db.slack).filter(([org]) => org !== params.org));
    saveDb();
    return new HttpResponse(null, { status: 204 });
  }),
];

const SLACK_WEBHOOK =
  /^https:\/\/hooks\.slack\.com\/services\/[A-Za-z0-9_-]+(\/[A-Za-z0-9_-]+){1,3}$/;

/** A refusal for someone who isn't the org's admin (null: go on). */
function slackAdmin(org: string): Response | null {
  const access = orgAccess(org);
  if (access instanceof Response) return access;
  const role = roleIn(org, access.user.id);
  return atLeast(role, "admin") ? null : insufficientRole("admin", role ?? "none");
}

function slackOut(found: { webhook_url: string; channel: string; kinds: NotificationKind[] }) {
  return {
    channel: found.channel,
    kinds: found.kinds,
    webhook_hint: `…${found.webhook_url.slice(-4)}`,
  };
}
