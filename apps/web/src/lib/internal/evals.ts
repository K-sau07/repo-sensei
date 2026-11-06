/**
 * The staff evals dashboard (10.6.2): what kind of run each is, the score its items are compared
 * on (as `sensei_evals.reports.item_metric` picks it), and which way each metric is better (as
 * `lower_is_better` says), so the pages and the API agree.
 */
import type { Schemas } from "@/lib/api/types";

export type EvalSummary = Schemas["EvalRunSummary"];
export type EvalRun = Schemas["EvalRunOut"];
export type EvalComparison = Schemas["EvalComparisonOut"];
export type MetricChange = Schemas["MetricChangeOut"];

export type RunKind = "retrieval" | "bench" | "fixbench" | "answers";

export const KIND_LABELS: Record<RunKind, string> = {
  retrieval: "Retrieval",
  bench: "CrossLayerBench",
  fixbench: "FixBench",
  answers: "Answers",
};

const RANK_K = 10;

export function runKind(run: Pick<EvalSummary, "metrics" | "retriever">): RunKind {
  if (run.metrics.fixbench) return "fixbench";
  if (run.metrics.bench) return "bench";
  if (run.retriever.startsWith("ask:")) return "answers";
  return "retrieval";
}

/** The per-item score of a run of its kind (its overall mean is the run's headline). */
export function itemMetric(run: Pick<EvalSummary, "metrics" | "retriever" | "config">): string {
  switch (runKind(run)) {
    case "fixbench":
      return "fixed";
    case "bench":
      return "hop_recall";
    case "answers":
      return "checklist";
    case "retrieval": {
      const listed = Array.isArray(run.config.ks) ? (run.config.ks as number[]) : [];
      const ks = listed.length > 0 ? listed : [RANK_K]; // as Python's `ks or [RANK_K]`
      return `recall_symbol@${ks.includes(RANK_K) ? RANK_K : Math.min(...ks)}`;
    }
  }
}

export function headline(run: EvalSummary): { metric: string; value: number | null } {
  const metric = itemMetric(run);
  const overall = (run.metrics.overall ?? {}) as Record<string, unknown>;
  const value = overall[metric];
  return { metric, value: typeof value === "number" ? value : null };
}

const LOWER_IS_BETTER = new Set([
  "contradicted",
  "unasked",
  "failed",
  "unsafe",
  "blocked",
  "not_reproduced",
  "cost_usd",
]);

export function lowerIsBetter(name: string): boolean {
  return (
    LOWER_IS_BETTER.has(name) ||
    name.endsWith("_ece") ||
    name.endsWith("_brier") ||
    name.endsWith("_ms")
  );
}

/** A score as it reads: costs in dollars, times in ms, shares to three places. */
export function formatMetric(name: string, value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (name === "cost_usd") return `$${value.toFixed(2)}`;
  if (name.endsWith("_ms")) return `${Math.round(value)} ms`;
  return Number.isInteger(value) && Math.abs(value) > 1 ? String(value) : value.toFixed(3);
}

export function formatDelta(change: MetricChange): string {
  if (change.delta === null) return "—";
  if (Math.abs(change.delta) < 1e-9) return "0";
  const sign = change.delta > 0 ? "+" : "−";
  return `${sign}${formatMetric(change.name, Math.abs(change.delta))}`;
}
