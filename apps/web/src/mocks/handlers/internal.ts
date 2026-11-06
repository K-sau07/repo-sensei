/**
 * The mock staff evals (10.6.2): a few stored runs of each kind (retrieval, CrossLayerBench,
 * FixBench), each with its baseline (the run before it of the same suite, version and retriever)
 * and the comparison the API makes, regressions counted the right way round. Staff only: anyone
 * else gets a 404, as from the API.
 */
import { http, HttpResponse } from "msw";

import {
  itemMetric,
  lowerIsBetter,
  type EvalComparison,
  type EvalRun,
  type EvalSummary,
  type MetricChange,
} from "@/lib/internal/evals";
import { OCTO } from "@/mocks/db";

import { apiError } from "./errors";
import { currentUser } from "./session";

const SHA = "b1125d8839482cb7bbae603ddc6d0081bf87532e";
const at = (day: number) => `2026-10-${String(day).padStart(2, "0")}T12:00:00Z`;
const id = (n: number) => `0199f000-0000-7000-8000-0000000e${String(n).padStart(4, "0")}`;

function retrievalItem(item: string, query: string, recall: number, gold: string, found: string) {
  return {
    id: item,
    query_class: "lookup",
    answerable: true,
    query,
    abstained: false,
    latency_ms: 140,
    metrics: { "recall_symbol@10": recall, "recall_file@10": recall },
    hits: [{ path: found, lines: [12, 30], score: 0.82, symbol: null }],
    missed: recall === 1 ? [] : [{ layer: "api", path: gold, symbol: "create_item" }],
  };
}

function retrieval(n: number, day: number, recall: number[], cost: number): EvalRun {
  const items = [
    retrievalItem("create-item", "Where is an item created?", recall[0] ?? 0,
      "backend/app/crud.py", recall[0] === 1 ? "backend/app/crud.py" : "backend/app/models.py"),
    retrievalItem("login-handler", "Which route signs a user in?", recall[1] ?? 0,
      "backend/app/api/routes/login.py", "backend/app/api/routes/login.py"),
    retrievalItem("items-page", "What page lists items?", recall[2] ?? 0,
      "frontend/src/routes/items.tsx", "frontend/src/routes/items.tsx"),
  ]; // prettier-ignore
  const mean = recall.reduce((a, b) => a + b, 0) / recall.length;
  return {
    id: id(n),
    suite: "fastapi-golden",
    suite_version: 2,
    retriever: "hybrid",
    config: { ks: [10], repo: "fastapi-template" },
    repo_sha: SHA,
    git_sha: "1".repeat(40),
    metrics: {
      overall: { "recall_symbol@10": mean, "recall_file@10": mean, cost_usd: cost, p50_ms: 140 },
    },
    created_at: at(day),
    per_item: items,
  };
}

function bench(n: number, day: number, matched: boolean): EvalRun {
  const hops = [
    { kind: "page", name: "Login", path: "frontend/src/routes/login.tsx", layer: "ui" },
    { kind: "call", name: "loginAccessToken", path: "frontend/src/client/sdk.gen.ts", layer: "ui" },
    { kind: "route", name: "POST /login/access-token", path: "backend/app/api/routes/login.py",
      layer: "api" },
    { kind: "function", name: "authenticate", path: "backend/app/crud.py", layer: "service" },
  ]; // prettier-ignore
  const traced = hops.slice(0, matched ? 4 : 3).map((hop, i) => ({
    ...hop,
    lines: [10 + i, 20 + i],
    confidence: 0.9 - i * 0.1,
    gold: i,
  }));
  const recall = traced.length / hops.length;
  return {
    id: id(n),
    suite: "crosslayerbench-v0",
    suite_version: 0,
    retriever: "trace",
    config: { repo: "fastapi-template" },
    repo_sha: SHA,
    git_sha: "2".repeat(40),
    metrics: {
      bench: true,
      overall: { hop_recall: recall, exact: matched ? 1 : 0, hop_ece: 0.08 },
    },
    created_at: at(day),
    per_item: [
      {
        id: "login-flow",
        query_class: "flow",
        answerable: true,
        query: "Signing in, from the login page to the user's row",
        abstained: false,
        latency_ms: 80,
        metrics: { hop_recall: recall, exact: matched ? 1 : 0 },
        hits: traced,
        missed: matched
          ? []
          : [{ path: "backend/app/crud.py", layer: "service", symbol: "authenticate" }],
      },
    ],
  };
}

function fixbench(n: number, day: number): EvalRun {
  const bugs: [string, string, string | null][] = [
    ["01-off-by-one", "fixed", null],
    ["02-null-owner", "fixed", null],
    ["03-stale-cache", "not_reproduced", "not_reproduced: the test it wrote passed before the fix"],
  ];
  return {
    id: id(n),
    suite: "fixbench",
    suite_version: 3,
    retriever: "sensei",
    config: { runner: "sandbox", items: bugs.length },
    repo_sha: "",
    git_sha: "3".repeat(40),
    metrics: {
      fixbench: true,
      overall: { fixed: 2 / 3, unsafe: 0, not_reproduced: 1 / 3, cost_usd: 1.4 },
    },
    created_at: at(day),
    per_item: bugs.map(([bug, verdict, error]) => ({
      id: bug,
      query_class: "logic",
      answerable: true,
      query: "logic (backend, low risk)",
      abstained: verdict !== "fixed",
      latency_ms: 95_000,
      metrics: { fixed: verdict === "fixed" ? 1 : 0, unsafe: 0, cost_usd: 0.47 },
      hits: [],
      missed: [],
      error,
    })),
  };
}

/** Oldest first. */
export const EVAL_RUNS: EvalRun[] = [
  retrieval(1, 10, [1, 1, 0], 0.4),
  bench(2, 12, false),
  retrieval(3, 14, [1, 1, 1], 0.4),
  fixbench(4, 15),
  bench(5, 16, true),
  retrieval(6, 18, [0, 1, 1], 0.6), // worse recall (symbol and file), dearer: three regressions
];

function baselineOf(run: EvalRun): EvalRun | null {
  const earlier = EVAL_RUNS.filter(
    (r) =>
      r.suite === run.suite &&
      r.suite_version === run.suite_version &&
      r.retriever === run.retriever &&
      r.created_at < run.created_at,
  );
  return earlier.at(-1) ?? null;
}

function changes(a: EvalRun, b: EvalRun) {
  const before = (a.metrics.overall ?? {}) as Record<string, number>;
  const after = (b.metrics.overall ?? {}) as Record<string, number>;
  const all: MetricChange[] = [...new Set([...Object.keys(before), ...Object.keys(after)])].map(
    (name) => {
      const x = before[name] ?? null;
      const y = after[name] ?? null;
      return { name, before: x, after: y, delta: x === null || y === null ? null : y - x };
    },
  );
  const gain = (c: MetricChange) => (lowerIsBetter(c.name) ? -1 : 1) * (c.delta ?? 0);
  const regressions = all.filter((c) => gain(c) < -1e-9).sort((p, q) => gain(p) - gain(q));
  const improvements = all.filter((c) => gain(c) > 1e-9).sort((p, q) => gain(q) - gain(p));
  return { regressions, improvements, unchanged: all.filter((c) => Math.abs(gain(c)) <= 1e-9) };
}

function summary(run: EvalRun): EvalSummary {
  const { per_item: _items, ...rest } = run;
  const base = baselineOf(run);
  return {
    ...rest,
    baseline_id: base?.id ?? null,
    regressions: base ? changes(base, run).regressions.length : null,
  };
}

function compared(run: EvalRun, base: EvalRun | null): EvalComparison {
  const own = summary(run);
  if (!base) {
    return {
      run: own,
      baseline: null,
      metric: null,
      regressions: [],
      improvements: [],
      unchanged: [],
      items_worse: [],
      items_better: [],
      warnings: [],
    };
  }
  const metric = itemMetric(run);
  const scoreOf = (r: EvalRun, item: string) => {
    const found = r.per_item.find((i) => i.id === item) as
      { metrics: Record<string, number> } | undefined;
    return found?.metrics[metric] ?? null;
  };
  const items = run.per_item.map((item) => ({
    id: item.id as string,
    query: item.query as string,
    before: scoreOf(base, item.id as string),
    after: scoreOf(run, item.id as string),
  }));
  const moved = items.filter((i) => i.before !== null && i.after !== null && i.before !== i.after);
  const { regressions, improvements, unchanged } = changes(base, run);
  return {
    run: { ...own, baseline_id: base.id, regressions: regressions.length },
    baseline: summary(base),
    metric,
    regressions,
    improvements,
    unchanged,
    items_worse: moved.filter((i) => (i.after ?? 0) < (i.before ?? 0)),
    items_better: moved.filter((i) => (i.after ?? 0) > (i.before ?? 0)),
    warnings: [],
  };
}

function staff(): Response | null {
  const user = currentUser();
  if (user instanceof Response) return user;
  return user.id === OCTO ? null : apiError(404, "not_found", "Not Found");
}

export const internalHandlers = [
  http.get("*/api/v1/internal/evals", ({ request }) => {
    const refused = staff();
    if (refused) return refused;
    const query = new URL(request.url).searchParams;
    const suite = query.get("suite");
    let items = EVAL_RUNS.filter((r) => !suite || r.suite === suite)
      .map(summary)
      .reverse();
    if (query.get("sort") === "regressions") {
      items = [...items].sort((a, b) => (b.regressions ?? 0) - (a.regressions ?? 0));
    }
    return HttpResponse.json({ items, next_cursor: null });
  }),

  http.get<{ run: string }>("*/api/v1/internal/evals/:run/compare", ({ params, request }) => {
    const refused = staff();
    if (refused) return refused;
    const run = EVAL_RUNS.find((r) => r.id === params.run);
    if (!run) return apiError(404, "not_found", "Eval run not found.");
    const wanted = new URL(request.url).searchParams.get("baseline");
    const base = wanted ? (EVAL_RUNS.find((r) => r.id === wanted) ?? null) : baselineOf(run);
    if (wanted && !base) return apiError(404, "not_found", "Baseline run not found.");
    return HttpResponse.json(compared(run, base));
  }),

  http.get<{ run: string }>("*/api/v1/internal/evals/:run", ({ params }) => {
    const refused = staff();
    if (refused) return refused;
    const run = EVAL_RUNS.find((r) => r.id === params.run);
    if (!run) return apiError(404, "not_found", "Eval run not found.");
    return HttpResponse.json({ ...run, ...summary(run) });
  }),
];
