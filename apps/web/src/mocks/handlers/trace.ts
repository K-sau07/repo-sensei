/**
 * The mock `GET …/trace?from=` and `POST …/trace` over the mock snapshot's traces
 * (mocks/trace.ts), with the API's checks in its order: the query or body (422), the user's graph
 * requests per minute (429 `rate_limited`, with `Retry-After`), the snapshot (404 before any
 * index), whether its graph is built (409 `graph_not_ready` during the first run), then the entry
 * (404 `trace_entry_not_found`). The fixture's traces are at the defaults: `kind`, `to` and
 * `min_conf` are checked, not applied.
 *
 * Situations worth seeing, from the browser:
 *   localStorage.setItem("sensei.mock.trace", "unlinked")
 * "unlinked" (the links stage hasn't run: the Login page's trace stays in the code), "slow" (1.5 s
 * per trace) or "rate-limited" (429, retry in 30 s).
 */
import { delay, http, HttpResponse } from "msw";

import type { Trace, TraceEntryKind } from "@/lib/api/trace";
import { mockTraces } from "@/mocks/trace";

import { validationFailed } from "./browse";
import { apiError } from "./errors";
import { graphReady } from "./graph";

export const TRACE_SCENARIO_KEY = "sensei.mock.trace";
export type TraceScenario = "unlinked" | "slow" | "rate-limited";
const SCENARIOS = new Set<string>(["unlinked", "slow", "rate-limited"]);
const SLOW_MS = 1500;
const RETRY_AFTER_S = 30;
const MAX_ENTRY = 2000;
const KINDS: ReadonlySet<string> = new Set<TraceEntryKind>(["auto", "ui", "route", "symbol"]);

export function traceScenario(): TraceScenario | null {
  if (typeof localStorage === "undefined") return null;
  const value = localStorage.getItem(TRACE_SCENARIO_KEY);
  return value !== null && SCENARIOS.has(value) ? (value as TraceScenario) : null;
}

interface Params {
  org: string;
  repo: string;
}

interface Query {
  from: string;
  kind: string;
  to: string | null;
  minConf: number;
}

function bodyFailed(field: string, message: string) {
  return apiError(422, "validation_failed", "Request validation failed.", [
    { loc: ["body", field], msg: message, type: "value_error" },
  ]);
}

/** The request's fields as the API validates them, or its 422. */
function checked(query: Partial<Record<keyof Query, unknown>>, where: "query" | "body") {
  const failed = where === "query" ? validationFailed : bodyFailed;
  const { from, kind = "auto", to = null, minConf = 0 } = query;
  if (typeof from !== "string") return failed("from", "Field required");
  if (from.length === 0) return failed("from", "String should have at least 1 character");
  if (from.length > MAX_ENTRY) {
    return failed("from", "String should have at most 2000 characters");
  }
  if (typeof kind !== "string" || !KINDS.has(kind)) {
    return failed("kind", "Input should be 'auto', 'ui', 'route' or 'symbol'");
  }
  if (to !== null && (typeof to !== "string" || to.length === 0 || to.length > MAX_ENTRY)) {
    return failed("to", "String should have between 1 and 2000 characters");
  }
  if (typeof minConf !== "number" || !(minConf >= 0 && minConf <= 1)) {
    return failed("min_conf", "Input should be between 0 and 1");
  }
  return { from, kind, to, minConf };
}

/** The trace, after the checks that come before it. */
async function traced(params: Params, request: Request, query: Query | Response) {
  if (query instanceof Response) return query;
  const scenario = traceScenario();
  if (scenario === "slow") await delay(SLOW_MS);
  if (scenario === "rate-limited") {
    const response = apiError(429, "rate_limited", "Too many graph requests. Wait a moment.");
    response.headers.set("Retry-After", String(RETRY_AFTER_S));
    return response;
  }
  const found = graphReady(params, request);
  if (found instanceof Response) return found;
  const trace = (await mockTraces()).find(query.from, scenario !== "unlinked");
  if (!trace) {
    return apiError(
      404,
      "trace_entry_not_found",
      `Nothing in this commit's graph is called '${query.from}', and no endpoint or code is named for it.`,
    );
  }
  return HttpResponse.json<Trace>(trace, {
    headers: { "Cache-Control": "private, no-cache", "X-Commit-Sha": found.commit },
  });
}

async function bodyOf(request: Request): Promise<Record<string, unknown>> {
  try {
    const body = (await request.json()) as unknown;
    return typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

export const traceHandlers = [
  http.get<Params>("*/api/v1/orgs/:org/repos/:repo/trace", async ({ params, request }) => {
    const search = new URL(request.url).searchParams;
    const minConf = search.get("min_conf");
    const query = checked(
      {
        from: search.get("from") ?? undefined,
        kind: search.get("kind") ?? undefined,
        to: search.get("to"),
        minConf: minConf === null ? undefined : Number(minConf),
      },
      "query",
    );
    return traced(params, request, query);
  }),
  http.post<Params>("*/api/v1/orgs/:org/repos/:repo/trace", async ({ params, request }) => {
    const body = await bodyOf(request);
    const query = checked(
      { from: body.from, kind: body.kind, to: body.to ?? null, minConf: body.min_conf },
      "body",
    );
    return traced(params, request, query);
  }),
];
