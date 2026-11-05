/**
 * The mock insights (10.6.1): a month of an org's questions over its repositories, how the
 * answers went, the modules they cite and each week's adoption, the costs for admins only, as
 * the API counts them. Deterministic: the same numbers every time, ending today (UTC).
 */
import { http, HttpResponse } from "msw";

import type { Schemas } from "@/lib/api/types";
import { atLeast, db } from "@/mocks/db";

import { orgAccess } from "./orgs";

type Insights = Schemas["InsightsOut"];

const DAY_MS = 86_400_000;

const MODULES: [string, number][] = [
  ["backend/app", 31],
  ["frontend/src", 22],
  ["backend/tests", 9],
  ["frontend/tests", 4],
  ["", 3],
];

export function insights(org: string, costs: boolean): Insights {
  const repos = db.repos[org] ?? [];
  const today = Date.UTC(
    new Date().getUTCFullYear(),
    new Date().getUTCMonth(),
    new Date().getUTCDate(),
  );
  const days = Array.from({ length: 30 }, (_, n) => {
    const day = new Date(today - (29 - n) * DAY_MS);
    const weekday = day.getUTCDay();
    const questions = repos.length === 0 ? 0 : weekday === 0 || weekday === 6 ? n % 3 : 4 + (n % 5);
    return {
      day: day.toISOString().slice(0, 10),
      questions,
      people: Math.min(questions, 1 + (n % 3)),
      cost_usd: costs ? (questions * 0.06).toFixed(2) : null,
    };
  });
  const total = days.reduce((sum, d) => sum + d.questions, 0);
  const mondays = [...new Set(days.map((d) => monday(d.day)))];
  return {
    days,
    repos: repos.map((repo, i) => ({
      repo_id: repo.id,
      full_name: repo.full_name,
      slug: repo.slug,
      questions: i === 0 ? total : 0,
      cost_usd: costs ? (total * 0.06).toFixed(2) : null,
    })),
    answers:
      total === 0
        ? { answered: 0, abstained: 0, helpful: 0, not_helpful: {} }
        : {
            answered: total - 6,
            abstained: 6,
            helpful: 19,
            not_helpful: { wrong_file: 2, missing_context: 1 },
          },
    weeks: mondays.map((week, i) => ({
      week,
      answered: total === 0 ? 0 : 18 + i,
      abstained: total === 0 ? 0 : 1 + (i % 2),
      helpful: total === 0 ? 0 : 3 + i,
      not_helpful: total === 0 ? 0 : i % 2,
      askers: total === 0 ? 0 : 2 + (i % 2),
      fixes: total === 0 ? 0 : i % 3,
      changes: total === 0 ? 0 : i % 2,
      diagrams: total === 0 ? 0 : 1 + (i % 2),
    })),
    modules:
      total === 0 || !repos[0]
        ? []
        : MODULES.map(([path, cited]) => ({
            repo_id: repos[0]?.id ?? "",
            full_name: repos[0]?.full_name ?? "",
            path,
            cited,
          })),
    costs,
  };
}

function monday(day: string): string {
  const date = new Date(`${day}T00:00:00Z`);
  const back = (date.getUTCDay() + 6) % 7;
  return new Date(date.getTime() - back * DAY_MS).toISOString().slice(0, 10);
}

export const insightHandlers = [
  http.get<{ org: string }>("*/api/v1/orgs/:org/insights", ({ params }) => {
    const access = orgAccess(params.org);
    if (access instanceof Response) return access;
    return HttpResponse.json(insights(params.org, atLeast(access.role, "admin")));
  }),
];
