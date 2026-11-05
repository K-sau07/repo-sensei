/**
 * The mock audit log (10.7.2): a few days of what an org records, newest first, filtered by
 * action and actor and paged by `before` as the API pages it; a month of its usage; and its
 * models, a step raised only to what the API allows.
 */
import { http, HttpResponse } from "msw";

import type { AuditEvent } from "@/lib/orgs/audit";
import type { EffortChoice, ModelsOut, Step, TierChoice } from "@/lib/orgs/models";
import { atLeast, db, HUBOT, OCTO, saveDb } from "@/mocks/db";

import { apiError } from "./errors";
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

/** A month of an org's spend, fixed so stories and tests can name it. */
function usage() {
  const days = Array.from({ length: 30 }, (_, i) => {
    const day = new Date(Date.parse("2026-09-19T00:00:00Z") + i * 86_400_000);
    const llm = i % 7 === 5 ? 0 : 0.4 + ((i * 37) % 11) / 10;
    return {
      day: day.toISOString().slice(0, 10),
      cost_usd: llm.toFixed(6),
      by_kind: llm ? { llm: (llm * 0.9).toFixed(6), embedding: (llm * 0.1).toFixed(6) } : {},
    };
  });
  return {
    today: {
      spent_usd: "3.400000",
      held_usd: "0.500000",
      cap_usd: "20",
      warn_share: 0.8,
      resets_at: "2026-10-19T00:00:00Z",
    },
    month: { spent_usd: "18.250000", cap_usd: "25", resets_at: "2026-11-01T00:00:00Z" },
    days,
    models: [
      {
        provider: "anthropic",
        model: "claude-sonnet-5-5",
        kind: "llm",
        calls: 412,
        tokens_in: 9_812_000,
        tokens_out: 402_000,
        cost_usd: "14.100000",
      },
      {
        provider: "voyage",
        model: "voyage-code-3",
        kind: "embedding",
        calls: 96,
        tokens_in: 3_400_000,
        tokens_out: 0,
        cost_usd: "2.150000",
      },
    ],
    people: [
      { login: "octo", cost_usd: "11.000000" },
      { login: null, cost_usd: "4.250000" },
      { login: "hubot", cost_usd: "1.000000" },
    ],
    limits: {
      asks_per_minute: 6,
      asks_per_day: 200,
      diagrams_per_minute: 6,
      index_runs_per_day: 30,
      public_imports_per_day: 5,
    },
  };
}

const STEPS: Record<Step, { tiers: TierChoice[]; efforts: EffortChoice[] }> = {
  answers: { tiers: ["recommended", "deep"], efforts: ["recommended", "medium", "high"] },
  diagrams: {
    tiers: ["recommended", "standard", "deep"],
    efforts: ["recommended", "medium", "high"],
  },
  fixes: { tiers: ["recommended", "deep"], efforts: ["recommended", "high"] },
};

function modelsOut(org: string): ModelsOut {
  const chosen = db.modelChoices?.[org] ?? {};
  return {
    steps: (Object.keys(STEPS) as Step[]).map((step) => ({
      step,
      tier: chosen[step]?.tier ?? "recommended",
      effort: chosen[step]?.effort ?? "recommended",
      ...STEPS[step],
    })),
    models: { fast: "claude-haiku-5-5", standard: "claude-sonnet-5-5", deep: "claude-opus-5-5" },
  };
}

function modelsAdmin(org: string): Response | null {
  const access = orgAccess(org);
  if (access instanceof Response) return access;
  return atLeast(access.role, "admin") ? null : insufficientRole("admin", access.role);
}

export const auditHandlers = [
  http.get<{ org: string }>("*/api/v1/orgs/:org/settings/models", ({ params }) => {
    return modelsAdmin(params.org) ?? HttpResponse.json(modelsOut(params.org));
  }),

  http.patch<{ org: string }>("*/api/v1/orgs/:org/settings/models", async ({ params, request }) => {
    const refused = modelsAdmin(params.org);
    if (refused) return refused;
    const body = (await request.json()) as Partial<
      Record<Step, { tier?: TierChoice; effort?: EffortChoice }>
    >;
    let chosen = { ...db.modelChoices?.[params.org] };
    for (const step of Object.keys(STEPS) as Step[]) {
      const wanted = body[step];
      if (!wanted) continue;
      const tier = wanted.tier ?? "recommended";
      const effort = wanted.effort ?? "recommended";
      if (!STEPS[step].tiers.includes(tier) || !STEPS[step].efforts.includes(effort)) {
        return apiError(422, "invalid_model_choice", `${step} can't run on that`);
      }
      chosen =
        tier === "recommended" && effort === "recommended"
          ? Object.fromEntries(Object.entries(chosen).filter(([key]) => key !== step))
          : { ...chosen, [step]: { tier, effort } };
    }
    db.modelChoices = { ...db.modelChoices, [params.org]: chosen };
    saveDb();
    return HttpResponse.json(modelsOut(params.org));
  }),

  http.get<{ org: string }>("*/api/v1/orgs/:org/usage", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    if (!atLeast(access.role, "admin")) return insufficientRole("admin", access.role);
    return HttpResponse.json(usage());
  }),

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
