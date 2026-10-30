/**
 * Agent runs (4.12): a run's steps as the timeline reads them. Each step is a model turn, a
 * tool call, a retrieval (round 0, the evidence pick) or a check, with when it started into the
 * run and how long it took (`offset_ms`, `ms`): the waterfall's bars. Calls made at once
 * overlap, and are drawn side by side.
 */
import { VERBS } from "./ask";
import type { Schemas } from "./types";

export type Run = Schemas["RunOut"];
export type RunDetail = Schemas["RunDetail"];
/** A step as the live stream sends it (its output only in the run's detail). */
export type Step = Omit<Schemas["StepOut"], "output"> & { output?: unknown };

const KINDS: Record<string, string> = {
  round0: "Searching before the first turn",
  evidence: "Choosing the evidence",
  verify: "Checking the answer against the code",
  gap: "Searching for what was missing",
};

/** What a step did, in words: "Reading files · backend/app/models.py". */
export function stepTitle(step: Step): string {
  if (step.kind === "llm") {
    const turn = typeof step.input.turn === "number" ? ` ${String(step.input.turn)}` : "";
    return `Model turn${turn}`;
  }
  if (step.kind === "tool") {
    const verb = VERBS[step.name] ?? step.name;
    const target = targetOf(step);
    return target ? `${verb} · ${target}` : verb;
  }
  return KINDS[step.name] ?? step.name;
}

function targetOf(step: Step): string | null {
  const input = step.input;
  for (const key of ["path", "query", "pattern", "name", "entry", "key"]) {
    const value = input[key];
    if (typeof value === "string" && value) return value;
  }
  return null;
}

/** The files a step read or named (a read's path, the evidence's targets), in order. */
export function filesOf(step: Step): string[] {
  const found: string[] = [];
  const add = (value: unknown) => {
    if (typeof value === "string" && value && !found.includes(value)) found.push(value);
  };
  if (step.kind !== "tool") return found;
  if (step.name === "read_code" || step.name === "list_dir") add(step.input.path);
  if (Array.isArray(step.input.paths)) step.input.paths.forEach(add);
  if (step.name === "submit_evidence" && Array.isArray(step.input.candidates)) {
    for (const candidate of step.input.candidates as unknown[]) {
      if (candidate && typeof candidate === "object") {
        const target = (candidate as Record<string, unknown>).target;
        if (typeof target === "string" && !target.startsWith("sym:")) add(target);
      }
    }
  }
  return step.name === "list_dir" ? [] : found; // a listing names a directory, not a file
}

/** Every file the run read, each with the first step that read it. */
export function filesRead(steps: readonly Step[]): { path: string; idx: number }[] {
  const first = new Map<string, number>();
  for (const step of steps) {
    for (const path of filesOf(step)) if (!first.has(path)) first.set(path, step.idx);
  }
  return [...first].map(([path, idx]) => ({ path, idx }));
}

export interface StepGroup {
  steps: Step[];
  /** "Searching code ×3" for repeated calls, else the step's title. */
  label: string;
}

/** Steps with each run of the same tool called again and again folded into one: the
 * aggregated view. */
export function aggregate(steps: readonly Step[]): StepGroup[] {
  const groups: StepGroup[] = [];
  for (const step of steps) {
    const last = groups.at(-1);
    const head = last?.steps[0];
    if (last && head && step.kind === "tool" && head.kind === "tool" && head.name === step.name) {
      last.steps.push(step);
      last.label = `${VERBS[step.name] ?? step.name} ×${String(last.steps.length)}`;
    } else {
      groups.push({ steps: [step], label: stepTitle(step) });
    }
  }
  return groups;
}

/** Each step's row in the waterfall: steps that overlap in time (calls made at once) get rows
 * of their own, side by side; one after another share the first free row. */
export function lanes(steps: readonly Step[]): Map<number, number> {
  const ends: number[] = []; // each lane's end so far
  const lane = new Map<number, number>();
  for (const step of [...steps].sort((a, b) => a.offset_ms - b.offset_ms || a.idx - b.idx)) {
    let at = ends.findIndex((end) => end <= step.offset_ms);
    if (at === -1) at = ends.push(0) - 1;
    ends[at] = step.offset_ms + Math.max(step.ms, 1);
    lane.set(step.idx, at);
  }
  return lane;
}

/** How long the run took, as far as its steps say (its latency once it has one). */
export function spanOf(steps: readonly Step[], latencyMs: number | null): number {
  const last = Math.max(0, ...steps.map((s) => s.offset_ms + s.ms));
  return Math.max(latencyMs ?? 0, last, 1);
}

/** A bar's place: where it starts and how wide it is, as shares of the run. */
export function bar(step: Step, span: number): { left: number; width: number } {
  const left = Math.min(step.offset_ms / span, 1);
  return { left, width: Math.max(Math.min(step.ms / span, 1 - left), 0.004) };
}

export function formatMs(ms: number): string {
  if (ms < 1000) return `${String(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
}
