/**
 * An org's model and effort per step (10.7.2), as the Models settings say them: each step's
 * recommended setting in words, and what it may be raised to. A choice is a floor: a step that
 * already runs higher for a hard case still does.
 */
import type { Schemas } from "@/lib/api/types";

export type ModelsOut = Schemas["ModelsOut"];
export type StepOut = Schemas["ModelStepOut"];
export type Step = StepOut["step"];
export type TierChoice = StepOut["tier"];
export type EffortChoice = StepOut["effort"];

/** "claude-opus-5-5" → "Opus 5.5"; an id it doesn't recognize, as it is. */
export function modelName(id: string): string {
  const match = /^claude-([a-z]+)-(\d+(?:-\d+)*)$/.exec(id);
  if (!match) return id;
  const [, family = "", version = ""] = match;
  return `${family.charAt(0).toUpperCase()}${family.slice(1)} ${version.replaceAll("-", ".")}`;
}

export const STEP_LABELS: Record<Step, string> = {
  answers: "Answers",
  diagrams: "Diagrams",
  fixes: "Fixes and changes",
};

/** What a step runs on when it is left as recommended. */
export function recommended(step: Step, models: ModelsOut["models"]): string {
  const fast = modelName(models.fast ?? "");
  const standard = modelName(models.standard ?? "");
  const deep = modelName(models.deep ?? "");
  switch (step) {
    case "answers":
      return `${standard}, at low effort for a lookup and medium otherwise; ${deep} at high effort in deep mode and for questions that cross layers.`;
    case "diagrams":
      return `${fast} at low effort to label a diagram; every label is checked against the code graph.`;
    case "fixes":
      return `${standard} at medium effort; ${deep} at high effort to investigate a report with no frame in the repository, to plan a change that names no file, and to verify every patch.`;
  }
}

export function tierLabel(tier: TierChoice, models: ModelsOut["models"]): string {
  if (tier === "recommended") return "Recommended";
  return `At least ${modelName(models[tier] ?? tier)}`;
}

export function effortLabel(effort: EffortChoice): string {
  return effort === "recommended" ? "Recommended" : `At least ${effort}`;
}
