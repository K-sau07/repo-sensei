/**
 * Requested changes (9.1-9.3): a change described in words, planned file by file, approved,
 * built with its tests and verified, to a draft pull request. A change is a run of the fix
 * pipeline's (`kind: "change"`), so its shapes are the fix's; its plan is read here: the files
 * grouped by layer, the tests it adds, the patterns it follows, its blast radius; and a person's
 * edits to it, as the API takes them.
 */
import { LAYER_LABELS, type ScopeLayer } from "./ask";
import type { Fix, FixStage, FixSummary } from "./fixes";
import type { Schemas } from "./types";

export type Change = Fix;
export type ChangeSummary = FixSummary;
export type PlanEdits = Schemas["PlanEdits"];

export type Layer = "ui" | "api" | "svc" | "db" | "other" | "test";

/** The layers in the order the plan canvas lays them out, page to table, tests last. */
export const LAYERS: readonly Layer[] = ["ui", "api", "svc", "db", "other", "test"];

export const LAYER_NAMES: Record<Layer, string> = {
  ui: "UI",
  api: "API",
  svc: "Service",
  db: "Data",
  other: "Other",
  test: "Tests",
};

/** A change's stages (no investigation or reproduction: it starts from its request). */
export const CHANGE_STAGES: readonly FixStage[] = [
  "intake",
  "plan",
  "awaiting_approval",
  "patch",
  "verify",
  "pr",
  "done",
];

export interface PlannedFile {
  path: string;
  change: "modify" | "create";
  why: string;
  layer: Layer;
}

export interface PlannedTest {
  path: string;
  checks: string;
  layer?: Layer;
}

export interface Pattern {
  claim: string;
  path: string;
  start_line: number;
  end_line: number;
}

export interface PlanStep {
  description: string;
  path: string;
}

export interface BlastItem {
  key: string;
  name: string;
  path: string | null;
  score: number;
  distance: number;
}

export interface Blast {
  changed: string[];
  counts: Partial<Record<"routes" | "screens" | "tests" | "tables" | "symbols", number>>;
  top: Partial<Record<"routes" | "screens" | "tests" | "tables" | "symbols", BlastItem[]>>;
  truncated: boolean;
}

export interface PlanRequest {
  by: string;
  comment: string | null;
  edits: PlanEdits | null;
  revision: number;
}

export interface ChangePlan {
  summary: string;
  files: PlannedFile[];
  tests: PlannedTest[];
  patterns: Pattern[];
  steps: PlanStep[];
  risks: string[];
  risk: { level: "low" | "medium" | "high"; reasons: string[] } | null;
  blast_radius: Blast | null;
  revision: number;
  requests: PlanRequest[];
}

function list<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

/** The change's plan as the canvas reads it (null: not planned yet). */
export function planOf(change: Pick<Change, "plan">): ChangePlan | null {
  const raw = change.plan as Partial<ChangePlan> | null;
  if (!raw) return null;
  return {
    summary: raw.summary ?? "",
    files: list<PlannedFile>(raw.files).map((f) => ({ ...f, layer: f.layer })),
    tests: list<PlannedTest>(raw.tests),
    patterns: list<Pattern>(raw.patterns),
    steps: list<PlanStep>(raw.steps),
    risks: list<string>(raw.risks),
    risk: raw.risk ?? null,
    blast_radius: raw.blast_radius ?? null,
    revision: typeof raw.revision === "number" ? raw.revision : 0,
    requests: list<PlanRequest>(raw.requests),
  };
}

/** The plan's files by layer, in the canvas's order (empty layers left out). */
export function byLayer(files: readonly PlannedFile[]): [Layer, PlannedFile[]][] {
  return LAYERS.flatMap((layer) => {
    const found = files.filter((f) => (LAYERS.includes(f.layer) ? f.layer : "other") === layer);
    return found.length > 0 ? [[layer, found] as [Layer, PlannedFile[]]] : [];
  });
}

/** A person's draft of the plan, as the API takes it: only what differs from the planner's. */
export function editsOf(
  plan: ChangePlan,
  draft: { files: PlannedFile[]; tests: PlannedTest[]; summary: string },
): PlanEdits | null {
  const edits: PlanEdits = {};
  const files = draft.files.map(({ path, change, why }) => ({ path, change, why }));
  const before = plan.files.map(({ path, change, why }) => ({ path, change, why }));
  if (JSON.stringify(files) !== JSON.stringify(before)) edits.files = files;
  const tests = draft.tests.map(({ path, checks }) => ({ path, checks }));
  if (
    JSON.stringify(tests) !==
    JSON.stringify(plan.tests.map(({ path, checks }) => ({ path, checks })))
  )
    edits.tests = tests;
  if (draft.summary.trim() !== plan.summary.trim()) edits.summary = draft.summary.trim();
  return Object.keys(edits).length > 0 ? edits : null;
}

/** A new file's layer, guessed as the planner would place it: a test path, a page, else other. */
export function layerFor(path: string): Layer {
  const lower = path.toLowerCase();
  if (/(^|\/)(tests?|__tests__)\/|(^|\/)test_[^/]+\.py$|\.(test|spec)\.[jt]sx?$/.test(lower))
    return "test";
  if (/\.(tsx|jsx|vue|svelte)$/.test(lower)) return "ui";
  if (/(^|\/)(api|routes?|routers?|handlers?|controllers?)\//.test(lower)) return "api";
  if (/(^|\/)(models?|migrations?|alembic|db|schemas?)\//.test(lower)) return "db";
  if (/(^|\/)(services?|workers?|tasks?|jobs|core|lib)\//.test(lower)) return "svc";
  return "other";
}

/** A request as it is sent: the description, and the layers it should stay in (in words the
 * planner reads). */
export function requestText(description: string, layers: readonly ScopeLayer[]): string {
  const text = description.trim();
  if (layers.length === 0) return text;
  const names = layers.map((layer) => LAYER_LABELS[layer]).join(", ");
  return `${text}\n\nScope: keep the change to these layers: ${names}.`;
}
